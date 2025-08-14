from dataclasses import dataclass
import json
from typing import Dict, Optional
import boto3
from datetime import datetime
import uuid
import logging

import jwt

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('chatbot_user_feedback_table')


@dataclass
class APIGatewayModel():
    http_method: Optional[str]
    resource: Optional[str]
    authorization: Optional[str]
    request_context: Optional[Dict[str, str]]
    headers: Optional[Dict[str, str]]
    body: Optional[Dict[str, str]]


def get_params_api_gateway(event):
    return APIGatewayModel(
        http_method=event.get("httpMethod"),
        resource=event.get("resource"),
        authorization=event.get('headers').get("Authorization"),
        request_context=event.get("requestContext"),
        headers=event.get("headers"),
        body=json.loads(event.get("body")),
    )

def get_user_context(event: APIGatewayModel):
    identity = event.request_context.get('identity', {})

    token = event.authorization.replace("Bearer ", "") if event.authorization else None
    user_email = None
    session_id = None

    if token:
        decoded = jwt.decode(token, options={"verify_signature": False})

        user_email = decoded.get("email")
        session_id = decoded.get("sub")

    if not session_id:
        session_id = str(uuid.uuid4())[:8]

    return session_id, user_email


def lambda_handler(event, context):
    try:
        logger.info(f"EVENT: {json.dumps(event)}")

        event = get_params_api_gateway(event)

        session_id, user_email = get_user_context(event)
        
        feedback = {
            "user_id": user_email,
            "feedback_date": datetime.now().isoformat(),
            "type": event.body.get("type"),
            "user_question": event.body.get("user_question"),
            "agent_response": event.body.get("agent_response"),
            "feedback": event.body.get("feedback"),
            "comment": event.body.get("comment"),
            "success": event.body.get("success"),
            "source": event.body.get("source"),
            "session_id": session_id
        }

        logger.info(f"Recibiendo feedback: {json.dumps(feedback)}")
        table.put_item(Item=feedback)

        return {
            "statusCode": 200,
            "body": json.dumps({"message": "Gracias por tu opinión!"}),
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "Content-Type",
                "Access-Control-Allow-Methods": "OPTIONS,POST"
            }
        }

    except Exception as e:
        logger.error(f"Error al guardar feedback: {str(e)}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Error al guardar feedback"}),
            "headers": {"Access-Control-Allow-Origin": "*"}
        }
