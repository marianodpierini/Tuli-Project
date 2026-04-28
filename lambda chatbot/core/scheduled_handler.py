import json
import os
from typing import List, Optional
import boto3
import botocore
import hashlib
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from logging import Logger
from twilio.rest import Client
from boto3.dynamodb.conditions import Attr

from google.oauth2 import service_account
import google.auth.transport.requests
import requests

from core.database.db import SessionLocal

dynamodb = boto3.resource("dynamodb")
user_table = dynamodb.Table("users_notifications_table")
users_sessions_table = dynamodb.Table("users_sessions_table")


class ScheduledHandler:
    def __init__(self, logger: Logger, params: Optional[List] = None):
        self.list_users = params
        self.logger = logger

    def validate_user_session(self, session_id, canal, ttl_hours=30):
        response = users_sessions_table.get_item(Key={"session_id": session_id})
        item = response.get("Item")

        now = datetime.now(timezone.utc)
        current_time = int(now.timestamp())

        expires_at = int((now + timedelta(minutes=ttl_hours)).timestamp())

        if item and item["expires_at"] > current_time:
            return item["session_id"]

        users_sessions_table.put_item(
            Item={"session_id": session_id, "channel": canal, "expires_at": expires_at}
        )
        return session_id

    def send_whatsapp(self, text, from_number, to_number):
        self.logger.info("Enviando respuesta a twilio...")
        client = Client(
            os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
        )

        return client.messages.create(from_=from_number, to=to_number, body=text)

    def get_chat_token(self):
        service_account_info = json.loads(
            os.environ["GOOGLE_CHAT_SERVICE"],
        )
        creds = service_account.Credentials.from_service_account_info(
            service_account_info, scopes=["https://www.googleapis.com/auth/chat.bot"]
        )
        request = google.auth.transport.requests.Request()
        creds.refresh(request)
        return creds.token

    def send_google_chat_message(self, space_name, text):
        token = self.get_chat_token()
        url = f"https://chat.googleapis.com/v1/{space_name}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        body = {"text": text[0]}
        r = requests.post(url, headers=headers, json=body)
        if r.status_code != 200:
            print("Error al enviar mensaje:", r.text)
        return r.json()

    def get_users(self):
        if self.list_users is not None:
            response = user_table.scan(
                ProjectionExpression="nombre, email, frecuencia, num_telefono, querys, ultima_vez, apodo, contexto_usuario, space_name",
                FilterExpression=Attr("nombre").is_in(self.list_users),
            )

            items = response["Items"]
        else:
            response = user_table.scan(
                ProjectionExpression="nombre, email, frecuencia, num_telefono, querys, ultima_vez, apodo, contexto_usuario, space_name"
            )
            items = response.get("Items", [])

        return items

    def ask_question(self, question, query, session_id, apodo, contexto_usuario):
        config = botocore.config.Config(
            connect_timeout=30,
            read_timeout=120,  # Aumentar si las respuestas del agente tardan
            retries={"max_attempts": 3, "mode": "standard"},
        )

        client = boto3.client(
            "bedrock-agent-runtime", region_name="us-east-1", config=config
        )

        with SessionLocal() as session:
            query_results = session.execute(text(query))
            query_result_dicts = [dict(row._mapping) for row in query_results]

        input_text = f"""
                    El usuario preguntó: "{question}"

                    La consulta SQL devolvió estos resultados:
                    {json.dumps(query_result_dicts, ensure_ascii=False, indent=2)}

                    Por favor responde al usuario en lenguaje natural, breve y clara,
                    usando los resultados de la consulta, utiliza su apodo que es {apodo}.

                    Utiliza un lenguaje mas relajado, no tan formal.

                    Tene en cuenta para algunas preguntas sobre el dia o fechat actual que hoy es {date.today().isoformat()}.
                    """

        params = {
            "agentId": "DRSOAFDOTR",  # Reemplazá con tu agente real si hace falta
            "agentAliasId": "XKJTFFEMPC",  # Reemplazá si tenés otro alias
            "sessionId": session_id,
            "inputText": input_text,
            "enableTrace": False,
        }

        response = client.invoke_agent(**params)

        event_stream = response.get("completion")

        if isinstance(event_stream, botocore.eventstream.EventStream):
            assistant_response = ""
            for event in event_stream:
                if "chunk" in event:
                    chunk_data = event["chunk"]["bytes"].decode("utf-8")
                    assistant_response += chunk_data

        return assistant_response.strip()

    def handle_event(self):
        users_to_questions = self.get_users()
        today = date.today()
        dict_responses_wsp = {}
        dict_responses_google = {}

        for user in users_to_questions:
            latest = date.fromisoformat(user["ultima_vez"])
            difference = today - latest
            if user["frecuencia"] <= difference.days:
                for query in user["querys"]:
                    num = user["num_telefono"]
                    key_wsp = f"{num}_whatsapp"
                    session_id_wsp = hashlib.sha256(key_wsp.encode("utf-8")).hexdigest()
                    session_id_validated = self.validate_user_session(
                        session_id_wsp, "whatsapp", 30
                    )
                    response = self.ask_question(
                        query["pregunta"],
                        query["query"],
                        session_id_validated,
                        user["apodo"],
                        user["contexto_usuario"],
                    )
                    if user["nombre"] not in dict_responses_wsp:
                        dict_responses_wsp[user["nombre"]] = [response]
                    else:
                        dict_responses_wsp[user["nombre"]].append(response)

                    email = user["email"]
                    key_gc = f"{email}_google"
                    session_id_gc = hashlib.sha256(key_gc.encode("utf-8")).hexdigest()
                    session_id_validated_gc = self.validate_user_session(
                        session_id_gc, "google_chat", 30
                    )
                    response = self.ask_question(
                        query["pregunta"],
                        query["query"],
                        session_id_validated_gc,
                        user["apodo"],
                        user["contexto_usuario"],
                    )
                    space_name = user["space_name"] if "space_name" in user else ""
                    key_dict_gc = f"{user['nombre']}_{space_name}"
                    if key_dict_gc not in dict_responses_google:
                        dict_responses_google[key_dict_gc] = [response]
                    else:
                        dict_responses_google[key_dict_gc].append(response)

                for key, value in dict_responses_wsp.items():
                    self.send_whatsapp(
                        value, "whatsapp:+14155238886", user["num_telefono"]
                    )

                for key, value in dict_responses_google.items():
                    data_key = key.split("_")
                    self.send_google_chat_message(data_key[1], value)

                user_table.update_item(
                    Key={"nombre": user["nombre"]},
                    UpdateExpression="SET ultima_vez = :fecha",
                    ExpressionAttributeValues={":fecha": today.isoformat()},
                    ReturnValues="UPDATED_NEW",
                )
