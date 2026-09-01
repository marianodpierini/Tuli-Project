import json
import base64
import re
import hashlib
import time
import unicodedata
from typing import Dict, Any, List, Optional, Tuple
from enum import Enum

from pdf2image import convert_from_bytes
from datetime import datetime, timezone, date
from io import BytesIO

from database.models import (
    InvoicesExtractedEmails,
    ServicesExtractedEmails,
    IncomingEmails,
    InvoiceCases,
    InvoiceTransitions,
    PercepcionesIIBB,
)
from core.invoices_validation import InvoicesValidation
from database.db_mysql import get_connection

from sqlalchemy.exc import IntegrityError

MODEL_DEFAULT = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
MODEL_POWERFUL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

TOLERANCIA = 0.05


class EmailsState(str, Enum):
    RECIBIDO = "RECIBIDO"
    SIN_ADJUNTO = "SIN_ADJUNTO"
    ADJUNTOS_INVALIDOS = "ADJUNTO_INVALIDO"
    PROCESADO = "PROCESADO"
    ERROR = "ERROR"
    SIN_OPERADOR_ASOCIADO = "SIN_OPERADOR_ASOCIADO"


class FacturasState(str, Enum):
    RECIBIDO = "RECIBIDO"
    LISTO_PARA_CARGAR = "LISTO_PARA_CARGAR"
    LOADED_BY_IT = "LOADED_BY_IT"
    LOAD_FAILED = "LOAD_FAILED"
    DUPLICADO = "DUPLICADO"
    DESCARTADO = "DESCARTADO"
    EN_REVISION = "EN_REVISION"
    RECHAZADA = "RECHAZADA"
    ERROR = "ERROR"
    YA_FACTURADO = "YA_FACTURADO"


