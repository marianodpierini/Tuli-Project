import json
import os
import boto3
import botocore
import hashlib
from datetime import date

from sqlalchemy import text
from logging import Logger
from twilio.rest import Client

from core.database.db import SessionLocal

dynamodb = boto3.resource('dynamodb')
user_table = dynamodb.Table('users_table')

class ScheduledHandler:
    def __init__(self, logger: Logger):
        self.logger = logger

    def send_whatsapp(self, text, from_number, to_number):
        self.logger.info("Enviando respuesta a twilio...")
        client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

        return client.messages.create(
            from_=from_number,
            to=to_number,
            body=text
        )

    def get_users(self):
        response = user_table.scan(
            ProjectionExpression="nombre, email, frecuencia, num_telefono, querys, ultima_vez"
        )
        items = response.get("Items", [])

        return items
    
    def ask_question(self, question, query, session_id):
        config = botocore.config.Config(
            connect_timeout=30,
            read_timeout=120,  # Aumentar si las respuestas del agente tardan
            retries={
                'max_attempts': 3,
                'mode': 'standard'
            }
        )

        client = boto3.client('bedrock-agent-runtime', region_name='us-east-1', config=config)

        with SessionLocal() as session:
            query_results = session.execute(text(query))
            query_result_dicts = [dict(row._mapping) for row in query_results]

        input_text = f"""
                    El usuario preguntó: "{question}"

                    La consulta SQL devolvió estos resultados:
                    {json.dumps(query_result_dicts, ensure_ascii=False, indent=2)}

                    Por favor responde al usuario en lenguaje natural, breve y clara,
                    usando los resultados de la consulta.
                    """
        
        params = {
                    'agentId': 'DRSOAFDOTR',         # Reemplazá con tu agente real si hace falta
                    'agentAliasId': 'XKJTFFEMPC',    # Reemplazá si tenés otro alias
                    'sessionId': session_id,
                    'inputText': input_text,
                    'enableTrace': False,
                }

        response = client.invoke_agent(**params)

        event_stream = response.get('completion')

        if isinstance(event_stream, botocore.eventstream.EventStream):
            assistant_response = ""
            for event in event_stream:
                if 'chunk' in event:
                    chunk_data = event['chunk']['bytes'].decode('utf-8')
                    assistant_response += chunk_data

        return assistant_response.strip()

    def handle_event(self):
        users_to_questions = self.get_users()
        today = date.today()
        dict_responses = {}

        for user in users_to_questions:
            latest = date.fromisoformat(user["ultima_vez"])
            difference = today - latest
            if user["frecuencia"] <= difference.days:
                print(user)
                for query in user["querys"]:
                    key = f"{user['nombre'].strip().lower()}:{user['email'].strip().lower()}"
                    session_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
                    response = self.ask_question(query["pregunta"], query["query"], session_id)
                    if user["nombre"] not in dict_responses:
                        dict_responses[user["nombre"]] = [response]
                    else:
                        dict_responses[user["nombre"]].append(response)

                for key, value in dict_responses.items():
                    self.send_whatsapp(value, "whatsapp:+14155238886", user["num_telefono"])

                user_table.update_item(
                    Key={"nombre": user["nombre"]},
                    UpdateExpression="SET ultima_vez = :fecha",
                    ExpressionAttributeValues={":fecha": today.isoformat()},
                    ReturnValues="UPDATED_NEW"
                )