import json
import base64
import re
import hashlib
from typing import Dict, Any, List, Optional
from enum import Enum

from pdf2image import convert_from_bytes
from datetime import datetime, timezone
from io import BytesIO

from database.models import InvoicesExtractedEmails, ServicesExtractedEmails, IncomingEmails, InvoiceCases, InvoiceTransitions
from core.invoices_validation import InvoicesValidation
from database.db_mysql import get_connection

from sqlalchemy.exc import IntegrityError
MODEL_DEFAULT = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
MODEL_POWERFUL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

conn_mysql = get_connection()


class EmailsState(str, Enum):
    RECIBIDO = "RECIBIDO"
    SIN_ADJUNTO = "SIN ADJUNTO"
    ADJUNTOS_INVALIDOS = "ADJUNTO INVALIDO"
    PROCESADO = "PROCESADO"
    ERROR = "ERROR"

class FacturasState(str, Enum):
    RECIBIDO = "RECIBIDO"
    LISTO_PARA_CARGAR = "LISTO PARA CARGAR"
    LOADED_BY_IT = "LOADED BY IT"
    LOAD_FAILED = "LOAD FAILED"
    DUPLICADO = "DUPLICADO"
    DESCARTADO = "DESCARTADO"
    EN_REVISION = "EN REVISION"
    RECHAZADA = "RECHAZADA"
    ERROR = "ERROR"


class JsonParser:
    """Utility for robust JSON parsing."""
    def safe_json_load(self, text: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        matches = re.findall(r"\{.*?\}", text, re.DOTALL)
        for match in matches:
            try:
                return json.loads(match)
            except json.JSONDecodeError:
                continue
        raise ValueError("No se pudo parsear JSON válido del texto proporcionado.")


class PdfBedrockExtractor:
    """Handles PDF to image conversion and extraction using Bedrock."""
    def __init__(self, bedrock_client, json_parser: JsonParser):
        self.bedrock_client = bedrock_client
        self.json_parser = json_parser

    def _pdf_to_base64_images(self, file_bytes: bytes) -> List[str]:
        """Converts PDF bytes to a list of base64 encoded PNG images."""
        images = convert_from_bytes(file_bytes, poppler_path="/opt/bin")
        base64_images = []
        for img in images:
            buffer = BytesIO()
            img.save(buffer, format="PNG")
            base64_images.append(base64.b64encode(buffer.getvalue()).decode("utf-8"))
        return base64_images

    def _invoke_bedrock_model(self, content: List[Dict[str, Any]], model_id: str) -> Dict[str, Any]:
        """Invokes the Bedrock model with the given content and model ID."""
        response = self.bedrock_client.invoke_model(
            modelId=model_id,
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 1500,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": content}],
                }
            ),
        )
        response_body = json.loads(response["body"].read())
        text = response_body["content"][0]["text"]
        return text.replace("```json", "").replace("```", "").strip()

    def extract_invoice_data(self, file_bytes: bytes, model_id: str = MODEL_DEFAULT) -> Optional[Dict[str, Any]]:
        """
        Extracts invoice data from PDF bytes using Bedrock.
        Performs a two-step process: validation (is it an invoice?) and then data extraction.
        """
        images_base64 = self._pdf_to_base64_images(file_bytes)
        if not images_base64:
            print("No images extracted from PDF.")
            return None

        validation_prompt = """
            Decime si este documento es una FACTURA.
            Responder SOLO:
            {
            "es_factura": true | false
            }
        """
        validation_content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}} for img_b64 in images_base64]
        validation_content.append({"type": "text", "text": validation_prompt})
        validation_response_text = self._invoke_bedrock_model(validation_content, model_id)
        validation_data = self.json_parser.safe_json_load(validation_response_text)

        if not validation_data or not validation_data.get("es_factura"):
            print(f"Documento no es una factura, se ignora archivo. Validation response: {validation_response_text}")
            return None

        extraction_prompt = """
            Analizá esta factura.
            Extraé:
            - cuit (del proveedor, excluir cuit AERO 30-70736214-2)
            - numero_factura
            - fecha (YYYY-MM-DD)
            - moneda
            - importe_total_final
            - servicios:
                - voucher
                - producto
                - nombre_del_viajero
                - importe
            IMPORTANTE:
            - Respetar la estructura visual de la tabla
            - Asociar correctamente cada importe con su pasajero
            - NO mezclar columnas
            - Devolver SOLO JSON válido
        """
        extraction_content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}} for img_b64 in images_base64]
        extraction_content.append({"type": "text", "text": extraction_prompt})
        extraction_response_text = self._invoke_bedrock_model(extraction_content, model_id)
        return self.json_parser.safe_json_load(extraction_response_text)


