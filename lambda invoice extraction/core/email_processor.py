import json
import base64
import re
from typing import Dict, Any, List, Optional

from pdf2image import convert_from_bytes
from datetime import datetime
from io import BytesIO

from database.models import InvoicesExtractedEmails, ServicesExtractedEmails
from core.invoices_validation import InvoicesValidation
from database.db_mysql import get_connection

MODEL_DEFAULT = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
MODEL_POWERFUL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

conn_mysql = get_connection()


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
            - cuit (del proveedor, NO de Aero)
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
            f"{filename}-{self.msg_id}"
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


    def process_email(self):
        attachments_saved = []
        data_to_insert = []
        for part in self.msg.iter_attachments():
            filename = part.get_filename()
            if not filename:
                continue

            content_type = part.get_content_type()
            print(f"Encontrado adjunto: {filename} ({content_type})")

            if not self.s3_manager.is_valid_invoice_attachment(content_type, filename):
                print(f"Archivo ignorado: {filename}")
                continue

            file_bytes = part.get_payload(decode=True)

            data_agent = self.pdf_extractor.extract_invoice_data(file_bytes)
            if data_agent is None:
                continue
            
            cuit = data_agent.get("cuit")
            if not cuit:
                print("No se pudo extraer CUIT, se ignora archivo")
                continue

            operadores = self._buscar_operador_por_cuit(cuit)
            if not operadores:
                print(f"CUIT {cuit} no encontrado")
                continue

            operadores_ids = [op["id"] for op in operadores]

            invoice_validator = InvoicesValidation(data_agent, operadores, conn_mysql)
            data_agent, needs_retry = invoice_validator.vincular_servicios()

            if needs_retry:
                print(f"Iniciando reintento con agente potente ({MODEL_POWERFUL}) para {filename}")
                data_agent_retry = self.pdf_extractor.extract_invoice_data(file_bytes, model_id=MODEL_POWERFUL)
                
                if data_agent_retry:
                    invoice_validator = InvoicesValidation(data_agent_retry, operadores, conn_mysql)
                    data_agent, _ = invoice_validator.vincular_servicios()

            dest_key = self.s3_manager.upload_attachment(filename, file_bytes, content_type)

            invoice = InvoicesExtractedEmails(
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

            invoice.services = services
            data_to_insert.append(invoice)

            attachments_saved.append({"filename": filename, "s3_key": dest_key})

        with self.db_session() as session:
            session.add_all(data_to_insert)
            session.commit()

        return attachments_saved
