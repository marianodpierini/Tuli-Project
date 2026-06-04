import json
from decimal import Decimal
from sqlalchemy import cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import joinedload
from database.db import SessionLocal
from database.models import (
    InvoicesExtractedEmails,
    ServicesExtractedEmails,
    IncomingEmails,
    InvoiceCases,
    InvoiceTransitions
)

class CustomJSONEncoder(json.JSONEncoder):
    """Codificador para manejar tipos Decimal y objetos de fecha en el JSON."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if hasattr(obj, 'isoformat'):
            return obj.isoformat()
        return super().default(obj)

def lambda_handler(event, context):
    """
    Handler que devuelve la información consolidada de las facturas en formato JSON.
    Relaciona la extracción de datos con el estado del procesamiento del mail.
    """
    session = SessionLocal()
    try:

        query = (
            session.query(
                InvoicesExtractedEmails,
                InvoiceCases.state,
                IncomingEmails.sender,
                IncomingEmails.received_at,
                IncomingEmails.subject
            )
            .join(
                InvoiceCases,
                InvoicesExtractedEmails.case_id == InvoiceCases.case_id
            )
            .join(
                IncomingEmails,
                IncomingEmails.email_id == InvoiceCases.email_id
            )
            .join(
                InvoiceTransitions,
                InvoiceCases.case_id == InvoiceTransitions.case_id
            )
            .filter(
                InvoiceCases.state == 'LISTO PARA CARGAR'
            )
            .options(joinedload(InvoicesExtractedEmails.services))
            .distinct(InvoicesExtractedEmails.id)
        )

        results = query.all()
        items = []

        for iee, state, sender, received_at, subject in results:
            invoice_item = {
                "cuit": iee.cuit,
                "numero_factura": iee.numero_factura,
                "fecha_factura": iee.fecha_factura,
                "razon_social": iee.razon_social,
                "moneda": iee.moneda,
                "importe_total": iee.importe_total,
                "estado_procesamiento": state,
                "email_info": {
                    "remitente": sender,
                    "asunto": subject,
                    "fecha_recepcion": received_at
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
                        "ya_facturado": s.ya_facturado
                    }
                    for s in iee.services
                ]
            }
            items.append(invoice_item)

        print(items)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"items": items}, cls=CustomJSONEncoder)
        }

    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Error consultando facturas", "details": str(e)})
        }
    finally:
        session.close()
