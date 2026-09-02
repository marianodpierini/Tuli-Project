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
    PercepcionesIIBB,
)

from .reprocess_invoice.reprocess_invoice import ReprocessInvoice

s3_client = boto3.client("s3")
BUCKET_NAME = os.getenv("PDF_BUCKET", "PDF_BUCKET")
OPERADORES_KEY = os.environ.get("OPERADORES_KEY", "lambda-files/operadores.json")

LIST_STATES = [
    "RECIBIDO",
    "LISTO_PARA_CARGAR",
    "LOADED_BY_IT",
    "LOAD_FAILED",
    "DUPLICADO",
    "DESCARTADO",
    "EN_REVISION",
    "RECHAZADA",
    "ERROR",
]

ACK_SUCCESS_STATES = {
    "cargada",
    "cargado",
    "loaded",
    "ok",
    "success",
    "exitosa",
}

ACK_ERROR_STATES = {
    "error",
    "failed",
    "fallida",
    "fallo",
    "rechazada",
}


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

    def _normalize_invoice_decision(self, raw_decision):
        if not isinstance(raw_decision, str):
            return None

        normalized_decision = (
            raw_decision.strip().lower().replace("-", "_").replace(" ", "_")
        )

        if normalized_decision in {"aprobar", "aprobada", "approved", "approve"}:
            return "LISTO_PARA_CARGAR"
        if normalized_decision in {"rechazar", "rechazada", "rejected", "reject"}:
            return "RECHAZADA"

        return None

    def handle_invoice_decision(self):
        invoice_id = self.event.get("pathParameters", {}).get("id")
        if invoice_id is None:
            invoice_id = self.event.get("pathParameters", {}).get("id_factura")

        if invoice_id is None:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "error": "Falta id de factura",
                        "details": "Se esperaba el parametro de path 'id'",
                    }
                ),
            }

        try:
            body = json.loads(self.event.get("body", "{}"))
        except json.JSONDecodeError:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Body JSON invalido"}),
            }

        raw_decision = body.get("decision")
        if raw_decision is None:
            raw_decision = body.get("action")

        reason = body.get("reason")
        if reason is None:
            reason = body.get("motivo")

        target_state = self._normalize_invoice_decision(raw_decision)
        if target_state is None:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "error": "Decision invalida",
                        "details": "Enviar decision=aprobar|rechazar",
                    }
                ),
            }

        normalized_reason = reason.strip() if isinstance(reason, str) else None
        if target_state == "RECHAZADA" and not normalized_reason:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "error": "Motivo requerido",
                        "details": "Para rechazar una factura se debe enviar reason",
                    }
                ),
            }

        with SessionLocal() as session:
            try:
                invoice = (
                    session.query(InvoicesExtractedEmails)
                    .filter_by(id=invoice_id)
                    .first()
                )

                if not invoice:
                    return {
                        "statusCode": 404,
                        "body": json.dumps({"error": "Factura no encontrada"}),
                    }

                invoice_case = (
                    session.query(InvoiceCases)
                    .filter_by(case_id=invoice.case_id)
                    .first()
                )

                if not invoice_case:
                    return {
                        "statusCode": 404,
                        "body": json.dumps({"error": "Caso de factura no encontrado"}),
                    }

                old_state = invoice_case.state
                old_reason = invoice_case.state_reason or ""
                new_reason = normalized_reason if target_state == "RECHAZADA" else None

                is_idempotent = old_state == target_state and (
                    target_state != "RECHAZADA" or old_reason == (new_reason or "")
                )

                if is_idempotent:
                    return {
                        "statusCode": 200,
                        "body": json.dumps(
                            {
                                "message": "Decision ya aplicada previamente",
                                "idempotent": True,
                                "id_factura": invoice.id,
                                "state": invoice_case.state,
                                "reason": invoice_case.state_reason,
                            }
                        ),
                    }

                invoice_case.state = target_state
                invoice_case.state_reason = new_reason

                invoice_transition = InvoiceTransitions(
                    case_id=invoice_case.case_id,
                    from_state=old_state,
                    to_state=target_state,
                    reason=new_reason,
                    actor="Frontend API",
                )
                session.add(invoice_transition)
                session.commit()

                return {
                    "statusCode": 200,
                    "body": json.dumps(
                        {
                            "message": "Decision aplicada correctamente",
                            "idempotent": False,
                            "id_factura": invoice.id,
                            "state": target_state,
                            "reason": new_reason,
                        }
                    ),
                }

            except Exception as e:
                session.rollback()
                self.logger.error(
                    f"Error aplicando decision de factura {invoice_id}: {e}"
                )
                return {
                    "statusCode": 500,
                    "body": json.dumps(
                        {
                            "error": "Error aplicando decision de factura",
                            "details": str(e),
                        }
                    ),
                }

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
                    PercepcionesIIBB.monto,
                    PercepcionesIIBB.provincia,
                    PercepcionesIIBB.id_provincia,
                )
                .join(
                    InvoiceCases,
                    InvoicesExtractedEmails.case_id == InvoiceCases.case_id,
                )
                .join(IncomingEmails, IncomingEmails.email_id == InvoiceCases.email_id)
                .join(
                    InvoiceTransitions,
                    InvoiceCases.case_id == InvoiceTransitions.case_id,
                )
                .join(
                    PercepcionesIIBB,
                    InvoicesExtractedEmails.id == PercepcionesIIBB.invoice_id,
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

            for iee, state, sender, received_at, subject, monto, provincia, id_provincia in results:
                reservas_por_id = {}
                for service in iee.services:
                    reserva_id = service.id_reserva_mo
                    reservas_por_id[reserva_id] = {
                        "reserve_id": reserva_id,
                        "importe": service.importe,
                    }

                invoice_date = iee.fecha_factura

                invoice_item = {
                    "id_factura": iee.id,
                    "cuit": iee.cuit,
                    "operador": {
                        "operator_aptour_id": (
                            iee.ids_operadores[0] if iee.ids_operadores else None
                        ),
                        "razon_social": iee.razon_social,
                    },
                    "invoice_kind": iee.tipo_comprobante,
                    "numero_factura": iee.numero_factura,
                    "branch": iee.punto_venta,
                    "number": iee.numero_comprobante,
                    "voucher": iee.voucher,
                    "invoice_date": invoice_date,
                    "month": invoice_date.month if invoice_date else None,
                    "year": invoice_date.year if invoice_date else None,
                    "currency": iee.moneda,
                    "cotization": iee.cotizacion,
                    "total": iee.importe_total,
                    "cost_center_one": "Aero B",
                    "cost_center_two": "Tours",
                    "invoice_amount_attributes": {
                        "exempt": iee.exento,
                        "not_computable": iee.no_computable,
                        "taxable_21": iee.gravado_21,
                        "taxable_10_5": iee.gravado_105,
                        "iva_perception": iee.percepcion_iva,
                    },
                    "invoice_perceptions_attributes": [
                        {
                            "amount": monto,
                            "province_id": id_provincia,
                        }
                    ],
                    "reservas": list(reservas_por_id.values()),
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
        reason_change = body.get("reason", "")

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

        if state is not None and state not in LIST_STATES:
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "error": "Estado inválido",
                        "details": f"El estado '{state}' no es válido. Estados válidos: {LIST_STATES}",
                    }
                ),
            }

        with SessionLocal() as session:
            try:
                query_get_invoice = session.query(InvoicesExtractedEmails).filter_by(
                    id=invoice_id
                )
                invoice = query_get_invoice.first()

                if not invoice:
                    return {
                        "statusCode": 404,
                        "body": json.dumps({"error": "Factura no encontrada"}),
                    }

                invoice_case = (
                    session.query(InvoiceCases)
                    .filter_by(case_id=invoice.case_id)
                    .first()
                )
                if not invoice_case:
                    return {
                        "statusCode": 404,
                        "body": json.dumps({"error": "Caso de factura no encontrado"}),
                    }

                if state is not None:
                    invoice_case.state = state
                    old_state = None

                    invoice_transition = (
                        session.query(InvoiceTransitions)
                        .filter_by(case_id=invoice_case.case_id)
                        .first()
                    )
                    if invoice_transition:
                        old_state = invoice_transition.to_state

                    invoice_transition = InvoiceTransitions(
                        case_id=invoice_case.case_id,
                        from_state=old_state,
                        to_state=state,
                        reason=reason_change,
                        actor="Frontend API",
                    )
                    session.add(invoice_transition)

                if operator_id is not None:
                    invoice.ids_operadores = [operator_id]
                    services_invoice = (
                        session.query(ServicesExtractedEmails)
                        .filter_by(invoice_id=invoice.id)
                        .all()
                    )
                    for service_invoice in services_invoice:
                        service_invoice.id_operador = operator_id

                updated_services = 0
                list_services_updated = []
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
                        service.id_reserva_mo = service_data.get("id_reserva_mo")
                        updated_services += 1
                        list_services_updated.append({
                            "id": service.id,
                            "codigo": service.codigo,
                            "pasajero": service.pasajero,
                            "importe": service.importe,
                            "vinculado": service.vinculado,
                            "id_servicio": service.id_servicio,
                            "id_reserva_aptour": service.id_reserva_aptour,
                            "id_reserva_mo": service.id_reserva_mo,
                            "id_operador": service.id_operador,
                            "ya_facturado": service.ya_facturado,
                        })

                session.commit()

                response = {
                    "cuit": invoice.cuit,
                    "numero_factura": invoice.numero_factura,
                    "fecha_factura": invoice.fecha_factura,
                    "razon_social": invoice.razon_social,
                    "moneda": invoice.moneda,
                    "importe_total": invoice.importe_total,
                    "tipo_comprobante": invoice.tipo_comprobante,
                    "punto_venta": invoice.punto_venta,
                    "numero_comprobante": invoice.numero_comprobante,
                    "cotizacion": invoice.cotizacion,
                    "estado_procesamiento": state,
                    "servicios": list_services_updated
                }

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

    def _normalize_ack_target_state(self, raw_state):
        if not isinstance(raw_state, str):
            return None

        normalized = raw_state.strip().lower().replace("-", "_").replace(" ", "_")

        if normalized in ACK_SUCCESS_STATES:
            return "LOADED_BY_IT"
        if normalized in ACK_ERROR_STATES:
            return "LOAD_FAILED"
        if normalized == "loaded_by_it":
            return "LOADED_BY_IT"
        if normalized == "load_failed":
            return "LOAD_FAILED"

        return None

    def handle_ack_invoices(self):
        try:
            body = json.loads(self.event.get("body", "{}"))
        except json.JSONDecodeError:
            return {
                "statusCode": 400,
                "body": json.dumps({"error": "Body JSON invalido"}),
            }

        acks = body.get("acuses")
        if acks is None:
            acks = body.get("acks")
        if acks is None:
            acks = body.get("items")

        if not isinstance(acks, list):
            return {
                "statusCode": 400,
                "body": json.dumps(
                    {
                        "error": "Formato invalido",
                        "details": "Se esperaba un array en 'acuses', 'acks' o 'items'",
                    }
                ),
            }

        responses = []

        with SessionLocal() as session:
            for index, ack in enumerate(acks):
                item_result = {
                    "index": index,
                    "ok": False,
                }

                if not isinstance(ack, dict):
                    item_result.update(
                        {
                            "status_code": 400,
                            "error": "Acuse invalido",
                            "details": "Cada elemento del array debe ser un objeto JSON",
                        }
                    )
                    responses.append(item_result)
                    continue

                invoice_id = ack.get("id_factura")
                if invoice_id is None:
                    invoice_id = ack.get("invoice_id")
                if invoice_id is None:
                    invoice_id = ack.get("id")

                raw_ack_state = ack.get("resultado")
                if raw_ack_state is None:
                    raw_ack_state = ack.get("status")
                if raw_ack_state is None:
                    raw_ack_state = ack.get("estado")
                if raw_ack_state is None:
                    raw_ack_state = ack.get("outcome")

                reason = ack.get("motivo")
                if reason is None:
                    reason = ack.get("reason")
                if reason is None:
                    reason = ack.get("detalle_error")

                item_result["id_factura"] = invoice_id
                target_state = self._normalize_ack_target_state(raw_ack_state)

                if invoice_id is None:
                    item_result.update(
                        {
                            "status_code": 400,
                            "error": "Falta id_factura",
                        }
                    )
                    responses.append(item_result)
                    continue

                if target_state is None:
                    item_result.update(
                        {
                            "status_code": 400,
                            "error": "Estado de acuse invalido",
                            "details": "Valores validos: cargada/loaded o error/failed",
                        }
                    )
                    responses.append(item_result)
                    continue

                normalized_reason = None
                if isinstance(reason, str):
                    normalized_reason = reason.strip()

                if target_state == "LOAD_FAILED" and not normalized_reason:
                    item_result.update(
                        {
                            "status_code": 400,
                            "error": "Motivo requerido",
                            "details": "Cuando el acuse es error/fallida se debe enviar motivo",
                        }
                    )
                    responses.append(item_result)
                    continue

                try:
                    invoice = (
                        session.query(InvoicesExtractedEmails)
                        .filter_by(id=invoice_id)
                        .first()
                    )

                    if not invoice:
                        item_result.update(
                            {
                                "status_code": 404,
                                "error": "Factura no encontrada",
                            }
                        )
                        responses.append(item_result)
                        continue

                    invoice_case = (
                        session.query(InvoiceCases)
                        .filter_by(case_id=invoice.case_id)
                        .first()
                    )

                    if not invoice_case:
                        item_result.update(
                            {
                                "status_code": 404,
                                "error": "Caso de factura no encontrado",
                            }
                        )
                        responses.append(item_result)
                        continue

                    current_reason = invoice_case.state_reason or ""
                    expected_reason = normalized_reason or ""

                    if invoice_case.state == target_state and (
                        target_state != "LOAD_FAILED"
                        or current_reason == expected_reason
                    ):
                        item_result.update(
                            {
                                "ok": True,
                                "status_code": 200,
                                "idempotent": True,
                                "state": invoice_case.state,
                                "message": "Acuse ya aplicado previamente",
                            }
                        )
                        responses.append(item_result)
                        continue

                    old_state = invoice_case.state
                    invoice_case.state = target_state
                    invoice_case.state_reason = (
                        normalized_reason if target_state == "LOAD_FAILED" else None
                    )

                    invoice_transition = InvoiceTransitions(
                        case_id=invoice_case.case_id,
                        from_state=old_state,
                        to_state=target_state,
                        reason=(
                            normalized_reason if target_state == "LOAD_FAILED" else None
                        ),
                        actor="IT",
                    )
                    session.add(invoice_transition)
                    session.commit()

                    item_result.update(
                        {
                            "ok": True,
                            "status_code": 200,
                            "idempotent": False,
                            "state": target_state,
                        }
                    )
                    responses.append(item_result)

                except Exception as e:
                    session.rollback()
                    self.logger.error(
                        f"Error procesando acuse para factura {invoice_id}: {e}"
                    )
                    item_result.update(
                        {
                            "status_code": 500,
                            "error": "Error procesando acuse",
                            "details": str(e),
                        }
                    )
                    responses.append(item_result)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"items": responses}, cls=CustomJSONEncoder),
        }

    def handle_get_pdf_invoice(self):
        try:
            invoice_id = self.event.get("pathParameters", {}).get("id_factura")

            with SessionLocal() as session:
                query_get_invoice = session.query(InvoicesExtractedEmails).filter_by(
                    id=invoice_id
                )
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

            return {"statusCode": 200, "body": json.dumps({"pdf_url": url})}
        except Exception as e:
            self.logger.error(f"Error obteniendo PDF de la factura: {e}")
            return {
                "statusCode": 500,
                "body": json.dumps({"error": "Error obteniendo PDF de la factura"}),
            }

    def handle_reprocess_invoice(self):
        try:

            invoice_id = self.event.get("pathParameters", {}).get("id_factura")

            reprocess_invoice = ReprocessInvoice(
                invoice_id,
                s3_client,
                self.logger,
                SessionLocal,
                BUCKET_NAME,
                OPERADORES_KEY,
            )
            return reprocess_invoice.reprocess()

        except Exception as e:
            self.logger.error(f"Error reprocessing invoice: {e}")
            return {
                "statusCode": 500,
                "body": json.dumps(
                    {"error": "Error reprocessing invoice", "details": str(e)}
                ),
            }

    def handle_list_invoices(self):
        query_params = self.event.get("queryStringParameters") or {}

        raw_states = query_params.get("estado")
        page_param = query_params.get("page")
        limit_param = query_params.get("limit")

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

        state_filters = []
        if raw_states:
            decoded_states = unquote_plus(raw_states)
            state_filters = [
                state.strip()
                for state in decoded_states.split(",")
                if state and state.strip()
            ]

        session = SessionLocal()

        try:
            service_counts_subquery = (
                session.query(
                    ServicesExtractedEmails.invoice_id.label("invoice_id"),
                    func.count(ServicesExtractedEmails.id).label("servicios_total"),
                    func.count(ServicesExtractedEmails.id_servicio).label(
                        "servicios_vinculados"
                    ),
                )
                .group_by(ServicesExtractedEmails.invoice_id)
                .subquery()
            )

            query = (
                session.query(
                    InvoicesExtractedEmails.id.label("id_factura"),
                    InvoiceCases.case_id,
                    InvoiceCases.state,
                    InvoiceCases.state_reason,
                    InvoicesExtractedEmails.razon_social,
                    InvoicesExtractedEmails.cuit,
                    InvoicesExtractedEmails.numero_factura,
                    InvoicesExtractedEmails.fecha_factura,
                    InvoicesExtractedEmails.moneda,
                    InvoicesExtractedEmails.importe_total,
                    InvoiceCases.attachment_name,
                    InvoiceCases.created_at,
                    func.coalesce(
                        service_counts_subquery.c.servicios_total,
                        0,
                    ).label("servicios_total"),
                    func.coalesce(
                        service_counts_subquery.c.servicios_vinculados,
                        0,
                    ).label("servicios_vinculados"),
                )
                .join(
                    InvoiceCases,
                    InvoicesExtractedEmails.case_id == InvoiceCases.case_id,
                )
                .outerjoin(
                    service_counts_subquery,
                    service_counts_subquery.c.invoice_id == InvoicesExtractedEmails.id,
                )
            )

            if state_filters:
                query = query.filter(InvoiceCases.state.in_(state_filters))

            total_items_query = session.query(
                func.count(InvoicesExtractedEmails.id)
            ).join(
                InvoiceCases,
                InvoicesExtractedEmails.case_id == InvoiceCases.case_id,
            )

            if state_filters:
                total_items_query = total_items_query.filter(
                    InvoiceCases.state.in_(state_filters)
                )

            total_items = total_items_query.scalar() or 0
            total_pages = (total_items + limit - 1) // limit if total_items else 0

            results = (
                query.order_by(InvoicesExtractedEmails.id.desc())
                .offset(offset)
                .limit(limit)
                .all()
            )

            items = []
            for row in results:
                items.append(
                    {
                        "id_factura": row.id_factura,
                        "case_id": (
                            str(row.case_id) if row.case_id is not None else None
                        ),
                        "state": row.state,
                        "state_reason": row.state_reason,
                        "razon_social": row.razon_social,
                        "cuit": row.cuit,
                        "numero_factura": row.numero_factura,
                        "fecha_factura": row.fecha_factura,
                        "moneda": row.moneda,
                        "importe_total": row.importe_total,
                        "servicios_total": row.servicios_total,
                        "servicios_vinculados": row.servicios_vinculados,
                        "attachment_name": row.attachment_name,
                        "created_at": row.created_at,
                    }
                )

            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(
                    {
                        "items": items,
                        "pagination": {
                            "page": page,
                            "limit": limit,
                            "total_items": total_items,
                            "total_pages": total_pages,
                            "has_next": page < total_pages,
                            "has_previous": page > 1,
                        },
                    },
                    cls=CustomJSONEncoder,
                ),
            }

        except Exception as e:
            self.logger.error(f"Error listando facturas: {e}")
            return {
                "statusCode": 500,
                "body": json.dumps(
                    {"error": "Error listando facturas", "details": str(e)}
                ),
            }
        finally:
            session.close()

    def handle_get_invoice(self):
        invoice_id = self.event.get("pathParameters", {}).get("id_factura")
        session = SessionLocal()

        try:
            query = (
                session.query(
                    InvoicesExtractedEmails,
                    InvoiceCases.state,
                    InvoiceCases.state_reason,
                    InvoiceCases.case_id,
                    InvoiceCases.created_at,
                    IncomingEmails.sender,
                    IncomingEmails.subject,
                    IncomingEmails.received_at,
                    PercepcionesIIBB.id_provincia,
                    PercepcionesIIBB.provincia,
                    PercepcionesIIBB.monto,
                )
                .join(
                    InvoiceCases,
                    InvoicesExtractedEmails.case_id == InvoiceCases.case_id,
                )
                .join(IncomingEmails, IncomingEmails.email_id == InvoiceCases.email_id)
                .join(
                    PercepcionesIIBB,
                    InvoicesExtractedEmails.id == PercepcionesIIBB.invoice_id,
                )
                .filter(InvoicesExtractedEmails.id == invoice_id)
                .options(joinedload(InvoicesExtractedEmails.services))
                .distinct(InvoicesExtractedEmails.id)
            )

            results = query.all()
            invoice_item = None

            for iee, state, state_reason, case_id, created_at, sender, subject, received_at, id_provincia, provincia, monto in results:
                invoice_item = {
                    "id_factura": iee.id,
                    "case_id": case_id,
                    "cuit": iee.cuit,
                    "state_reason": state_reason,
                    "created_at": created_at,
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
                    "email": {
                        "remitente": sender,
                        "asunto": subject,
                        "recibido": received_at,
                    },
                    "desgloce_impositivo": {
                        "moneda": iee.moneda,
                        "exento": iee.exento,
                        "no_computable": iee.no_computable,
                        "gravado_21": iee.gravado_21,
                        "gravado_105": iee.gravado_105,
                        "percepcion_iva": iee.percepcion_iva,
                        "percepciones_iibb": [
                            {
                                "provincia": provincia,
                                "monto": monto,
                                "id_provincia": id_provincia,
                            }
                        ]
                    },
                    "servicios": [
                        {
                            "id": s.id,
                            "codigo": s.codigo,
                            "pasajero": s.pasajero,
                            "importe": s.importe,
                            "desc_neto": s.desc_neto,
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

            if invoice_item is None:
                return {
                    "statusCode": 404,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"item": {}}, cls=CustomJSONEncoder),
                }

            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"item": invoice_item}, cls=CustomJSONEncoder),
            }

        except Exception as e:
            self.logger.error(f"Error searching invoice: {e}")
            return {
                "statusCode": 500,
                "body": json.dumps(
                    {"error": "Error searching invoice", "details": str(e)}
                ),
            }

    def handle_get_meta(self):
        session = SessionLocal()
        try:
            rows = (
                session.query(InvoiceCases.state, func.count(InvoiceCases.case_id))
                .group_by(InvoiceCases.state)
                .all()
            )

            state_counts = {state: 0 for state in LIST_STATES}
            for state, count in rows:
                if state in state_counts:
                    state_counts[state] = count

            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"states": state_counts}),
            }
        except Exception as e:
            self.logger.error(f"Error getting invoice metadata: {e}")
            return {
                "statusCode": 500,
                "body": json.dumps(
                    {"error": "Error getting invoice metadata", "details": str(e)}
                ),
            }
        finally:
            session.close()
