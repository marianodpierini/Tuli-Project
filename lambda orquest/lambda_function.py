import json
import boto3
import os

lambda_client = boto3.client("lambda")
PROCESSING_LAMBDA = os.environ["PROCESSING_LAMBDA_NAME"]
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "miverifytoken123")

def lambda_handler(event, context):
    http_method = event.get("httpMethod", "")
    
    if http_method == "GET":
        params = event.get("queryStringParameters", {})
        if params and params.get("hub.verify_token") == VERIFY_TOKEN:
            return {
                "statusCode": 200,
                "body": params.get("hub.challenge", "")
            }
        return {"statusCode": 403, "body": "Forbidden"}

    if http_method == "POST":
        response = {"statusCode": 200, "body": "EVENT_RECEIVED"}

        payload = {
            event: event,
            "source": "whatsapp",
        }

        try:
            lambda_client.invoke(
                FunctionName=PROCESSING_LAMBDA,
                InvocationType="Event",
                Payload=json.dumps(payload)
            )
        except Exception as e:
            print(f"Error invocando la lambda de procesamiento: {e}")

        return response

    return {"statusCode": 404, "body": "Not Found"}
