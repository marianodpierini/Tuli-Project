import json
import boto3
import os
import google.cloud.pubsub_v1 as pubsub_v1

import base64
from email import policy
from email.parser import BytesParser
from typing import List, Dict, Any, Tuple

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

from core.email_processor import EmailProcessor
from database.db import SessionLocal

OPERADORES_BUCKET = os.environ.get("OPERADORES_BUCKET", "aero-turi-documents")
OPERADORES_KEY = os.environ.get("OPERADORES_KEY", "lambda-files/operadores.json")
KEY_SVS_ACC = os.environ.get("SVS_ACC", "lambda-files/key_account_serv.json")
DEST_BUCKET = os.environ.get("DEST_BUCKET", "aero-turi-documents")


class ConfigService:
    def __init__(self, s3_client):
        self.s3_client = s3_client

    def load_operators(self) -> Dict[str, Any]:
        print(f"Cargando operadores desde s3://{OPERADORES_BUCKET}/{OPERADORES_KEY}")
        response = self.s3_client.get_object(Bucket=OPERADORES_BUCKET, Key=OPERADORES_KEY)
        return json.loads(response["Body"].read())

    def get_service_account_credentials(self) -> Dict[str, Any]:
        print(f"Cargando credenciales de servicio desde s3://{OPERADORES_BUCKET}/{KEY_SVS_ACC}")
        response = self.s3_client.get_object(Bucket=OPERADORES_BUCKET, Key=KEY_SVS_ACC)
        content = response["Body"].read().decode("utf-8")
        return json.loads(content)


class GmailStateRepository:
    def __init__(self, dynamodb_client, table_name_state="gmail_state", table_name_processed="gmail_processed_messages"):
        self.dynamodb_client = dynamodb_client
        self.table_name_state = table_name_state
        self.table_name_processed = table_name_processed

    def get_last_history_id(self) -> str | None:
        response = self.dynamodb_client.get_item(TableName=self.table_name_state, Key={"id": {"S": "global"}})
        return response.get("Item", {}).get("last_history_id", {}).get("S")

    def save_history_id(self, history_id: str):
        self.dynamodb_client.put_item(
            TableName=self.table_name_state,
            Item={"id": {"S": "global"}, "last_history_id": {"S": history_id}},
        )
        print(f"History ID guardado: {history_id}")

    def is_message_processed(self, message_id: str) -> bool:
        response = self.dynamodb_client.get_item(
            TableName=self.table_name_processed, Key={"message_id": {"S": message_id}}
        )
        return "Item" in response

    def mark_message_processed(self, message_id: str):
        self.dynamodb_client.put_item(
            TableName=self.table_name_processed, Item={"message_id": {"S": message_id}}
        )
        print(f"Mensaje marcado como procesado: {message_id}")


class PubSubService:
    def __init__(self, service_account_credentials, project_id="turi-chat-476619", subscription_name="gmail-facturas-topic-sub"):
        self.credentials = service_account.Credentials.from_service_account_info(service_account_credentials)
        self.subscriber = pubsub_v1.SubscriberClient(credentials=self.credentials)
        self.subscription_path = self.subscriber.subscription_path(project_id, subscription_name)

    def pull_messages(self, max_messages: int = 5) -> Tuple[List[Dict[str, Any]], str]:
        print(f"Pulling messages from {self.subscription_path}...")
        response = self.subscriber.pull(
            request={"subscription": self.subscription_path, "max_messages": max_messages}
        )
        messages = []
        for msg in response.received_messages:
            try:
                data = json.loads(msg.message.data.decode("utf-8"))
                messages.append({"data": data, "ack_id": msg.ack_id})
            except json.JSONDecodeError as e:
                print(f"Error decoding Pub/Sub message: {e} - Data: {msg.message.data}")
        return messages, self.subscription_path

    def ack_messages(self, ack_ids: List[str]):
        if ack_ids:
            print(f"Acknowledging {len(ack_ids)} messages.")
            self.subscriber.acknowledge(
                request={"subscription": self.subscription_path, "ack_ids": ack_ids}
            )


