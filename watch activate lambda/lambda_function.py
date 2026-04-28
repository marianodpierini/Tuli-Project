import json
import boto3

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

secrets = boto3.client("secretsmanager")


def get_secret():
    secret_data = secrets.get_secret_value(SecretId="gmail/token/facturas_bot")
    data = json.loads(secret_data["SecretString"])
    return json.loads(data["token_facturas_bot"])


def build_credentials(data):
    creds = Credentials(
        token=data["token"],
        refresh_token=data["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=SCOPES,
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    return creds


def activar_watch(creds):
    service = build("gmail", "v1", credentials=creds)

    request = {
        "labelIds": ["INBOX"],
        "topicName": "projects/turi-chat-476619/topics/gmail-facturas-topic",
    }

    response = service.users().watch(userId="me", body=request).execute()

    print("Watch activado correctamente")
    print(response)


def lambda_handler(event, context):
    try:
        secret_data = get_secret()
        creds = build_credentials(secret_data)
        activar_watch(creds)

        return {"statusCode": 200, "body": "Watch renovado correctamente"}

    except Exception as e:
        print("ERROR:", str(e))
        raise e
