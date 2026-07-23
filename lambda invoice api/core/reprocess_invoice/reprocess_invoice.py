import json
import boto3
import base64
import re

from typing import Dict, Any, Tuple, Optional, List

from pdf2image import convert_from_bytes
from io import BytesIO

from database.models import (
    InvoicesExtractedEmails
)
from database.db_mysql import get_connection

from .invoices_validation import InvoicesValidation

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


class ReprocessInvoice:
    def __init__(self, invoice_id, s3_cliente, logger, SessionLocal, s3_bucket, key_operadores):
        self.invoice_id = invoice_id
        self.s3_client = s3_cliente
        self.logger = logger
        self.db_session = SessionLocal
        self.s3_bucket = s3_bucket
        self.key_operadores = key_operadores
        self.json_parser = JsonParser()
        self.bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")

    def load_operators(self) -> Dict[str, Any]:
        self.logger.info(f"Cargando operadores desde s3://{self.s3_bucket}/{self.key_operadores}")
        response = self.s3_client.get_object(Bucket=self.s3_bucket, Key=self.key_operadores)
        return json.loads(response["Body"].read())

    def get_pdf_invoice(self):
        with self.db_session() as session:
            query_get_invoice = session.query(InvoicesExtractedEmails).filter_by(id=self.invoice_id)
            invoice = query_get_invoice.first()

            if not invoice:
                return {
                    "statusCode": 404,
                    "body": json.dumps({"error": "Factura no encontrada"}),
                }
            
        return invoice.s3_key
    def _pdf_to_base64_images(self, file_bytes: bytes) -> List[str]:
        """Converts PDF bytes to a list of base64 encoded PNG images."""
        images = convert_from_bytes(file_bytes, poppler_path="/opt/bin")
        base64_images = []
        for img in images:
            buffer = BytesIO()
            img.save(buffer, format="PNG")
            base64_images.append(base64.b64encode(buffer.getvalue()).decode("utf-8"))
        return base64_images
    
    def _buscar_operador_por_cuit(self, cuit: str, operadores: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """Busca operadores por CUIT."""
        for cuit_ops, operadores in operadores["operadores_by_cuit"].items():
            cuit_limpio = cuit_ops.replace("-", "")
            if cuit_limpio == cuit.replace("-", ""):
                return operadores
            
            if cuit_ops.split("-")[1] == cuit.split("-")[1]:
                self.logger.info(f"Coincidencia parcial de CUIT encontrada: {cuit_ops} para CUIT {cuit}")
                return operadores
            
        return None
    
    def _invoke_bedrock_model(self, content: List[Dict[str, Any]], model_id: str) -> Tuple[str, int]:
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
        usage = response_body.get("usage", {})
        cleaned_text = text.replace("```json", "").replace("```", "").strip()
        return cleaned_text
    
    def extract_invoice_data(self, file_bytes: bytes, model_id: str) -> Tuple[Optional[Dict[str, Any]], int]:
        """
        Extracts invoice data from PDF bytes using Bedrock.
        Performs a two-step process: validation (is it an invoice?) and then data extraction.
        """
        images_base64 = self._pdf_to_base64_images(file_bytes)
        if not images_base64:
            self.logger.info("No images extracted from PDF.")
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
            self.logger.info(f"Documento no es una factura, se ignora archivo. Validation response: {validation_response_text}")
            return None

        extraction_prompt = """
            Analizá esta factura.
            Extraé:
            - cuit Ejemplo: C.U.I.T. Nº : 33-54799242-9 (excluir cuit AERO 30-70736214-2)
            - numero_factura
            - fecha (YYYY-MM-DD)
            - moneda
            - importe_total_final
            - tipo_comprobante (factura / nota de débito / nota de crédito)
            - cotizacion (si la moneda es distinta a ARS) (El texto donde aparece es este 'A efectos contables e impositivos el tipo de cambio de esta factura es  $ 1385')
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
    
    def map_data(self, data_agent: Dict[str, Any]) -> Dict[str, Any]:
        """
        Maps the extracted data to the database model fields.
        """
        services = []
        mapped_data = {
            "cuit": data_agent.get("cuit"),
            "numero_factura": data_agent.get("numero_factura"),
            "fecha_factura": data_agent.get("fecha"),
            "moneda": data_agent.get("moneda"),
            "importe_total": data_agent.get("importe_total_final"),
            "tipo_comprobante": data_agent.get("tipo_comprobante"),
            "cotizacion": data_agent.get("cotizacion"),
            "punto_venta": data_agent.get("numero_factura").split("-")[0],
            "numero_comprobante": data_agent.get("numero_factura").split("-")[1],
        }

        for service in data_agent.get("servicios", []):
            service_mapped = {
                "codigo": service.get("voucher"),
                "pasajero": service.get("nombre_del_viajero"),
                "importe": service.get("importe"),
                "vinculado": service.get("vinculado"),
                "id_servicio": service.get("service_id"),
                "id_reserva_aptour": service.get("reserve_id"),
                "id_reserva_mo": service.get("id_reserva_mo"),
                "importe_usd": service.get("importeUSD"),
                "ya_facturado": service.get("ya_facturado"),
                "factura": service.get("factura"),
                "pending": service.get("pending"),
                "id_operador": service.get("operator_id")
            }

            services.append(service_mapped)

        mapped_data["services"] = services

        return mapped_data
    
    def _update_invoice(self, invoice, mapped_data):
        invoice_fields = [
            "cuit",
            "numero_factura",
            "fecha_factura",
            "moneda",
            "importe_total",
            "tipo_comprobante",
            "cotizacion",
            "punto_venta",
            "numero_comprobante",
        ]

        changes = []

        for field in invoice_fields:
            new_value = mapped_data.get(field)
            old_value = getattr(invoice, field)

            if old_value != new_value:
                setattr(invoice, field, new_value)

                changes.append(
                    {
                        "field": field,
                        "old": old_value,
                        "new": new_value,
                    }
                )

        return changes


    def _update_services(self, invoice, mapped_data):
        service_fields = [
            "importe",
            "vinculado",
            "id_servicio",
            "id_reserva_aptour",
            "id_reserva_mo",
            "id_operador",
            "importe_usd",
            "ya_facturado",
            "factura",
            "pending",
        ]

        changes = []

        existing_services = {
            (
                service.codigo,
                service.pasajero,
            ): service
            for service in invoice.services
        }

        for service_data in mapped_data.get("services", []):

            key = (
                service_data.get("codigo"),
                service_data.get("pasajero"),
            )

            existing_service = existing_services.get(key)

            if not existing_service:
                self.logger.warning(
                    f"Servicio no encontrado para factura {invoice.id}: {key}"
                )
                continue

            for field in service_fields:
                new_value = service_data.get(field)
                old_value = getattr(existing_service, field)

                if old_value != new_value:
                    setattr(existing_service, field, new_value)

                    changes.append(
                        {
                            "service": key,
                            "field": field,
                            "old": old_value,
                            "new": new_value,
                        }
                    )

        return changes


    def reprocess(self):
        s3_key = self.get_pdf_invoice()

        response = self.s3_client.get_object(Bucket=self.s3_bucket, Key=s3_key)
        file_bytes = response["Body"].read()
        operators_file = self.load_operators()

        data_agent = self.extract_invoice_data(file_bytes, model_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0")

        operadores = self._buscar_operador_por_cuit(data_agent.get("cuit"), operators_file)

        invoice_validator = InvoicesValidation(data_agent, operadores, conn_mysql, self.logger)
        data_agent = invoice_validator.vincular_servicios()
        mapped_data = self.map_data(data_agent)
        
        with self.db_session() as session:

            invoice = (
                session.query(InvoicesExtractedEmails)
                .filter_by(id=self.invoice_id)
                .first()
            )

            if not invoice:
                return {
                    "statusCode": 404,
                    "body": json.dumps(
                        {"error": "Factura no encontrada"}
                    ),
                }

            invoice_changes = self._update_invoice(
                invoice,
                mapped_data,
            )

            service_changes = self._update_services(
                invoice,
                mapped_data,
            )

            if invoice_changes or service_changes:

                self.logger.info(
                    f"Factura {invoice.id} actualizada."
                )

                self.logger.info(
                    f"Cambios factura: {invoice_changes}"
                )

                self.logger.info(
                    f"Cambios servicios: {service_changes}"
                )

                session.commit()

            else:
                self.logger.info(
                    f"Factura {invoice.id} sin cambios."
                )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": f"Invoice {self.invoice_id} reprocessed successfully.",
                    "invoice_changes": len(invoice_changes),
                    "service_changes": len(service_changes),
                }
            ),
        }