class StateReason(str, Enum):
    CUIT_NO_IDENTIFICADO = "CUIT_NO_IDENTIFICADO"
    CUIT_DUDOSO = "CUIT_DUDOSO"
    DISTRIBUCION_A_CONFIRMAR = "DISTRIBUCION_A_CONFIRMAR"
    DESGLOCE_NO_CUADRA = "DESGLOCE_NO_CUADRA"
    REMITENTE_NO_COINCIDE = "REMITENTE_NO_COINCIDE"
    EXTENSION_INVALIDA = "EXTENSION_INVALIDA"
    FACTURA_DUPLICADA = "FACTURA_DUPLICADA"


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

    def __init__(self, bedrock_client, logger, json_parser: JsonParser):
        self.bedrock_client = bedrock_client
        self.json_parser = json_parser
        self.logger = logger

    def _pdf_to_base64_images(self, file_bytes: bytes) -> List[str]:
        """Converts PDF bytes to a list of base64 encoded PNG images."""
        images = convert_from_bytes(file_bytes, poppler_path="/opt/bin")
        base64_images = []
        for img in images:
            buffer = BytesIO()
            img.save(buffer, format="PNG")
            base64_images.append(base64.b64encode(buffer.getvalue()).decode("utf-8"))
        return base64_images

    def _invoke_bedrock_model(
        self, content: List[Dict[str, Any]], model_id: str
    ) -> Tuple[str, int]:
        """Invokes the Bedrock model with the given content and model ID."""
        response = self.bedrock_client.invoke_model(
            modelId=model_id,
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 5000,
                    "temperature": 0,
                    "messages": [{"role": "user", "content": content}],
                }
            ),
        )
        response_body = json.loads(response["body"].read())
        text = response_body["content"][0]["text"]
        usage = response_body.get("usage", {})
        tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        cleaned_text = text.replace("```json", "").replace("```", "").strip()
        return cleaned_text, tokens

    def extract_invoice_data(
        self, file_bytes: bytes, model_id: str = MODEL_DEFAULT
    ) -> Tuple[Optional[Dict[str, Any]], int]:
        """
        Extracts invoice data from PDF bytes using Bedrock.
        Performs a two-step process: validation (is it an invoice?) and then data extraction.
        """
        total_tokens = 0
        images_base64 = self._pdf_to_base64_images(file_bytes)
        if not images_base64:
            self.logger.info("No images extracted from PDF.")
            return None, 0

        validation_prompt = """
            Decime si este documento es una FACTURA.
            Responder SOLO:
            {
            "es_factura": true | false
            }
        """
        validation_content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": img_b64,
                },
            }
            for img_b64 in images_base64
        ]
        validation_content.append({"type": "text", "text": validation_prompt})
        validation_response_text, tokens_val = self._invoke_bedrock_model(
            validation_content, model_id
        )
        total_tokens += tokens_val
        validation_data = self.json_parser.safe_json_load(validation_response_text)

        if not validation_data or not validation_data.get("es_factura"):
            self.logger.info(
                f"Documento no es una factura, se ignora archivo. Validation response: {validation_response_text}"
            )
            return None, total_tokens

        extraction_prompt = """
            Analiza esta factura y devuelve SOLO un JSON valido, sin texto adicional.

            Devuelve exactamente estas claves (snake_case):
            {
                "cuit": "",
                "numero_factura": "",
                "fecha": "YYYY-MM-DD",
                "moneda": "",
                "importe_total_final": 0.0,
                "tipo_comprobante": "factura|nota de debito|nota de credito",
                "tipo_factura": "factura A|factura B|factura C|null",
                "cotizacion": 0.0,
                "subtotal": 0.0,
                "descuento": 0.0,
                "total_sin_iva": 0.0,
                "iva_21": 0.0,
                "percepcion_iibb_texto": "",
                "percepcion_iibb": 0.0,
                "no_computable": 0.0,
                "gravado_21": 0.0,
                "gravado_105": 0.0,
                "percepcion_iva": 0.0,
                "servicios": [
                    {
                        "voucher": "",
                        "producto": "",
                        "nombre_del_viajero": "",
                        "desc": 0.0,
                        "importe": 0.0
                    }
                ]
            }

            Reglas de extraccion:
            - cuit: tomar CUIT del emisor/proveedor de la factura (ejemplo: "C.U.I.T. N°: 33-54799242-9").
            - Nunca usar el CUIT del cliente/comprador/receptor (por ejemplo AERO 30-70736214-2).
            - numero_factura: formato punto de venta-numero (ejemplo 0080-00322758).
            - fecha: usar la fecha de emision de factura, no vencimiento.
            - moneda: ARS o USD segun simbolo/leyenda.
            - cotizacion: si la moneda es distinta de ARS, tomar el tipo de cambio del texto
              "A efectos contables e impositivos el tipo de cambio...". Si no existe, usar 0.
            - percepcion_iibb_texto: devolver el texto exacto del concepto de percepcion IIBB
              (ejemplo: "Percepcion IIBB BSAS"). Si no aparece, devolver "".
            - Valores numericos: usar punto decimal, sin simbolos de moneda ni separadores de miles.
            - Si un campo numerico no aparece, usar 0.

            Reglas de tabla de servicios:
            - Mantener el orden visual de filas.
            - Cada fila del array servicios debe salir de una unica fila horizontal de la tabla.
            - No mover importes/descuentos de una fila a otra.
            - No usar totales del pie (subtotal/total) como importe de una fila.
            - Si una fila tiene pasajero pero falta importe o desc en esa misma fila, usar 0 en el faltante.

            Validaciones internas antes de responder:
            - subtotal debe ser igual a la suma de servicios[].importe (tolerancia pequena por redondeo).
            - total_sin_iva debe ser igual a la suma de servicios[].desc (tolerancia pequena por redondeo).
            - importe_total_final debe coincidir con el total final informado.
        """
        extraction_content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": img_b64,
                },
            }
            for img_b64 in images_base64
        ]
        extraction_content.append({"type": "text", "text": extraction_prompt})
        extraction_response_text, tokens_ext = self._invoke_bedrock_model(
            extraction_content, model_id
        )
        total_tokens += tokens_ext
        return self.json_parser.safe_json_load(extraction_response_text), total_tokens


