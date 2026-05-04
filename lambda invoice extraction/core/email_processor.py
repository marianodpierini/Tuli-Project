import json
import base64
import re

from pdf2image import convert_from_bytes
from datetime import datetime
from io import BytesIO

from database.models import InvoicesExtractedEmails, ServicesExtractedEmails
from core.invoices_validation import InvoicesValidation

CUIT_AERO = "30707362142"
MODEL_DEFAULT = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
MODEL_POWERFUL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"


class EmailProcessor:
    def __init__(
        self, msg, operadores, s3_bucket_destino, s3_client, db_session, bedrock_client
    ):
        self.msg = msg
        self.operadores = operadores
        self.s3_bucket_destino = s3_bucket_destino
        self.s3_client = s3_client
        self.db_session = db_session
        self.bedrock_client = bedrock_client

    def safe_json_load(self, text: str):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 🔥 buscar primer objeto JSON válido
        matches = re.findall(r"\{.*?\}", text, re.DOTALL)

        for match in matches:
            try:
                return json.loads(match)
            except:
                continue

        raise ValueError("No se pudo parsear JSON válido")

    def normalizar_codigo(self, codigo: str) -> str:
        if codigo.startswith("540"):
            return codigo[3:]
        return codigo

    def generate_s3_key(self, filename, now):
        return (
            f"facturas/"
            f"Año={now.year}/"
            f"Mes={now.month:02d}/"
            f"Dia={now.day:02d}/"
            f"{filename}"
        )

    def is_valid_invoice(self, content_type, filename):
        allowed_types = ["application/pdf", "text/xml", "application/xml"]

        allowed_extensions = (".pdf", ".xml")

        return content_type in allowed_types or filename.lower().endswith(
            allowed_extensions
        )

    def buscar_operador_por_cuit(self, cuit):
        for cuit_ops, operadores in self.operadores.items():
            cuit_limpio = cuit_ops.replace("-", "")
            if cuit_limpio == cuit.replace("-", ""):
                return operadores

        return None

    def pdf_a_imagenes_base64(self, file_bytes):
        images = convert_from_bytes(file_bytes, poppler_path="/opt/bin")

        imagenes_base64 = []

        for img in images:
            buffer = BytesIO()
            img.save(buffer, format="PNG")
            imagen_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

            imagenes_base64.append(imagen_base64)

        return imagenes_base64

    def extraer_con_bedrock(self, imagenes_base64, model_id=MODEL_DEFAULT):
        content = []

        for img_b64 in imagenes_base64:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": img_b64,
                    },
                }
            )

        prompt_validacion = """
            Decime si este documento es una FACTURA.

            Responder SOLO:

            {
            "es_factura": true | false
            }
        """

        content.append({"type": "text", "text": prompt_validacion})

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

        text = text.replace("```json", "").replace("```", "").strip()

        if text is False:
            print(f"Documento no es una factura, se ignora archivo")
            return None

        prompt = """
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

        content.append({"type": "text", "text": prompt})

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

        text = text.replace("```json", "").replace("```", "").strip()

        return self.safe_json_load(text)

    def extraer_datos_de_pdf(self, file_bytes, model_id=MODEL_DEFAULT):
        imagenes_base64 = self.pdf_a_imagenes_base64(file_bytes)

        data_agent = self.extraer_con_bedrock(imagenes_base64, model_id=model_id)

        return data_agent

    def process_email(self):
        attachments_saved = []
        data_to_insert = []
        for part in self.msg.iter_attachments():
            filename = part.get_filename()

            if not filename:
                continue

            content_type = part.get_content_type()

            print(f"Encontrado adjunto: {filename} ({content_type})")

            if not self.is_valid_invoice(content_type, filename):
                print(f"Archivo ignorado: {filename}")
                continue

            file_bytes = part.get_payload(decode=True)

            cuit = None

            data_agent = self.extraer_datos_de_pdf(file_bytes)
            if data_agent is None:
                continue
            
            cuit = data_agent.get("cuit")

            if not cuit:
                print("No se pudo extraer CUIT, se ignora archivo")
                continue

            operadores = self.buscar_operador_por_cuit(cuit)

            if not operadores:
                print(f"CUIT {cuit} no encontrado")
                continue

            operadores_ids = [op["id"] for op in operadores]

            invoice_validator = InvoicesValidation(data_agent, operadores)
            data_agent, needs_retry = invoice_validator.vincular_servicios()

            if needs_retry:
                print(f"Iniciando reintento con agente potente ({MODEL_POWERFUL}) para {filename}")
                data_agent_retry = self.extraer_datos_de_pdf(file_bytes, model_id=MODEL_POWERFUL)
                
                if data_agent_retry:
                    invoice_validator = InvoicesValidation(data_agent_retry, operadores)
                    data_agent, _ = invoice_validator.vincular_servicios()

            now = datetime.now()
            dest_key = self.generate_s3_key(filename, now)

            self.s3_client.put_object(
                Bucket=self.s3_bucket_destino,
                Key=dest_key,
                Body=file_bytes,
                ContentType=content_type,
            )

            print(f"Guardado en: {self.s3_bucket_destino}/{dest_key}")

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
