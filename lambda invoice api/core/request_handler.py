import json
import boto3
import os
from decimal import Decimal
from urllib.parse import unquote_plus

from sqlalchemy import func
from sqlalchemy.orm import joinedload
from database.db import SessionLocal
from database.models import (
    InvoicesExtractedEmails,
    IncomingEmails,
    InvoiceCases,
    InvoiceTransitions,
    ServicesExtractedEmails,
)

from .reprocess_invoice.reprocess_invoice import ReprocessInvoice

s3_client = boto3.client("s3")
BUCKET_NAME = os.getenv("PDF_BUCKET", "PDF_BUCKET")
OPERADORES_KEY = os.environ.get("OPERADORES_KEY", "lambda-files/operadores.json")


class CustomJSONEncoder(json.JSONEncoder):
    """Codificador para manejar tipos Decimal y objetos de fecha en el JSON."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return super().default(obj)


class RequestHandler:
    def __init__(self, event, logger):
        self.event = event
        self.logger = logger

    def handle_send_invoices(self):
        raw_estado = self.event.get("pathParameters", {}).get(
            "estado", "LISTO PARA CARGAR"
        )
        estado = unquote_plus(raw_estado).strip() if raw_estado else "LISTO_PARA_CARGAR"
        query_params = self.event.get("queryStringParameters") or {}
        page_param = query_params.get("page")
        limit_param = query_params.get("limit")
        use_pagination = page_param is not None or limit_param is not None

        page = None
        limit = None
        offset = None

        if use_pagination:
            try:
                page = int(page_param) if page_param is not None else 1
                limit = int(limit_param) if limit_param is not None else 50
            except (TypeError, ValueError):
                return {
                    "statusCode": 400,
                    "body": json.dumps(
                        {
                            "error": "Parametros de paginacion invalidos",
                            "details": "page y limit deben ser numeros enteros",
                        }
                    ),
                }

            if page < 1 or limit < 1:
                return {
                    "statusCode": 400,
                    "body": json.dumps(
                        {
                            "error": "Parametros de paginacion invalidos",
                            "details": "page y limit deben ser mayores o iguales a 1",
                        }
                    ),
                }

            limit = min(limit, 200)
            offset = (page - 1) * limit
        session = SessionLocal()

        try:
            query = (
                session.query(
                    InvoicesExtractedEmails,
                    InvoiceCases.state,
                    IncomingEmails.sender,
                    IncomingEmails.received_at,
                    IncomingEmails.subject,
                )
                .join(InvoiceCases, InvoicesExtractedEmails.case_id == InvoiceCases.case_id)
                .join(IncomingEmails, IncomingEmails.email_id == InvoiceCases.email_id)
                .join(
                    InvoiceTransitions, InvoiceCases.case_id == InvoiceTransitions.case_id
                )
                .filter(InvoiceCases.state == estado)
                .options(joinedload(InvoicesExtractedEmails.services))
                .distinct(InvoicesExtractedEmails.id)
                .order_by(InvoicesExtractedEmails.id.desc())
            )

            total_items = None
            total_pages = None

            if use_pagination:
                total_items = (
                    session.query(func.count(func.distinct(InvoicesExtractedEmails.id)))
                    .join(
                        InvoiceCases,
                        InvoicesExtractedEmails.case_id == InvoiceCases.case_id,
                    )
                    .join(
                        InvoiceTransitions,
                        InvoiceCases.case_id == InvoiceTransitions.case_id,
                    )
                    .filter(InvoiceCases.state == estado)
                    .scalar()
                ) or 0

                total_pages = (total_items + limit - 1) // limit if total_items else 0
                query = query.offset(offset).limit(limit)

            results = query.all()
            items = []

            for iee, state, sender, received_at, subject in results:
                invoice_item = {
                    "id_factura": iee.id,
                    "cuit": iee.cuit,
                    "numero_factura": iee.numero_factura,
                    "fecha_factura": iee.fecha_factura,
                    "razon_social": iee.razon_social,
                    "moneda": iee.moneda,
                    "importe_total": iee.importe_total,
                    "tipo_comprobante": iee.tipo_comprobante,
                    "punto_venta": iee.punto_venta,
                    "numero_comprobante": iee.numero_comprobante,
                    "cotizacion": iee.cotizacion,
                    "estado_procesamiento": state,
                    "email_info": {
                        "remitente": sender,
                        "asunto": subject,
                        "fecha_recepcion": received_at,
                    },
                    "servicios": [
                        {
                            "codigo": s.codigo,
                            "pasajero": s.pasajero,
                            "importe": s.importe,
                            "vinculado": s.vinculado,
                            "id_servicio": s.id_servicio,
                            "id_reserva_aptour": s.id_reserva_aptour,
                            "id_reserva_mo": s.id_reserva_mo,
                            "id_operador": s.id_operador,
                            "ya_facturado": s.ya_facturado,
                        }
                        for s in iee.services
                    ],
                }
                items.append(invoice_item)

            response_body = {"items": items}
            if use_pagination:
                response_body["pagination"] = {
                    "page": page,
                    "limit": limit,
                    "total_items": total_items,
                    "total_pages": total_pages,
                    "has_next": page < total_pages,
                    "has_previous": page > 1,
                }

            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(response_body, cls=CustomJSONEncoder),
            }

        except Exception as e:
            self.logger.error(f"Error consultando facturas: {e}")
            return {
                "statusCode": 500,
                "body": json.dumps(
                    {"error": "Error consultando facturas", "details": str(e)}
                ),
            }
        finally:
            session.close()
    
    def handle_update_invoice(self):
        invoice_id = self.event.get("pathParameters", {}).get("id_factura")
        body = json.loads(self.event.get("body", "{}"))
        state = body.get("state")
        operator_id = body.get("operator_id")
        service_updates = body.get("services", [])

        if state is None and operator_id is None and not service_updates:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "error": "No hay campos para actualizar",
                        "details": "Enviar al menos uno de: state, operator_id o services",
                    }
                ),
            }

        with SessionLocal() as session:
            try:
                query_get_invoice = session.query(InvoicesExtractedEmails).filter_by(id=invoice_id)
                invoice = query_get_invoice.first()

                if not invoice:
                    return {
                        "statusCode": 404,
                        "body": json.dumps({"error": "Factura no encontrada"}),
                    }

                invoice_case = session.query(InvoiceCases).filter_by(case_id=invoice.case_id).first()
                if not invoice_case:
                    return {
                        "statusCode": 404,
                        "body": json.dumps({"error": "Caso de factura no encontrado"}),
                    }
                
                if state is not None:
                    invoice_case.state = state

                    invoice_transition = session.query(InvoiceTransitions).filter_by(case_id=invoice_case.case_id).first()
                    if invoice_transition:
                        invoice_transition.from_state = invoice_transition.to_state
                        invoice_transition.to_state = state

                if operator_id is not None:
                    invoice.ids_operadores = [operator_id]
                    service_invoice = session.query(ServicesExtractedEmails).filter_by(invoice_id=invoice.id).first()
                    service_invoice.id_operador = operator_id
                    

                updated_services = 0
                if service_updates:
                    for service_data in service_updates:
                        service_id = service_data.get("id")
                        if service_id is None:
                            continue

                        service = (
                            session.query(ServicesExtractedEmails)
                            .filter_by(id=service_id, invoice_id=invoice.id)
                            .first()
                        )

                        if not service:
                            continue

                        service.vinculado = True
                        service.id_servicio = service_data.get("id_servicio")
                        service.id_reserva_aptour = service_data.get("id_reserva_aptour")
                        service.id_reserva_mo = service_data.get("id_reserva_mo")
                        updated_services += 1

                session.commit()

                return {
                    "statusCode": 200,
                    "body": json.dumps(
                        {
                            "message": "Factura actualizada correctamente",
                            "updated": {
                                "state": state is not None,
                                "operator_id": operator_id is not None,
                                "services": updated_services,
                            },
                        }
                    ),
                }

            except Exception as e:
                session.rollback()
                self.logger.error(f"Error actualizando factura: {e}")
                return {
                    "statusCode": 500,
                    "body": json.dumps(
                        {"error": "Error actualizando factura", "details": str(e)}
                    ),
                }
            
    def handle_get_pdf_invoice(self):
        try:
            invoice_id = self.event.get("pathParameters", {}).get("id_factura")

            with SessionLocal() as session:
                query_get_invoice = session.query(InvoicesExtractedEmails).filter_by(id=invoice_id)
                invoice = query_get_invoice.first()

                if not invoice:
                    return {
                        "statusCode": 404,
                        "body": json.dumps({"error": "Factura no encontrada"}),
                    }
                
            s3_key = invoice.s3_key

            url = s3_client.generate_presigned_url(
                ClientMethod="get_object",
                Params={
                    "Bucket": BUCKET_NAME,
                    "Key": s3_key,
                },
                ExpiresIn=300,  # 5 minutos
            )

            print(url)

            return {
                "statusCode": 200,
                "body": json.dumps({
                    "pdf_url": url
                })
            }
        except Exception as e:
            self.logger.error(f"Error obteniendo PDF de la factura: {e}")
            return {
                "statusCode": 500,
                "body": json.dumps(
                    {"error": "Error obteniendo PDF de la factura"}
                ),
            }
        
    def handle_reprocess_invoice(self):
        try:

            invoice_id = self.event.get("pathParameters", {}).get("id_factura")

            reprocess_invoice = ReprocessInvoice(invoice_id, s3_client, self.logger, SessionLocal, BUCKET_NAME, OPERADORES_KEY)
            return reprocess_invoice.reprocess()

        except Exception as e:
            self.logger.error(f"Error reprocessing invoice: {e}")
            return {
                "statusCode": 500,
                "body": json.dumps(
                    {"error": "Error reprocessing invoice", "details": str(e)}
                ),
            }
        


        
