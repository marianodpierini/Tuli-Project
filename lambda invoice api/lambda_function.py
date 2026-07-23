import json
import sys
import logging
import os

from core.request_handler import RequestHandler

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    force=True,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

CORS_ALLOW_ORIGIN = os.getenv("CORS_ALLOW_ORIGIN", "*")
CORS_ALLOW_HEADERS = os.getenv(
    "CORS_ALLOW_HEADERS",
    "Content-Type,Authorization,X-Api-Key,X-Amz-Date,X-Amz-Security-Token",
)
CORS_ALLOW_METHODS = os.getenv("CORS_ALLOW_METHODS", "GET,PATCH,OPTIONS")


def _cors_headers():
    return {
        "Access-Control-Allow-Origin": CORS_ALLOW_ORIGIN,
        "Access-Control-Allow-Headers": CORS_ALLOW_HEADERS,
        "Access-Control-Allow-Methods": CORS_ALLOW_METHODS,
    }


def _response(status_code, body):
    if isinstance(body, str):
        payload = body
    else:
        payload = json.dumps(body)

    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            **_cors_headers(),
        },
        "body": payload,
    }


def _with_cors(response):
    response = response or {}
    headers = response.get("headers", {})
    response["headers"] = {
        **_cors_headers(),
        **headers,
    }

    if "statusCode" not in response:
        response["statusCode"] = 200
    if "body" not in response:
        response["body"] = ""

    return response

def lambda_handler(event, context):
    try:
        logger.info(f"Received event: {json.dumps(event)}")
        method = event.get("httpMethod")
        resource = event.get("resource")

        if method == "OPTIONS":
            return _response(200, {"ok": True})

        request_handler = RequestHandler(event, logger)

        if resource == "/invoices/send_invoices" and method == "GET":
            return _with_cors(request_handler.handle_send_invoices())
        if resource == "/invoices/update_invoice/{id_factura}" and method == "PATCH":
            return _with_cors(request_handler.handle_update_invoice())
        if resource == "/invoices/see_invoice/{id_factura}/pdf" and method == "GET":
            return _with_cors(request_handler.handle_get_pdf_invoice())
        if resource == "/invoices/reprocess_invoice/{id_factura}" and method == "GET":
            return _with_cors(request_handler.handle_reprocess_invoice())

        known_resources = {
            "/invoices/send_invoices": {"GET"},
            "/invoices/update_invoice/{id_factura}": {"PATCH"},
            "/invoices/see_invoice/{id_factura}/pdf": {"GET"},
            "/invoices/reprocess_invoice/{id_factura}": {"GET"},
        }
        if resource in known_resources:
            return _response(
                405,
                {
                    "error": "Method Not Allowed",
                    "allowed_methods": sorted(list(known_resources[resource] | {"OPTIONS"})),
                },
            )

        return _response(404, {"error": "Not Found"})
        
    except Exception as e:
        logger.error(f"Error processing request: {e}")
        return _response(500, {"error": "Internal server error"})