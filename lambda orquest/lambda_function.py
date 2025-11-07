import json
import boto3
import os


PROCESSING_LAMBDA = os.environ["PROCESSING_LAMBDA_NAME"]

def lambda_handler(event, context):
    lambda_client = boto3.client("lambda")

    lambda_client.invoke(
        FunctionName=PROCESSING_LAMBDA,
        InvocationType="Event",
        Payload=json.dumps(event)
    )

    return {
        "statusCode": 200,
        "body": json.dumps({"message": "ok"})
    }