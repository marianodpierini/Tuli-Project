import json
import boto3
import os

from urllib.parse import parse_qs

lambda_client = boto3.client("lambda")
PROCESSING_LAMBDA = os.environ["PROCESSING_LAMBDA_NAME"]
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "miverifytoken123")

def lambda_handler(event, context):
    http_method = event.get("httpMethod", "")

    if http_method == "POST":
        headers = event.get("headers", {})
        raw_body = event.get("body", "")
        is_base64 = event.get("isBase64Encoded", False)

        if is_base64 and isinstance(raw_body, str):
            import base64
            raw_body = base64.b64decode(raw_body).decode("utf-8")

        if headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
            params = parse_qs(raw_body)

            message = params.get("Body", [""])[0]
            from_number = params.get("From", [""])[0]
            to_number = params.get("To", [""])[0]

            enriched_event = {
                "resource": "/webhooks/whatsapp",
                "path": "/webhooks/whatsapp",
                "httpMethod": "POST",
                "headers": headers,
                "multiValueHeaders": {k: [v] if not isinstance(v, list) else v for k, v in headers.items()},
                "queryStringParameters": event.get("queryStringParameters"),
                "multiValueQueryStringParameters": event.get("multiValueQueryStringParameters"),
                "pathParameters": event.get("pathParameters"),
                "stageVariables": event.get("stageVariables"),
                "requestContext": event.get("requestContext", {}),
                "body": json.dumps({
                    "from": from_number,
                    "to": to_number,
                    "message": message,
                    "raw": params
                }),
                "isBase64Encoded": False,
                "source": "whatsapp"
            }

        else:
            enriched_event = dict(event)
            enriched_event["source"] = "whatsapp_meta"

        try:
            lambda_client.invoke(
                FunctionName=PROCESSING_LAMBDA,
                InvocationType="Event",
                Payload=json.dumps(enriched_event)
            )
        except Exception as e:
            print(f"Error invocando la lambda de procesamiento: {e}")

        return {"statusCode": 200, "body": "EVENT_RECEIVED"}

    return {"statusCode": 404, "body": "Not Found"}
