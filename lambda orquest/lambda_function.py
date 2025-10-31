import json
import boto3
import os

lambda_client = boto3.client("lambda")
PROCESSING_LAMBDA = os.environ["PROCESSING_LAMBDA_NAME"]
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "miverifytoken123")

def lambda_handler(event, context):
    lambda_client.invoke(
        FunctionName=PROCESSING_LAMBDA,
        InvocationType="Event",
        Payload=json.dumps(event)
    )

    raw_body = event.get("body", "")
    body_json = json.loads(raw_body)
    message_payload = body_json.get("chat", {}).get("messagePayload", {})
    message_obj = message_payload.get("message", {})

    response_body = {
        "text": "Procesando tu solicitud...",
        "space": message_obj.get("space", {}).get("name", ""),
        "thread": message_obj.get("thread", {}).get("name", ""),
    }

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(response_body)
    }
