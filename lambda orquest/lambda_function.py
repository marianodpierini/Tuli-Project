import json
import boto3
import os


PROCESSING_LAMBDA = os.environ["PROCESSING_LAMBDA_NAME"]
PROCESSING_LAMBDA_TEST = os.environ["PROCESSING_LAMBDA_TEST_NAME"]


def lambda_handler(event, context):
    lambda_client = boto3.client("lambda")

    if event.get("source", "") == "warmup":
        event_data = json.dumps(event)
        lambda_client.invoke(
            FunctionName=PROCESSING_LAMBDA, InvocationType="Event", Payload=event_data
        )

        lambda_client.invoke(
            FunctionName=PROCESSING_LAMBDA_TEST, InvocationType="Event", Payload=event_data
        )

        return True

    payload = json.dumps(event)

    lambda_name = (
        PROCESSING_LAMBDA_TEST
        if event.get("resource") == "/webhooks/google-test"
        else PROCESSING_LAMBDA
    )

    lambda_client.invoke(
        FunctionName=lambda_name, InvocationType="Event", Payload=payload
    )

    body = {
        "hostAppDataAction": {
            "chatDataAction": {
                "createMessageAction": {
                    "message": {"text": "Estoy pensando en tu consulta..."}
                }
            }
        }
    }

    response = {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }

    return response