class S3AttachmentManager:
    """Handles S3 operations for attachments."""
    def __init__(self, s3_client, s3_bucket_destino: str, msg_id: str):
        self.s3_client = s3_client
        self.s3_bucket_destino = s3_bucket_destino
        self.msg_id = msg_id

    def generate_s3_key(self, filename: str, now: datetime) -> str:
        """Generates a unique S3 key for the attachment."""
        return (
            f"facturas/"
            f"Año={now.year}/"
            f"Mes={now.month:02d}/"
            f"Dia={now.day:02d}/"
            f"{self.msg_id}-{filename}"
        )

    def is_valid_invoice_attachment(self, content_type: str, filename: str) -> bool:
        """Checks if the attachment is a valid invoice type."""
        allowed_types = ["application/pdf", "text/xml", "application/xml"]
        allowed_extensions = (".pdf", ".xml")
        return content_type in allowed_types or filename.lower().endswith(allowed_extensions)

    def upload_attachment(self, filename: str, file_bytes: bytes, content_type: str) -> str:
        """Uploads the attachment to S3 and returns its key."""
        now = datetime.now()
        dest_key = self.generate_s3_key(filename, now)
        self.s3_client.put_object(
            Bucket=self.s3_bucket_destino,
            Key=dest_key,
            Body=file_bytes,
            ContentType=content_type,
        )
        print(f"Guardado en: {self.s3_bucket_destino}/{dest_key}")
        return dest_key


