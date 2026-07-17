import json
import sys
import logging

from core.request_handler import RequestHandler

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    force=True,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

def lambda_handler(event, context):
    try:
        logger.info(f"Received event: {json.dumps(event)}")
        request_handler = RequestHandler(event, logger)

        if event.get("resource") == "/invoices/send_invoices":
            if event.get("httpMethod") == "GET":
                return request_handler.handle_send_invoices()
        if event.get("resource") == "/invoices/update_invoice/{id_factura}":
            if event.get("httpMethod") == "PATCH":
                return request_handler.handle_update_invoice()
        if event.get("resource") == "/invoices/see_invoice/{id_factura}/pdf":
            if event.get("httpMethod") == "GET":
                return request_handler.handle_get_pdf_invoice()
        if event.get("resource") == "/invoices/reprocess_invoice/{id_factura}":
            if event.get("httpMethod") == "GET":
                return request_handler.handle_reprocess_invoice()
        
    except Exception as e:
        logger.error(f"Error processing request: {e}")
        return {
            "statusCode": 500,
            "body": "Internal server error"
        }