class GmailService:
    def __init__(self, secrets_client, secret_id="gmail/token/facturas_bot"):
        self.secrets_client = secrets_client
        self.secret_id = secret_id
        self._gmail_service = None

    def _build_credentials(self) -> Credentials:
        secret_data = self.secrets_client.get_secret_value(SecretId=self.secret_id)
        data = json.loads(secret_data["SecretString"])
        gmail_token_data = json.loads(data["token_facturas_bot"])

        creds = Credentials(
            token=gmail_token_data["token"],
            refresh_token=gmail_token_data["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=gmail_token_data["client_id"],
            client_secret=gmail_token_data["client_secret"],
        )

        if creds.expired and creds.refresh_token:
            print("Refreshing Gmail API credentials...")
            creds.refresh(Request())

        return creds

    def get_service(self):
        if not self._gmail_service:
            creds = self._build_credentials()
            self._gmail_service = build("gmail", "v1", credentials=creds)
        return self._gmail_service

    def get_message_ids_from_history(self, start_history_id: str) -> Tuple[List[str], str | None]:
        service = self.get_service()
        print(f"Fetching Gmail history from ID: {start_history_id}")
        results = (
            service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"]
            )
            .execute()
        )

        ids = []
        for h in results.get("history", []):
            for m in h.get("messagesAdded", []):
                ids.append(m["message"]["id"])

        latest_history_id = results.get("historyId")
        print(f"Found {len(ids)} new message IDs. Latest history ID: {latest_history_id}")
        return ids, latest_history_id

    def get_raw_email(self, msg_id: str) -> bytes:
        service = self.get_service()
        msg = service.users().messages().get(userId="me", id=msg_id, format="raw").execute()
        return base64.urlsafe_b64decode(msg["raw"])


class InvoiceExtractionOrchestrator:
    def __init__(self, config_service: ConfigService, state_repo: GmailStateRepository,
                 pubsub_service: PubSubService, gmail_service: GmailService,
                 s3_client, bedrock_client, db_session_factory):
        self.config_service = config_service
        self.state_repo = state_repo
        self.pubsub_service = pubsub_service
        self.gmail_service = gmail_service
        self.s3_client = s3_client
        self.bedrock_client = bedrock_client
        self.db_session_factory = db_session_factory

    def process_pubsub_messages(self, messages: List[Dict[str, Any]], subscription_path: str):
        operadores = self.config_service.load_operators()
        last_history_id = self.state_repo.get_last_history_id()

        if not last_history_id:
            print("Primer ejecución o historyId no encontrado. Guardando el más alto de los mensajes actuales y saliendo.")
            if messages:
                max_history_id = max([msg["data"]["historyId"] for msg in messages])
                self.state_repo.save_history_id(str(max_history_id))
            ack_ids = [m["ack_id"] for m in messages]
            self.pubsub_service.ack_messages(ack_ids)
            return
        
        message_ids, latest_gmail_history_id = self.gmail_service.get_message_ids_from_history(last_history_id)

        if not message_ids:
            print("No hay nuevos emails en Gmail history.")
            if latest_gmail_history_id:
                self.state_repo.save_history_id(str(latest_gmail_history_id))
            ack_ids = [m["ack_id"] for m in messages]
            self.pubsub_service.ack_messages(ack_ids)
            return

        for msg_id in message_ids:
            if self.state_repo.is_message_processed(msg_id):
                print(f"Mensaje ya procesado: {msg_id}")
                continue

            try:
                raw_email = self.gmail_service.get_raw_email(msg_id)
                parsed_msg = BytesParser(policy=policy.default).parsebytes(raw_email)

                email_processor = EmailProcessor(
                    parsed_msg, operadores, DEST_BUCKET, self.s3_client, self.db_session_factory, self.bedrock_client, msg_id
                )
                email_processor.process_email()
                self.state_repo.mark_message_processed(msg_id)
            except Exception as e:
                print(f"Error processing message {msg_id}: {e}")

        if latest_gmail_history_id:
            self.state_repo.save_history_id(str(latest_gmail_history_id))

        ack_ids = [m["ack_id"] for m in messages]
        self.pubsub_service.ack_messages(ack_ids)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    s3_client = boto3.client("s3")
    secrets_client = boto3.client("secretsmanager")
    bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
    dynamodb_client = boto3.client("dynamodb")

    try:
        config_service = ConfigService(s3_client)
        service_account_creds = config_service.get_service_account_credentials()

        pubsub_service = PubSubService(service_account_creds)
        messages, subscription_path = pubsub_service.pull_messages()

        if not messages:
            print("No hay mensajes nuevos de Pub/Sub.")
            return {"statusCode": 200, "body": "No new messages to process."}

        state_repo = GmailStateRepository(dynamodb_client)
        gmail_service = GmailService(secrets_client)

        orchestrator = InvoiceExtractionOrchestrator(
            config_service, state_repo, pubsub_service, gmail_service,
            s3_client, bedrock_client, SessionLocal
        )

        orchestrator.process_pubsub_messages(messages, subscription_path)

        return {"statusCode": 200, "body": "Procesado correctamente"}

    except Exception as e:
        print(f"ERROR en lambda_handler: {e}")
        import traceback
        traceback.print_exc()
        raise e
