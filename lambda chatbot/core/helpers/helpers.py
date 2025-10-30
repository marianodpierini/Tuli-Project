import json
from urllib.parse import parse_qs

def normalize_event(event):
    http_method = event.get("httpMethod", "")
    path = event.get("path", "")

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
        
        elif path.endswith("/google"):
            body_json = json.loads(raw_body)
            message = (
                body_json.get("chat", {})
                    .get("messagePayload", {})
                    .get("message", {})
                    .get("text", "")
            )
            sender = (
                body_json.get("chat", {})
                    .get("messagePayload", {})
                    .get("message", {})
                    .get("sender", {})
            )
            sender_email = sender.get("email", "")
            sender_name = sender.get("displayName", "")

            message_payload = body_json.get("chat", {}).get("messagePayload", {})
            message_obj = message_payload.get("message", {})
            space_name = message_obj.get("space", {}).get("name", "")
            thread_name = message_obj.get("thread", {}).get("name", "")

            enriched_event = {
                "resource": "/webhooks/google",
                "path": "/webhooks/google",
                "httpMethod": "POST",
                "headers": headers,
                "requestContext": event.get("requestContext", {}),
                "body": json.dumps({
                    "text": message,
                    "name": sender_name,
                    "email": sender_email,
                    "space_name": space_name,
                    "thread_name": thread_name,
                    "raw": body_json,
                }),
                "isBase64Encoded": False,
                "source": "google_chat"
            }
    
    return enriched_event