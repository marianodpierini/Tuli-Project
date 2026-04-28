import json
import os
import boto3
import botocore
import uuid
import jwt
import hashlib
import requests
import time
from boto3.dynamodb.conditions import Attr

from datetime import datetime
from logging import Logger

from google.oauth2 import service_account
import google.auth.transport.requests

from core.improved_context_classes import (
    EnhancedUserContext,
    CustomJSONEncoder,
    LanggraphManager,
)
from core.request_handler import APIGatewayModel, RequestHandler
from core.agents.rag_agent import RagAgent
import os
from core.agents.sql_agent import SqlAgent

from core.database.db import SessionLocal

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "https://front-app-ia.s3.us-east-1.amazonaws.com",
    "Access-Control-Allow-Credentials": "true",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST",
}

STOP_WORDS = {
    "en",
    "la",
    "el",
    "los",
    "las",
    "que",
    "de",
    "a",
    "y",
    "o",
    "un",
    "una",
    "?",
}

LIMITE_TOKENS_DIARIO = 550000

dynamodb = boto3.resource("dynamodb")
user_questions_table = dynamodb.Table("user_questions_table")
user_table = dynamodb.Table("users_notifications_table")
usage_table = dynamodb.Table("user_usage_tokens")
agent_responses_feedback = dynamodb.Table("agent_responses_feedback")