class S3AttachmentManager:
    """Handles S3 operations for attachments."""

    def __init__(self, s3_client, s3_bucket_destino: str, msg_id: str, logger):
        self.s3_client = s3_client
        self.s3_bucket_destino = s3_bucket_destino
        self.msg_id = msg_id
        self.logger = logger

    def generate_s3_key(self, filename: str, now: datetime) -> str:
        """Generates a unique S3 key for the attachment."""
        return (
            f"facturas/"
            f"fecha={now.year}-{now.month:02d}-{now.day:02d}/"
            f"{self.msg_id}-{filename}"
        )

    def is_valid_invoice_attachment(self, content_type: str, filename: str) -> bool:
        """Checks if the attachment is a valid invoice type."""
        allowed_types = ["application/pdf", "text/xml", "application/xml"]
        allowed_extensions = (".pdf", ".xml")
        return content_type in allowed_types or filename.lower().endswith(
            allowed_extensions
        )

    def upload_attachment(
        self, filename: str, file_bytes: bytes, content_type: str
    ) -> str:
        """Uploads the attachment to S3 and returns its key."""
        now = datetime.now()
        dest_key = self.generate_s3_key(filename, now)
        self.s3_client.put_object(
            Bucket=self.s3_bucket_destino,
            Key=dest_key,
            Body=file_bytes,
            ContentType=content_type,
        )
        self.logger.info(f"Guardado en: {self.s3_bucket_destino}/{dest_key}")
        return dest_key


