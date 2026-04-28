import json
import boto3
import os
import base64
import google.cloud.pubsub_v1 as pubsub_v1

from email import policy
from email.parser import BytesParser

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from core.email_processor import EmailProcessor
from database.db import SessionLocal

s3 = boto3.client("s3")
secrets = boto3.client("secretsmanager")
bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
dynamodb = boto3.client("dynamodb")


OPERADORES_BUCKET = os.environ.get("OPERADORES_BUCKET", "aero-turi-documents")
OPERADORES_KEY = os.environ.get("OPERADORES_KEY", "lambda-files/operadores.json")
KEY_SVS_ACC = os.environ.get("SVS_ACC", "lambda-files/key_account_serv.json")

DEST_BUCKET = os.environ.get("DEST_BUCKET", "aero-turi-documents")


def cargar_operadores():
    response = s3.get_object(Bucket=OPERADORES_BUCKET, Key=OPERADORES_KEY)
    data = json.loads(response["Body"].read())
    return data


def get_last_history_id():
    response = dynamodb.get_item(TableName="gmail_state", Key={"id": {"S": "global"}})

    return response.get("Item", {}).get("last_history_id", {}).get("S")


def save_history_id(history_id):
    dynamodb.put_item(
        TableName="gmail_state",
        Item={"id": {"S": "global"}, "last_history_id": {"S": str(history_id)}},
    )


def is_message_processed(message_id):
    response = dynamodb.get_item(
        TableName="gmail_processed_messages", Key={"message_id": {"S": message_id}}
    )
    return "Item" in response


def mark_message_processed(message_id):
    dynamodb.put_item(
        TableName="gmail_processed_messages", Item={"message_id": {"S": message_id}}
    )


def get_gmail_secret():
    secret_data = secrets.get_secret_value(SecretId="gmail/token/facturas_bot")
    data = json.loads(secret_data["SecretString"])
    return json.loads(data["token_facturas_bot"])


def get_subscriber():
    response = s3.get_object(Bucket=OPERADORES_BUCKET, Key=KEY_SVS_ACC)
    content = response["Body"].read().decode("utf-8")

    creds_json = json.loads(content)

    credentials = service_account.Credentials.from_service_account_info(creds_json)

    return pubsub_v1.SubscriberClient(credentials=credentials)


def pull_messages(subscriber):
    subscription_path = subscriber.subscription_path(
        "turi-chat-476619", "gmail-facturas-topic-sub"
    )

    response = subscriber.pull(
        request={"subscription": subscription_path, "max_messages": 5}
    )

    messages = []

    for msg in response.received_messages:
        data = json.loads(msg.message.data.decode("utf-8"))

        messages.append({"data": data, "ack_id": msg.ack_id})

    return messages, subscription_path


def ack_messages(subscriber, subscription_path, ack_ids):
    if ack_ids:
        subscriber.acknowledge(
            request={"subscription": subscription_path, "ack_ids": ack_ids}
        )


def build_gmail_credentials(secret_data):
    creds = Credentials(
        token=secret_data["token"],
        refresh_token=secret_data["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=secret_data["client_id"],
        client_secret=secret_data["client_secret"],
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    return creds


def get_gmail_service(secret):
    creds = Credentials(
        token=secret["token"],
        refresh_token=secret["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=secret["client_id"],
        client_secret=secret["client_secret"],
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())

    return build("gmail", "v1", credentials=creds)


def get_message_ids(service, start_history_id):
    results = (
        service.users()
        .history()
        .list(
            userId="me", startHistoryId=start_history_id, historyTypes=["messageAdded"]
        )
        .execute()
    )

    ids = []

    for h in results.get("history", []):
        for m in h.get("messagesAdded", []):
            ids.append(m["message"]["id"])

    return ids


def get_raw_email(service, msg_id):
    msg = service.users().messages().get(userId="me", id=msg_id, format="raw").execute()

    return base64.urlsafe_b64decode(msg["raw"])


def parse_email(raw_email):
    return BytesParser(policy=policy.default).parsebytes(raw_email)


def process_email_raw(raw_email, operadores):
    parsed_msg = BytesParser(policy=policy.default).parsebytes(raw_email)

    processor = EmailProcessor(
        parsed_msg, operadores, DEST_BUCKET, s3, SessionLocal, bedrock
    )

    return processor.process_email()


def process_history(service, history_id, operadores):
    print(f"Procesando historyId: {history_id}")

    message_ids = get_message_ids(service, history_id)

    for msg_id in message_ids:
        raw_email = get_raw_email(service, msg_id)
        process_email_raw(raw_email, operadores)


def process_pubsub_messages(
    subscriber, messages, subscription_path, gmail_service, operadores
):
    last_history_id = get_last_history_id()

    if not last_history_id:
        print("Primer ejecución, guardando historyId y saliendo")
        max_history_id = max([msg["data"]["historyId"] for msg in messages])
        save_history_id(max_history_id)
        return

    ack_ids = []
    new_history_ids = []

    for msg in messages:
        history_id = msg["data"].get("historyId")

        if not history_id:
            continue

        print(f"Procesando historyId: {history_id}")

        message_ids = get_message_ids(gmail_service, last_history_id)

        for msg_id in message_ids:
            if is_message_processed(msg_id):
                print(f"Mensaje ya procesado: {msg_id}")
                continue

            raw_email = get_raw_email(gmail_service, msg_id)

            parsed_msg = BytesParser(policy=policy.default).parsebytes(raw_email)

            email_processor = EmailProcessor(
                parsed_msg, operadores, DEST_BUCKET, s3, SessionLocal, bedrock
            )

            email_processor.process_email()

            mark_message_processed(msg_id)

        new_history_ids.append(int(history_id))
        ack_ids.append(msg["ack_id"])

    if new_history_ids:
        max_history_id = str(max(new_history_ids))
        save_history_id(max_history_id)
        print(f"Nuevo historyId guardado: {max_history_id}")

    ack_messages(subscriber, subscription_path, ack_ids)


def lambda_handler(event, context):
    try:
        subscriber = get_subscriber()

        messages, subscription_path = pull_messages(subscriber)

        if not messages:
            print("No hay mensajes nuevos")
            return

        gmail_secret = get_gmail_secret()
        gmail_service = get_gmail_service(gmail_secret)

        operadores = cargar_operadores()

        process_pubsub_messages(
            subscriber, messages, subscription_path, gmail_service, operadores
        )

        return {"statusCode": 200, "body": "Procesado correctamente"}

    except Exception as e:
        print("ERROR:", str(e))
        raise e