class ApiRequestHandler(RequestHandler):
    def __init__(
        self, logger: Logger, req_id: str, event: APIGatewayModel, lambda_handler
    ):
        self.event = event
        super().__init__(logger, req_id, event, lambda_handler)

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
        body = {"text": text}
        r = requests.post(url, headers=headers, json=body)
        if r.status_code != 200:
            print("Error al enviar mensaje:", r.text)
        return r.json()

    def format_response_for_api(
        self, resource, http_method, status_code, data, request_context
    ):
        json_body = json.dumps(data, cls=CustomJSONEncoder)
        return {
            "messageVersion": "1.0",
            "response": {
                "resource": resource,
                "httpMethod": http_method,
                "httpStatusCode": status_code,
                "responseBody": {"application/json": {"body": json_body}},
            },
            "requestContext": request_context,
        }

    def get_user_context(self):
        identity = self.event.request_context.get("identity", {})

        token = (
            self.event.authorization.replace("Bearer ", "")
            if self.event.authorization
            else None
        )
        user_email = None
        username = None
        session_id = None
        work_area = None
        canal = ""
        nickname = ""
        name = ""
        from_num = None
        space_name = None
        source = self.event.source

        if token and source not in ["whatsapp", "google_chat"]:
            decoded = jwt.decode(token, options={"verify_signature": False})

            user_email = decoded.get("email")
            username = decoded.get("cognito:username")
            sub = decoded.get("sub", "")
            session_id = f"{sub}_frontend"
            work_area = decoded.get("custom:work_area")
            canal = "frontend"

            response = user_table.scan(
                ProjectionExpression="apodo, contexto_usuario, nombre",
                FilterExpression=Attr("email").eq(user_email),
            )

            items = response.get("Items", [])

            nickname = items[0]["apodo"] if items else None
            name = items[0]["nombre"]

        if "google_chat" in source and session_id is None:
            canal = "google_chat"
            user_email = self.event.body["email"]
            space_name = self.event.body["space_name"]

            key = f"{user_email}_google"
            session_id = hashlib.sha256(key.encode("utf-8")).hexdigest()

            response = user_table.scan(
                ProjectionExpression="apodo, contexto_usuario, nombre",
                FilterExpression=Attr("email").eq(user_email),
            )

            items = response.get("Items", [])

            if "space_name" not in items[0].keys():
                user_table.update_item(
                    Key={"nombre": items[0]["nombre"]},
                    UpdateExpression="SET space_name = :nuevo",
                    ExpressionAttributeValues={
                        ":nuevo": space_name,
                    },
                )

            nickname = items[0]["apodo"] if items else None
            name = items[0]["nombre"]

        if not session_id:
            session_id = str(uuid.uuid4())[:8]

        if token is None:
            user_id = "anonymous"
        else:
            user_id = self.event.authorization.split(" ")[-1] or "anonymous"

        ip_address = (
            self.event.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or self.event.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or identity.get("sourceIp")
            or "unknown"
        )

        user_agent = (
            self.event.headers.get("user-agent")
            or self.event.headers.get("User-Agent")
            or identity.get("userAgent")
            or "unknown"
        )

        session_id_validated = self.validate_user_session(session_id, canal, 30)

        user_context = EnhancedUserContext(
            user_id,
            session_id_validated,
            ip_address,
            user_agent,
            user_email,
            username,
            nickname,
            from_num,
            name,
            work_area,
        )

        return user_context

    def normalize_question(self, question: str) -> str:
        tokens = [
            word.lower() for word in question.split() if word.lower() not in STOP_WORDS
        ]
        return " ".join(tokens)

    def is_message_valid(self, text):
        prohibited_words = ["palabra_prohibida1", "palabra_prohibida2"]
        return not any(word in text.lower() for word in prohibited_words)

    def save_user_question(self, question: str):
        now = datetime.now().isoformat()
        standardized_question = self.normalize_question(question)
        question_id = hashlib.md5(standardized_question.encode()).hexdigest()

        user_id = (
            self.user_context.user_email
            if self.user_context.user_email is not None
            else self.event.body["raw"]["ProfileName"][0]
        )

        response = user_questions_table.update_item(
            Key={"user_id": user_id, "question_id": question_id},
            UpdateExpression="""
                SET #c = if_not_exists(#c, :start) + :inc,
                    question = :q,
                    last_asked = :now
            """,
            ExpressionAttributeNames={"#c": "count"},
            ExpressionAttributeValues={
                ":start": 0,
                ":inc": 1,
                ":q": question,
                ":now": now,
            },
            ReturnValues="UPDATED_NEW",
        )

        return response

    def handle_event(self):
        try:
            start = time.perf_counter()
            source = self.event.source
            validate_tokens = self.user_context.validate_use_tokens(source)
            if not validate_tokens:
                self.send_google_chat_message(
                    self.event.body["space_name"],
                    "Alcanzaste el limite de tokens por el dia de hoy...",
                )
                return True

            self.logger.info("[SOURCE] Evento recibido desde Google Chat")
            conversation_history = self.event.body["text"]
            last_message = self.event.body["text"]

            if not conversation_history:
                return {
                    "statusCode": 400,
                    "headers": CORS_HEADERS,
                    "body": json.dumps({"error": "No se proporcionó historial."}),
                }

            config = botocore.config.Config(
                connect_timeout=30,
                read_timeout=120,  # Aumentar si las respuestas del agente tardan
                retries={"max_attempts": 3, "mode": "standard"},
            )

            client = boto3.client(
                "bedrock-agent-runtime", region_name="us-east-1", config=config
            )

            self.save_user_question(last_message)

            sql_agent = SqlAgent(
                self.logger,
                self.user_context,
                client,
                agent_responses_feedback,
                config,
                self.user_context.session_id,
                SessionLocal,
                source,
            )
            rag_agent = RagAgent(
                self.logger,
                self.user_context,
                client,
                agent_responses_feedback,
                config,
                self.user_context.session_id,
                SessionLocal,
                source,
            )

            langgraph_manager = LanggraphManager(self.logger, sql_agent, rag_agent)

            response = langgraph_manager.graph.invoke(
                {
                    "question": conversation_history,
                    "needs_sql": False,
                    "needs_rag": False,
                    "sql_result": None,
                    "rag_result": None,
                    "final_answer": None,
                }
            )

            self.send_google_chat_message(self.event.body["space_name"], response)

            end = time.perf_counter()

            total_ms_lambda = (end - start) * 1000

            # self.user_context.metrics_questions(total_ms, total_tokens, total_steps, input_to_metrics, str(total_ms_lambda).split(".")[0])

        except Exception as e:
            self.logger.error(f"Error en API Gateway: {str(e)}")
            return {
                "statusCode": 500,
                "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": str(e)}),
            }
