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

    return {"statusCode": 200, "body": "EVENT_RECEIVED"}