class EmailProcessor:
    def __init__(
        self, msg, operadores, s3_bucket_destino, s3_client, db_session, bedrock_client, msg_id,
    ):
        self.msg = msg
        self.operadores = operadores
        self.db_session = db_session
        self.msg_id = msg_id
        self.email_id = None
        self.json_parser = JsonParser()
        self.pdf_extractor = PdfBedrockExtractor(bedrock_client, self.json_parser)
        self.s3_manager = S3AttachmentManager(s3_client, s3_bucket_destino, msg_id)

    def normalizar_codigo(self, codigo: str) -> str:
        if codigo.startswith("540"):
            return codigo[3:]
        return codigo

    def _buscar_operador_por_cuit(self, cuit: str) -> Optional[List[Dict[str, Any]]]:
        """Busca operadores por CUIT."""
        for cuit_ops, operadores in self.operadores.items():
            cuit_limpio = cuit_ops.replace("-", "")
            if cuit_limpio == cuit.replace("-", ""):
                return operadores
        return None
    

    def insert_email(self):
        """Inserta el registro inicial del correo en la tabla incoming_emails."""
        attachments = list(self.msg.iter_attachments())
        attachment_count = len(attachments)
        
        state = EmailsState.RECIBIDO
        reason = None
        if attachment_count == 0:
            state = EmailsState.SIN_ADJUNTO
            reason = "El correo no contiene archivos adjuntos."

        email_record = IncomingEmails(
            message_id=self.msg_id,
            received_at=datetime.now(timezone.utc),
            sender=str(self.msg.get("From", "Desconocido")),
            subject=str(self.msg.get("Subject", "Sin Asunto")),
            has_attachments=attachment_count > 0,
            attachment_count=attachment_count,
            processing_state=state,
            processing_reason=reason
        )
        self.email_id = email_record.email_id
        with self.db_session() as session:
            session.add(email_record)
            session.commit()

    def process_email(self):
        self.insert_email()
        
        attachments_data_for_db = []
        
        for part in self.msg.iter_attachments():
            filename = part.get_filename()
            if not filename:
                continue

            content_type = part.get_content_type()
            print(f"Encontrado adjunto: {filename} ({content_type})")

            if not self.s3_manager.is_valid_invoice_attachment(content_type, filename):
                print(f"Archivo ignorado por tipo/extensión inválida: {filename}")
                continue

            file_bytes = part.get_payload(decode=True)
            attachment_hash = hashlib.sha256(file_bytes).hexdigest()

            data_agent = self.pdf_extractor.extract_invoice_data(file_bytes)
            if data_agent is None:
                print(f"No se pudo extraer datos de la factura para {filename}, se ignora.")
                continue
            
            cuit = data_agent.get("cuit")
            if not cuit:
                print(f"No se pudo extraer CUIT para {filename}, se ignora archivo.")
                continue

            operadores = self._buscar_operador_por_cuit(cuit)
            if not operadores:
                print(f"CUIT {cuit} no encontrado")
                continue

            operadores_ids = [op["id"] for op in operadores]

            invoice_case = InvoiceCases(
                email_id=self.email_id,
                attachment_hash=attachment_hash,
                attachment_name=filename,
                operator_cuit=cuit,
                operator_id=operadores_ids[0] if operadores_ids else None,
                state=FacturasState.RECIBIDO,
                extraction_method="Bedrock"
            )

            invoice_validator = InvoicesValidation(data_agent, operadores, conn_mysql)
            data_agent, needs_retry = invoice_validator.vincular_servicios()

            if needs_retry:
                print(f"Iniciando reintento con agente potente ({MODEL_POWERFUL}) para {filename}")
                data_agent_retry = self.pdf_extractor.extract_invoice_data(file_bytes, model_id=MODEL_POWERFUL)
                
                if data_agent_retry:
                    invoice_validator = InvoicesValidation(data_agent_retry, operadores, conn_mysql)
                    data_agent, needs_retry = invoice_validator.vincular_servicios()

            old_state = invoice_case.state
            state_invoice = (
                FacturasState.EN_REVISION
                if any(not s.get("vinculado") for s in data_agent.get("servicios", []))
                else FacturasState.LISTO_PARA_CARGAR
            )

            invoice_case.state = state_invoice

            invoice_transition_validation = InvoiceTransitions(
                case=invoice_case,
                from_state=old_state,
                to_state=state_invoice,
                reason="Validación de servicios y vinculación.",
                metadata_={"numero_factura": data_agent.get("numero_factura")},
                actor="System/Validator"
            )

            dest_key = self.s3_manager.upload_attachment(filename, file_bytes, content_type)

            invoice_extracted = InvoicesExtractedEmails(
                cuit=cuit,
                ids_operadores=operadores_ids,
                s3_key=dest_key,
                numero_factura=data_agent.get("numero_factura"),
                fecha_factura=data_agent.get("fecha"),
                razon_social=operadores[0]["razon_social"],
                moneda=data_agent.get("moneda"),
                importe_total=data_agent.get("importe_total_final"),
            )

            services = []
            servicios_pdf = data_agent.get("servicios", [])

            for servicio in servicios_pdf:
                service = ServicesExtractedEmails(
                    codigo=self.normalizar_codigo(servicio.get("voucher")),
                    pasajero=servicio.get("nombre_del_viajero"),
                    importe=servicio.get("importe"),
                    vinculado=servicio.get("vinculado"),
                    id_servicio=servicio.get("service_id"),
                    id_reserva=servicio.get("reserve_id"),
                    importe_usd=servicio.get("importeUSD"),
                    ya_facturado=servicio.get("ya_facturado"),
                    factura=servicio.get("factura"),
                    pending=servicio.get("pending"),
                )
                services.append(service)

            invoice_extracted.services = services
            
            attachments_data_for_db.append({
                "filename": filename,
                "s3_key": dest_key,
                "objects": [
                    invoice_case,
                    invoice_transition_validation,
                    invoice_extracted,
                    *services
                ]
            })

        successful_attachments = []
        failed_attachments = []

        with self.db_session() as session:
            for attachment_data in attachments_data_for_db:
                filename = attachment_data["filename"]
                s3_key = attachment_data["s3_key"]
                objects_to_add = attachment_data["objects"]
                
                try:
                    session.add_all(objects_to_add)
                    session.flush()
                    session.commit()
                    successful_attachments.append({"filename": filename, "s3_key": s3_key})
                    print(f"Factura {filename} procesada y guardada exitosamente.")
                except IntegrityError as e:
                    session.rollback()
                    print(f"Error de integridad al guardar factura {filename}: {e}")
                    
                    current_invoice_case = next((obj for obj in objects_to_add if isinstance(obj, InvoiceCases)), None)
                    current_invoice_extracted = next((obj for obj in objects_to_add if isinstance(obj, InvoicesExtractedEmails)), None)

                    current_services = [obj for obj in objects_to_add if isinstance(obj, ServicesExtractedEmails)]
                    current_validation_transition = next((obj for obj in objects_to_add if isinstance(obj, InvoiceTransitions) and obj.from_state is not None), None)

                    if current_invoice_case and current_invoice_extracted:
                        existing_invoice_case_in_db = session.query(InvoiceCases).filter_by(
                            email_id=current_invoice_case.email_id,
                            attachment_hash=current_invoice_case.attachment_hash
                        ).first()
                        
                        if existing_invoice_case_in_db:
                            print(f"Factura {filename} (hash: {current_invoice_case.attachment_hash}) ya existe. Marcando *nueva* factura como DUPLICADO.")
                            
                            original_intended_state = current_invoice_case.state
                            current_invoice_case.state = FacturasState.DUPLICADO
                            
                            duplicate_transition = InvoiceTransitions(
                                case=current_invoice_case,
                                from_state=original_intended_state,
                                to_state=FacturasState.DUPLICADO,
                                reason=f"Factura duplicada detectada (hash: {current_invoice_case.attachment_hash}).",
                                metadata_={"numero_factura": current_invoice_extracted.numero_factura if current_invoice_extracted else "N/A"},
                                actor="System/Extractor"
                            )
                            try:
                                session.add(current_invoice_case)
                                session.add(current_invoice_extracted)
                                session.add_all(current_services)
                                session.add(current_validation_transition)
                                session.add(duplicate_transition)
                                session.commit()
                                successful_attachments.append({"filename": filename, "s3_key": s3_key, "status": "DUPLICADO"})
                                print(f"Nueva factura {filename} marcada como DUPLICADO y guardada.")
                            except Exception as update_e:
                                session.rollback()
                                print(f"Error al guardar la nueva factura {filename} como DUPLICADO y su transición: {update_e}")
                                failed_attachments.append({"filename": filename, "reason": "ERROR_DUPLICADO_SAVE"})
                        else:
                            print(f"Error de integridad no relacionado con duplicado de InvoiceCase para {filename}: {e}")
                            failed_attachments.append({"filename": filename, "reason": "ERROR_INTEGRIDAD"})
                except Exception as e:
                    session.rollback()
                    print(f"Error inesperado al guardar factura {filename}: {e}")
                    failed_attachments.append({"filename": filename, "reason": "ERROR_INESPERADO"})

            final_state = EmailsState.PROCESADO
            if not successful_attachments and not failed_attachments:
                final_state = EmailsState.ADJUNTOS_INVALIDOS
            elif not successful_attachments and failed_attachments:
                final_state = EmailsState.ERROR

            session.query(IncomingEmails).filter(IncomingEmails.message_id == self.msg_id).update({
                IncomingEmails.processing_state: final_state
            })
            
            session.commit()

        return successful_attachments
