import json
import logging
import sys
import time as pytime
import traceback

from core.request_handler import APIGatewayModel, BedrockEvent
from core.api_handler import ApiRequestHandler
from core.bedrock_handler import BedrockRequestHandler
from core.scheduled_handler import ScheduledHandler
from core.helpers.helpers import normalize_event

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    force=True,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)
session_store = {}


def get_params_api_gateway(event):
    return APIGatewayModel(
        http_method=event.get("httpMethod"),
        resource=event.get("resource"),
        authorization=event.get("headers").get("Authorization"),
        request_context=event.get("requestContext"),
        headers=event.get("headers"),
        body=json.loads(event.get("body")),
        source=event.get("source", "front"),
    )


def get_params_bedrock(event):
    return BedrockEvent(
        http_method=event.get("httpMethod"),
        session_id=event.get("sessionId"),
        action_group=event.get("actionGroup"),
        input_text=event.get("inputText"),
        api_path=event.get("apiPath"),
        request_body=event.get("requestBody"),
        session_attributes=event.get("sessionAttributes"),
        prompt_session_attributes=event.get("promptSessionAttributes"),
        agent=event.get("agent"),
        parameters=event.get("parameters"),
    )


def is_api_gateway_event(event: dict) -> bool:
    return "httpMethod" in event and "path" in event


def is_rule_event(event: dict) -> bool:
    return "message" in event and "Scheduled Event" in event["message"]["detail-type"]


def lambda_handler(event, context):
    """
    Lambda handler principal con sistema completo de logging por usuarios
    """
    start_time = pytime.time()

    event_normalize = normalize_event(event)
    logger.info(f"EVENT: {json.dumps(event_normalize)}")

    req_id = event_normalize.get("requestContext", {}).get(
        "requestId", context.aws_request_id
    )

    try:

        if is_api_gateway_event(event_normalize):
            logger.debug(f"[{req_id}] Evento tipo API Gateway detectado")

            event = get_params_api_gateway(event_normalize)
            handler = ApiRequestHandler(logger, req_id, event, lambda_handler)

            return handler.handle_event()
        elif is_rule_event(event_normalize):
            logger.debug(f"[{req_id}] Evento tipo Scheduled detectado")

            params = (
                event_normalize["message"]["detail"]["users"]
                if len(event_normalize["message"]["detail"]["users"]) > 0
                else None
            )

            handler = ScheduledHandler(logger, params)
            return handler.handle_event()
        else:
            logger.debug(f"[{req_id}] Evento tipo Bedrock detectado")

            event = get_params_bedrock(event)
            handler = BedrockRequestHandler(logger, req_id, event, lambda_handler)

            return handler.handle_event()

    except Exception as e:
        execution_time = (pytime.time() - start_time) * 1000
        stack_trace = traceback.format_exc()

        # Log estructurado del error principal
        logger.error(
            f"LAMBDA_ERROR: {str(e)}, REQUEST_ID: {req_id}, EXECUTION_TIME: {execution_time}, STACK_TRACE: {stack_trace}"
        )

        logger.error(
            f"[{req_id}] Error general en lambda_handler: {str(e)}\n{stack_trace}"
        )

        return False
