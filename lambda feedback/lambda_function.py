from dataclasses import dataclass
import json
from typing import Dict, Optional
import boto3
from datetime import datetime
import uuid
import logging

import jwt

from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table('chatbot_user_feedback_table')
agent_responses_feedback = dynamodb.Table("agent_responses_feedback")


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

        feedback = event.body.get("text")

        
        feedback = {
            "user_id": event.body.get("user"),
            "feedback_date": datetime.now().isoformat(),
            "feedback": feedback.split("/feedback")[1],
            "source": "Google Chat",
        }

        if event.body.get("isReply"):
            space_name = event.body.get("space")
            resp = agent_responses_feedback.query(
                KeyConditionExpression=Key("id_thread").eq(space_name),
                ScanIndexForward=False,
                Limit=1
            )

            item = resp["Items"][0] if resp["Items"] else None

            if item:
                feedback["bot_response_text"] = item.get("bot_response_text")

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