class EmailProcessor:
    def __init__(
        self,
        msg,
        operadores,
        s3_bucket_destino,
        s3_client,
        db_session,
        bedrock_client,
        msg_id,
        logger,
    ):
        self.msg = msg
        self.operadores = operadores
        self.db_session = db_session
        self.msg_id = msg_id
        self.email_id = None
        self.json_parser = JsonParser()
        self.pdf_extractor = PdfBedrockExtractor(bedrock_client, logger, self.json_parser)
        self.s3_manager = S3AttachmentManager(
            s3_client, s3_bucket_destino, msg_id, logger
        )
        self.logger = logger

    def get_date(self, invoice_date) -> str:
        if isinstance(invoice_date, datetime) or isinstance(invoice_date, date):
            return invoice_date.isoformat()
        if isinstance(invoice_date, str):
            return invoice_date

    def _normalize_text_encoding(self, value: Optional[str]) -> Optional[str]:
        """Normaliza Unicode y corrige mojibake UTF-8/latin-1 cuando aplica."""
        if value is None:
            return None

        if not isinstance(value, str):
            value = str(value)

        normalized = unicodedata.normalize("NFC", value)

        if any(marker in normalized for marker in ("Ã", "Â", "Ð", "�")):
            try:
                repaired = normalized.encode("latin-1").decode("utf-8")
                normalized = unicodedata.normalize("NFC", repaired)
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass

        return normalized.strip()

    def validar_desglose(self, factura) -> tuple[bool, str | None]:
        def _to_float(value: Any, default: float = 0.0) -> float:
            if value is None:
                return default
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        servicios = factura.get("servicios", [])
        suma_importe = sum(_to_float(s.get("importe")) for s in servicios)
        suma_desc = sum(_to_float(s.get("desc")) for s in servicios)

        subtotal = _to_float(factura.get("subtotal"))
        total_sin_iva = _to_float(factura.get("total_sin_iva"))
        percepcion_iibb = _to_float(factura.get("percepcion_iibb"))
        percepcion_iva = _to_float(factura.get("percepcion_iva"))
        importe_total_final = _to_float(factura.get("importe_total_final"))

        checks = [
            (
                "SUBTOTAL_NO_CUADRA",
                abs(suma_importe - subtotal) > TOLERANCIA,
            ),
            (
                "TOTAL_SIN_IVA_NO_CUADRA",
                abs(suma_desc - total_sin_iva) > TOLERANCIA,
            ),
            (
                "TOTAL_NO_CUADRA",
                abs(
                    total_sin_iva
                    + percepcion_iibb
                    + percepcion_iva
                    - importe_total_final
                )
                > TOLERANCIA,
            ),
        ]

        for motivo, falla in checks:
            if falla:
                return False, motivo
        return True, None

    def _buscar_operador_por_cuit(self, cuit: str) -> Optional[List[Dict[str, Any]]]:
        """Busca operadores por CUIT."""
        for cuit_ops, operadores in self.operadores["operadores_by_cuit"].items():
            cuit_limpio = cuit_ops.replace("-", "")
            if cuit_limpio == cuit.replace("-", ""):
                return operadores

        for cuit_ops, operadores in self.operadores["operadores_by_cuit"].items():
            if cuit_ops.split("-")[1] == cuit.split("-")[1]:
                self.logger.info(
                    f"Coincidencia parcial de CUIT encontrada: {cuit_ops} para CUIT {cuit}"
                )
                return None

        return None

    def _buscar_operador_por_sender(self, sender: str) -> Optional[str]:
        """Busca operadores por dirección de correo del remitente."""
        cuit = self.operadores.get("cuit_by_sender", {}).get(sender)
        if cuit:
            self.logger.info(f"CUIT {cuit} encontrado para sender {sender}")
            return cuit
        self.logger.info(
            f"No se encontró CUIT para sender {sender}, se buscara por contenido de la factura."
        )
        return None

    def insert_email(self):
        """Inserta el registro inicial del correo en la tabla incoming_emails."""
        attachments = list(self.msg.iter_attachments())
        attachment_count = len(attachments)
        sender = str(self.msg.get("From", "Desconocido"))

        state = EmailsState.RECIBIDO
        reason = None

        cuit_by_sender = self._buscar_operador_por_sender(sender)

        if attachment_count == 0:
            state = EmailsState.SIN_ADJUNTO
            reason = "El correo no contiene archivos adjuntos."

        email_record = IncomingEmails(
            message_id=self.msg_id,
            received_at=datetime.now(timezone.utc),
            sender=sender,
            subject=str(self.msg.get("Subject", "Sin Asunto")),
            has_attachments=attachment_count > 0,
            attachment_count=attachment_count,
            processing_state=state,
            processing_reason=reason,
        )
        self.email_id = email_record
        with self.db_session() as session:
            session.add(email_record)
            session.commit()

        return cuit_by_sender

    def process_email(self):
        start_time = time.perf_counter()
        total_tokens_email = 0

        try:
            cuit_by_sender = self.insert_email()

            if not cuit_by_sender:
                self.logger.info(
                    f"No se encontró CUIT asociado al sender {self.msg.get('From')}."
                )
                invoice_case = InvoiceCases(
                    email=self.email_id,
                    attachment_hash=None,
                    attachment_name=None,
                    operator_cuit=None,
                    operator_id=None,
                    state=FacturasState.EN_REVISION,
                    state_reason=StateReason.REMITENTE_NO_COINCIDE,
                    extraction_method="Bedrock",
                )

                with self.db_session() as session:
                    session.add(invoice_case)
                    session.commit()

                return 0, 0

            attachments_data_for_db = []
        except Exception as e:
            self.logger.error(
                f"Error inesperado durante la inserción del correo {self.msg_id}: {e}"
            )
            return 0, 0

        for part in self.msg.iter_attachments():
            try:
                filename = part.get_filename()
                if not filename:
                    continue

                content_type = part.get_content_type()
                self.logger.info(f"Encontrado adjunto: {filename} ({content_type})")

                file_bytes = part.get_payload(decode=True)
                attachment_hash = hashlib.sha256(file_bytes).hexdigest()
                
                dest_key = self.s3_manager.upload_attachment(
                    filename, file_bytes, content_type
                )

                if not self.s3_manager.is_valid_invoice_attachment(
                    content_type, filename
                ):
                    self.logger.info(
                        f"Archivo ignorado por tipo/extensión inválida: {filename}"
                    )

                    invoice_case = InvoiceCases(
                        email=self.email_id,
                        attachment_hash=attachment_hash,
                        attachment_name=filename,
                        operator_cuit=cuit if cuit else None,
                        operator_id=None,
                        state=FacturasState.EN_REVISION,
                        state_reason=StateReason.EXTENSION_INVALIDA,
                        extraction_method="Bedrock",
                    )
                    with self.db_session() as session:
                        session.add(invoice_case)
                        session.commit()
                    continue

                data_agent, tokens = self.pdf_extractor.extract_invoice_data(file_bytes)
                total_tokens_email += tokens
                if data_agent is None:
                    self.logger.info(
                        f"No se pudo extraer datos de la factura para {filename}, se ignora."
                    )
                    continue

                cuit = cuit_by_sender or data_agent.get("cuit")
                if not cuit:
                    self.logger.info(
                        f"No se pudo extraer CUIT para {filename}, se ignora archivo."
                    )
                    invoice_case = InvoiceCases(
                        email=self.email_id,
                        attachment_hash=attachment_hash,
                        attachment_name=filename,
                        operator_cuit=cuit if cuit else None,
                        operator_id=None,
                        state=FacturasState.EN_REVISION,
                        state_reason=StateReason.CUIT_NO_IDENTIFICADO,
                        extraction_method="Bedrock",
                    )
                    with self.db_session() as session:
                        session.add(invoice_case)
                        session.commit()
                    continue

                if cuit_by_sender != data_agent.get("cuit"):
                    self.logger.warning(
                        f"CUIT extraído {data_agent.get('cuit')} no coincide con CUIT asociado al sender {cuit_by_sender} para {filename}."
                    )

                operadores = self._buscar_operador_por_cuit(cuit)
                if not operadores:
                    self.logger.info(
                        f"No se pudo extraer operadores para este CUIT {cuit}"
                    )
                    invoice_case = InvoiceCases(
                        email=self.email_id,
                        attachment_hash=attachment_hash,
                        attachment_name=filename,
                        operator_cuit=cuit if cuit else None,
                        operator_id=None,
                        state=FacturasState.EN_REVISION,
                        state_reason=StateReason.CUIT_DUDOSO,
                        extraction_method="Bedrock",
                    )
                    with self.db_session() as session:
                        session.add(invoice_case)
                        session.commit()
                    continue

                operadores_ids = [op["id"] for op in operadores]

                invoice_case = InvoiceCases(
                    email=self.email_id,
                    attachment_hash=attachment_hash,
                    attachment_name=filename,
                    operator_cuit=cuit,
                    operator_id=operadores_ids[0] if operadores_ids else None,
                    state=FacturasState.RECIBIDO,
                    extraction_method="Bedrock",
                )

                conn_mysql = get_connection()
                conn_mysql.ping(reconnect=True)
                try:
                    invoice_validator = InvoicesValidation(
                        data_agent, operadores, conn_mysql, self.logger
                    )
                    data_agent, needs_retry = invoice_validator.vincular_servicios()

                    if needs_retry:
                        self.logger.info(
                            f"Iniciando reintento con agente potente ({MODEL_POWERFUL}) para {filename}"
                        )
                        data_agent_retry, tokens_retry = (
                            self.pdf_extractor.extract_invoice_data(
                                file_bytes, model_id=MODEL_POWERFUL
                            )
                        )
                        total_tokens_email += tokens_retry

                        if data_agent_retry:
                            invoice_validator = InvoicesValidation(
                                data_agent_retry, operadores, conn_mysql, self.logger
                            )
                            data_agent, needs_retry = invoice_validator.vincular_servicios()
                finally:
                    try:
                        conn_mysql.close()
                    except Exception:
                        pass

                old_state = invoice_case.state
                servicios = data_agent.get("servicios", [])
                if any(not s.get("vinculado") for s in servicios):
                    state_invoice = FacturasState.EN_REVISION
                elif servicios and all(s.get("ya_facturado") for s in servicios):
                    state_invoice = FacturasState.YA_FACTURADO
                else:
                    state_invoice = FacturasState.LISTO_PARA_CARGAR

                if self.get_date(data_agent.get("fecha")) > date.today().isoformat():
                    state_invoice = FacturasState.EN_REVISION

                if self.validar_desglose(data_agent)[0] is False:
                    state_invoice = FacturasState.EN_REVISION
                    invoice_case.state_reason = StateReason.DESGLOCE_NO_CUADRA

                invoice_case.state = state_invoice

                invoice_transition_validation = InvoiceTransitions(
                    case=invoice_case,
                    from_state=old_state,
                    to_state=state_invoice,
                    reason="Validación de servicios y vinculación.",
                    metadata_={"numero_factura": data_agent.get("numero_factura")},
                    actor="System/Validator",
                )

                tipo_factura = "FA" if data_agent.get("tipo_factura") == "factura A" else (
                    "FB" if data_agent.get("tipo_factura") == "factura B" else (
                        "FC" if data_agent.get("tipo_factura") == "factura C" else None
                    )
                )

                invoice_extracted = InvoicesExtractedEmails(
                    cuit=cuit,
                    ids_operadores=operadores_ids,
                    s3_key=dest_key,
                    numero_factura=data_agent.get("numero_factura"),
                    fecha_factura=data_agent.get("fecha"),
                    razon_social=operadores[0]["razon_social"],
                    moneda=data_agent.get("moneda"),
                    importe_total=data_agent.get("importe_total_final"),
                    tipo_comprobante=data_agent.get("tipo_comprobante"),
                    punto_venta=data_agent.get("numero_factura").split("-")[0],
                    numero_comprobante=data_agent.get("numero_factura").split("-")[1],
                    cotizacion=data_agent.get("cotizacion"),
                    exento=data_agent.get("total_sin_iva"),
                    no_computable=data_agent.get("no_computable"),
                    gravado_21=data_agent.get("gravado_21"),
                    gravado_105=data_agent.get("gravado_105"),
                    percepcion_iva=data_agent.get("percepcion_iva"),
                    subtotal_control=data_agent.get("subtotal"),
                    descuento_control=data_agent.get("descuento"),
                    total_sin_iva_control=data_agent.get("total_sin_iva"),
                    total_control=data_agent.get("importe_total_final"),
                    voucher=tipo_factura,
                )

                services = []
                servicios_pdf = data_agent.get("servicios", [])

                for servicio in servicios_pdf:
                    service = ServicesExtractedEmails(
                        codigo=servicio.get("voucher"),
                        pasajero=self._normalize_text_encoding(
                            servicio.get("nombre_del_viajero")
                        ),
                        importe=servicio.get("importe"),
                        vinculado=servicio.get("vinculado"),
                        id_servicio=servicio.get("service_id"),
                        id_reserva_aptour=servicio.get("reserve_id"),
                        id_reserva_mo=servicio.get("id_reserva_mo"),
                        importe_usd=servicio.get("importeUSD"),
                        ya_facturado=servicio.get("ya_facturado"),
                        factura=servicio.get("factura"),
                        pending=servicio.get("pending"),
                        desc_neto=servicio.get("desc"),
                        id_operador=servicio.get("operator_id"),
                    )
                    services.append(service)

                invoice_extracted.services = services
                invoice_extracted.case = invoice_case

                id_provincia = None
                percepcion_texto = (data_agent.get("percepcion_iibb_texto") or "").strip()
                percepciones_config = (
                    operadores[0].get("percepciones_config", {}) if operadores else {}
                )
                if percepcion_texto and percepciones_config:
                    texto_norm = percepcion_texto.casefold()
                    for key, value in percepciones_config.items():
                        if texto_norm == str(key).strip().casefold():
                            id_provincia = value
                            break

                percepcion_record = PercepcionesIIBB(
                    invoice=invoice_extracted,
                    provincia="Buenos Aires",
                    monto=data_agent.get("percepcion_iibb"),
                    id_provincia=id_provincia,
                )

                attachments_data_for_db.append(
                    {
                        "filename": filename,
                        "s3_key": dest_key,
                        "objects": [
                            invoice_case,
                            invoice_transition_validation,
                            invoice_extracted,
                            percepcion_record,
                            *services,
                        ],
                    }
                )
            except Exception as e:
                self.logger.error(
                    f"Error inesperado durante el procesamiento del correo {self.msg_id}: {e}"
                )
                error_reason = f"{type(e).__name__}: {e}"
                invoice_case = InvoiceCases(
                    email=self.email_id,
                    attachment_hash=attachment_hash if attachment_hash else None,
                    attachment_name=filename if filename else None,
                    operator_cuit=cuit if cuit else None,
                    operator_id=operadores_ids[0] if operadores_ids else None,
                    state=FacturasState.ERROR,
                    state_reason=error_reason,
                    extraction_method="Bedrock",
                )
                
                with self.db_session() as session:
                    session.add(invoice_case)
                    session.commit()

                continue

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
                    successful_attachments.append(
                        {"filename": filename, "s3_key": s3_key}
                    )
                    self.logger.info(
                        f"Factura {filename} procesada y guardada exitosamente."
                    )
                except IntegrityError as e:
                    session.rollback()
                    self.logger.error(
                        f"Error de integridad al guardar la factura {filename}: {e}"
                    )

                    error_message = str(getattr(e, "orig", e))
                    is_invoice_duplicate = (
                        "_invoice_unique_constraint_" in error_message
                        or (
                            "invoices_extracted_emails" in error_message
                            and "cuit" in error_message
                            and "numero_factura" in error_message
                            and "tipo_comprobante" in error_message
                        )
                    )

                    if is_invoice_duplicate:
                        original_case = next(
                            (
                                obj
                                for obj in objects_to_add
                                if isinstance(obj, InvoiceCases)
                            ),
                            None,
                        )

                        duplicate_case = InvoiceCases(
                            email=self.email_id,
                            attachment_hash=(
                                original_case.attachment_hash if original_case else None
                            ),
                            attachment_name=(
                                original_case.attachment_name if original_case else filename
                            ),
                            operator_cuit=(
                                original_case.operator_cuit if original_case else None
                            ),
                            operator_id=(
                                original_case.operator_id if original_case else None
                            ),
                            state=FacturasState.DUPLICADO,
                            state_reason=StateReason.FACTURA_DUPLICADA,
                            extraction_method=(
                                original_case.extraction_method
                                if original_case
                                else "Bedrock"
                            ),
                        )

                        duplicate_transition = InvoiceTransitions(
                            case=duplicate_case,
                            from_state=(
                                original_case.state if original_case else FacturasState.RECIBIDO
                            ),
                            to_state=FacturasState.DUPLICADO,
                            reason=StateReason.FACTURA_DUPLICADA,
                            actor="System/DB",
                        )

                        try:
                            session.add_all([duplicate_case, duplicate_transition])
                            session.commit()
                            self.logger.info(
                                f"Factura {filename} registrada como DUPLICADO."
                            )
                        except IntegrityError as duplicate_error:
                            session.rollback()
                            self.logger.error(
                                "No se pudo registrar el caso duplicado para "
                                f"{filename}: {duplicate_error}"
                            )

            final_state = EmailsState.PROCESADO
            if not successful_attachments and not failed_attachments:
                final_state = EmailsState.ADJUNTOS_INVALIDOS
            elif not successful_attachments and failed_attachments:
                final_state = EmailsState.ERROR

            processing_time_ms = int((time.perf_counter() - start_time) * 1000)
            self.logger.info(f"Procesamiento finalizado para {self.msg_id}. Tiempo: {processing_time_ms}ms, Tokens: {total_tokens_email}")

            session.query(IncomingEmails).filter(IncomingEmails.message_id == self.msg_id).update({
                IncomingEmails.processing_state: final_state
            })
            
            session.commit()
                    

        return processing_time_ms, total_tokens_email
