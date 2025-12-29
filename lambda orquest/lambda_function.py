import json
import boto3
import os


PROCESSING_LAMBDA = os.environ["PROCESSING_LAMBDA_NAME"]
PROCESSING_LAMBDA_TEST = os.environ["PROCESSING_LAMBDA_TEST_NAME"]

def lambda_handler(event, context):
    lambda_client = boto3.client("lambda")

    payload = json.dumps(event)

    lambda_name = PROCESSING_LAMBDA_TEST if event.get("resource") == "/webhooks/google-test" else PROCESSING_LAMBDA

    lambda_client.invoke(
        FunctionName=lambda_name,
        InvocationType="Event",
        Payload=payload
    )

    return {
        "statusCode": 200,
        "body": json.dumps({"message": "ok"})
    }