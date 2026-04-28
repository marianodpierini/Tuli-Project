import json
import os
from datetime import date, timedelta, timezone
import unicodedata
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

from twilio.rest import Client
from google.oauth2 import service_account
import google.auth.transport.requests

from core.improved_context_classes import EnhancedUserContext, CustomJSONEncoder
from core.request_handler import APIGatewayModel, RequestHandler

from core.database.db import SessionLocal
from core.database.models import SuggestedQuestions
from core.helpers.helpers import valite_existing_response, is_context_independent_heuristic, classify_with_bedrock, get_agent_id, titan_embed

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "https://front-app-ia.s3.us-east-1.amazonaws.com",
    "Access-Control-Allow-Credentials": "true",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST"
}

STOP_WORDS = {"en", "la", "el", "los", "las", "que", "de", "a", "y", "o", "un", "una", "?"}


KEYWORDS_USER_CONTEXT = [
        "mis", "mi", "yo", "voy", "hice", "hago", "personales", "personal"
        "objetivos",
    ]


LIMITE_TOKENS_DIARIO = 550000

dynamodb = boto3.resource('dynamodb')
user_questions_table = dynamodb.Table('user_questions_table')
user_table = dynamodb.Table('users_notifications_table')
usage_table = dynamodb.Table("user_usage_tokens")
agent_responses_feedback = dynamodb.Table("agent_responses_feedback")

class ApiRequestHandler(RequestHandler):
    def __init__(self, logger: Logger, req_id: str, event: APIGatewayModel, lambda_handler):
        self.event = event
        super().__init__(logger, req_id, event, lambda_handler)

    def send_whatsapp(self, text):
        self.logger.info("Enviando respuesta a twilio...")
        client = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

        from_number = self.event.body["to"]
        to_number = self.event.body["from"]

        return client.messages.create(
            from_=from_number,
            to=to_number,
            body=text
        )

    def get_chat_token(self):
        service_account_info = json.loads(os.environ["GOOGLE_CHAT_SERVICE"],)
        creds = service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=["https://www.googleapis.com/auth/chat.bot"]
        )
        request = google.auth.transport.requests.Request()
        creds.refresh(request)
        return creds.token

    def send_google_chat_message(self, space_name, text):
        token = self.get_chat_token()
        url = f"https://chat.googleapis.com/v1/{space_name}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        body = {"text": text}
        r = requests.post(url, headers=headers, json=body)
        if r.status_code != 200:
            print("Error al enviar mensaje:", r.text)
        return r.json()

    def format_response_for_api(self, resource, http_method, status_code, data, request_context):
        json_body = json.dumps(data, cls=CustomJSONEncoder)
        return {
            "messageVersion": "1.0",
            "response": {
                "resource": resource,
                "httpMethod": http_method,
                "httpStatusCode": status_code,
                "responseBody": {
                    "application/json": {
                        "body": json_body
                    }
                }
            },
            "requestContext": request_context,
        }

    def get_user_context(self):
        identity = self.event.request_context.get('identity', {})

        token = self.event.authorization.replace("Bearer ", "") if self.event.authorization else None
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
                FilterExpression=Attr("email").eq(user_email)
            )

            items = response.get("Items", [])

            nickname = items[0]["apodo"] if items else None
            name = items[0]["nombre"]

        if "whatsapp" in source and session_id is None:
            from_num = self.event.body["from"]
            key = f"{from_num}_whatsapp"
            session_id = hashlib.sha256(key.encode("utf-8")).hexdigest()
            canal = "whatsapp"

            response = user_table.scan(
                ProjectionExpression="apodo, contexto_usuario, nombre",
                FilterExpression=Attr("num_telefono").eq(from_num)
            )

            items = response.get("Items", [])

            nickname = items[0]["apodo"] if items else None
            name = items[0]["nombre"]

        if "google_chat" in source and session_id is None:
            canal = "google_chat"
            user_email = f"{self.event.body['email']}_test"
            space_name = self.event.body["space_name"]

            key = f"{user_email}_google"
            session_id = hashlib.sha256(key.encode("utf-8")).hexdigest()

            response = user_table.scan(
                ProjectionExpression="apodo, contexto_usuario, nombre",
                FilterExpression=Attr("email").eq(user_email)
            )

            items = response.get("Items", [])

            if "space_name" not in items[0].keys():
                user_table.update_item(
                Key={"nombre": items[0]["nombre"]},
                UpdateExpression="SET space_name = :nuevo",
                ExpressionAttributeValues={
                    ":nuevo": space_name,
                }
            )

            nickname = items[0]["apodo"] if items else None
            name = items[0]["nombre"]

        if not session_id:
            session_id = str(uuid.uuid4())[:8]
        
        if token is None:
            user_id = "anonymous"
        else:
            user_id = (
                self.event.authorization.split(' ')[-1] or
                "anonymous"
            )
        
        ip_address = (
            self.event.headers.get('x-forwarded-for', '').split(',')[0].strip() or
            self.event.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
            identity.get('sourceIp') or
            "unknown"
        )
        
        user_agent = (
            self.event.headers.get('user-agent') or
            self.event.headers.get('User-Agent') or
            identity.get('userAgent') or
            "unknown"
        )

        session_id_validated = self.validate_user_session(session_id, canal, 30)
        
        user_context = EnhancedUserContext(user_id, session_id_validated, ip_address, user_agent, user_email, username, nickname, from_num, name, work_area)
        
        return user_context
  
    def clean_input_text(self, text):
        text = ''.join(filter(str.isprintable, text))
        return text

    def normalize_text(self, text):
        return unicodedata.normalize('NFKC', text)
    
    def normalize_question(self, question: str) -> str:
        tokens = [
            word.lower()
            for word in question.split()
            if word.lower() not in STOP_WORDS
        ]
        return " ".join(tokens)
    
    def is_message_valid(self, text):
        prohibited_words = ['palabra_prohibida1', 'palabra_prohibida2']
        return not any(word in text.lower() for word in prohibited_words)
    
    def save_user_question(self, question: str):
        now = datetime.now().isoformat()
        standardized_question = self.normalize_question(question)
        question_id = hashlib.md5(standardized_question.encode()).hexdigest()

        user_id = self.user_context.user_email if self.user_context.user_email is not None else self.event.body["raw"]["ProfileName"][0]

        response = user_questions_table.update_item(
            Key={
                'user_id': user_id,
                'question_id': question_id
            },
            UpdateExpression="""
                SET #c = if_not_exists(#c, :start) + :inc,
                    question = :q,
                    last_asked = :now
            """,
            ExpressionAttributeNames={
                '#c': 'count'
            },
            ExpressionAttributeValues={
                ':start': 0,
                ':inc': 1,
                ':q': question,
                ':now': now
            },
            ReturnValues="UPDATED_NEW"
        )

        return response
    
    def process_conversation_with_bedrock(self, conversation_history, session_id, db_data=""):
        """
        Envía el último mensaje de 'conversation_history' + un contexto adicional con
        datos ('db_data') al agente de Bedrock, e implementa logs y validaciones para
        diagnosticar problemas de payload.
        """

        config = botocore.config.Config(
            connect_timeout=30,
            read_timeout=120,  # Aumentar si las respuestas del agente tardan
            retries={
                'max_attempts': 3,
                'mode': 'standard'
            }
        )

        client = boto3.client('bedrock-agent-runtime', region_name='us-east-1', config=config)

        agent_id = get_agent_id(self.user_context.user_email)

        # 1. Extrae el último mensaje
        source = self.event.source
        if "whatsapp" or "google_chat" in source:
            last_message = conversation_history
        else:
            last_message = conversation_history[-1]['content']

        # 2. Log para diagnosticar tamaño y snippet
        self.logger.info(f"[Bedrock] Mensaje final enviado (len={len(last_message)}): {repr(last_message)}")

        # 3. Inyecta datos de la BD si están disponibles
        if db_data:
            db_data = db_data[:800] + "..." if len(db_data) > 800 else db_data
            last_message += f"\n\n[Datos de la Base de Datos]\n{db_data}"

        # 4. Control de tamaño máximo - CAMBIO: Truncamos a 256 caracteres
        last_message = last_message[:256] if len(last_message) > 256 else last_message

        # 5. Normalización y validación del mensaje
        last_message = self.clean_input_text(self.normalize_text(last_message))
        if not self.is_message_valid(last_message):
            return "Mensaje contiene contenido no permitido.", None
        
        input_to_metrics = last_message
        
        self.save_user_question(last_message)

        # 6. Obtiene session_id o genera uno nuevo
        self.logger.info(f"sessionId enviado a Bedrock: {session_id}")

        # 7. Construcción de parámetros - CAMBIO: Agregamos parámetros de control

        keywords_user = [kw for kw in last_message.split() if kw.lower() in KEYWORDS_USER_CONTEXT]

        keywords_cache = [kw.lower() for kw in last_message.split() if kw.lower() not in STOP_WORDS]

        new_q_id = None

        if agent_id == "XKJTFFEMPC":

            validation = valite_existing_response(session_id, keywords_cache, last_message, config)

            if validation:
                if validation == "Pregunta sin query":
                    self.logger.info("La pregunta no tiene una query asociada.")
                else:
                    assistant_response = ""
                    event_stream = validation.get('completion')
                    
                    if isinstance(event_stream, botocore.eventstream.EventStream):
                        for event in event_stream:
                            if 'chunk' in event:
                                chunk_data = event['chunk']['bytes'].decode('utf-8')
                                assistant_response += chunk_data

                    self.logger.info(f"[AGENT RESPONSE] Respuesta del agente: {assistant_response.strip()}")
                    return assistant_response.strip(), 0, 0, 0, input_to_metrics
            else:
                save_question = False

                decision = is_context_independent_heuristic(last_message)

                if decision is True:
                    save_question = True
                elif decision is None:
                    response, total_tokens_llm = classify_with_bedrock(last_message)
                    self.user_context.update_use_tokens(source, total_tokens=total_tokens_llm)
                    if response:
                        save_question = True

                if save_question:
                    with SessionLocal() as session:
                        embedding = titan_embed(last_message, keywords_cache, config)
                        new_q = SuggestedQuestions(
                            nombre=last_message,
                            activa=True, 
                            keywords=keywords_cache,
                            embedding=embedding
                        )
                        session.add(new_q)
                        session.commit()

                        new_q_id = new_q.id

        if len(keywords_user) >= 1:
            last_message += f"""
                El usuario hizo un pregunta personal referente a el.
                Para armar la respuesta y la query, tene en cuenta su nombre que es {self.user_context.name}.
                En la query deberas utilizar los campos 'nom_pro_cli' o si este no se encuentra usar 'nom_usu' para filtrar.
            """

        if self.user_context.nickname is not None:
            last_message += f"""
                Utiliza el apodo del usuario para responder, el cual es {self.user_context.nickname}.
                Tene en cuenta para algunas preguntas sobre el dia o fecha actual que hoy es {date.today().isoformat()}
            """

        
        params = {
            'agentId': 'DRSOAFDOTR',         # Reemplazá con tu agente real si hace falta
            'agentAliasId': agent_id,    # Reemplazá si tenés otro alias
            'sessionId': session_id,
            'inputText': last_message,
            'enableTrace': True,
            'sessionState': {
                "sessionAttributes": {
                    "suggestion_id": str(new_q_id) if new_q_id else ""
                }
            }
        }

        try:
            self.logger.info(f"Enviando solicitud a Bedrock con parámetros: {params}")
            response = client.invoke_agent(**params)

            # 8. Procesar EventStream correctamente
            assistant_response = ""
            event_stream = response.get('completion')
            
            if isinstance(event_stream, botocore.eventstream.EventStream):
                total_ms = 0
                total_tokens = 0
                total_steps = 0
                for event in event_stream:
                    if 'chunk' in event:
                        try:
                            chunk_data = event['chunk']['bytes'].decode('utf-8')
                            assistant_response += chunk_data
                        except Exception as decode_error:
                            self.logger.error(f"Error decodificando chunk: {decode_error}")

                    if 'trace' in event:
                        trace_root = event["trace"].get("trace", {})
                        trace_steps = trace_root.get("traceSteps", [])

                        print(f"Trace steps: {trace_root}")

                        for step in trace_steps:
                            metadata = step.get("modelInvocationOutput", {}).get("metadata", {})
                            usage = metadata.get("usage", {})

                            input_tokens = usage.get("inputTokens", 0)
                            output_tokens = usage.get("outputTokens", 0)

                            total_tokens += input_tokens + output_tokens
                            total_steps += 1

            else:
                self.logger.error(f"Tipo inesperado en 'completion': {type(event_stream)}")

            self.logger.info(f"[AGENT RESPONSE] Respuesta del agente: {assistant_response.strip()}")
            agent_responses_feedback.put_item(Item={
                "id_thread": self.event.body["space_name"],
                "last_update_time": datetime.now().isoformat(),
                "bot_response_text": assistant_response.strip(),
                "user_question_text": input_to_metrics,
                "user": self.event.body["email"],
                "agend_id": agent_id,
                "channel": source,
                "created_at": date.today().isoformat(),
                "expires_at": int((datetime.now(timezone.utc) + timedelta(hours=24)).timestamp())
            })
            return assistant_response.strip(), total_ms, total_tokens, total_steps, input_to_metrics
        
        except botocore.exceptions.ReadTimeoutError as e:
            self.logger.error(f"Timeout al invocar agente Bedrock: {str(e)}")
            return "El agente tardó demasiado en responder. Intente nuevamente."

    def get_top_questions(limit: int=3):
        response = user_questions_table.scan(
            ProjectionExpression="question, count"
        )
        items = response.get("Items", [])

        sorted_items = sorted(items, key=lambda x: x.get("count", 0), reverse=True)

        return sorted_items[:limit]
    
    def get_active_suggestions(self, user_input: str):
        tokens = set(
            word.lower()
            for word in user_input.split()
            if word.lower() not in STOP_WORDS
        )
        coincidencias = []

        with SessionLocal() as session:
            query = session.query(SuggestedQuestions).filter(SuggestedQuestions.activa == True)
            if self.user_context.work_area:
                query = query.filter(SuggestedQuestions.categoria == self.user_context.work_area)

            results = query.order_by(SuggestedQuestions.prioridad).all()

        for result in results:
            if result.keywords:
                if any(kw.lower() in tokens for kw in result.keywords):
                    coincidencias.append(result)

        suggestions = [{
            "nombre": p.nombre,
            "descripcion": p.descripcion,
            "categoria": p.categoria
        } for p in coincidencias]

            
        return suggestions
    
    def handle_event(self):
        try:
            start = time.perf_counter()
            first_call = self.event.body.get('firstCall', None)
            if first_call:
                top_questions = self.get_top_questions()

                return {
                "statusCode": 200,
                "headers": CORS_HEADERS,
                "body": json.dumps({
                    "suggestions": [
                        {"question": s["question"], "count": s["count"]}
                        for s in top_questions
                    ],
                    "sessionId": self.user_context.session_id
                })
            }   

            source = self.event.source
            validate_tokens = self.user_context.validate_use_tokens(source)
            if "whatsapp" in source:
                self.send_whatsapp("Estoy pensando en tu consulta...")

                if not validate_tokens:
                    self.send_whatsapp("Alcanzaste el limite de tokens por el dia de hoy...")
                    return True

                self.logger.info("[SOURCE] Evento recibido desde WhatsApp")
                conversation_history = self.event.body["message"]
                last_message = self.event.body["message"]
            elif "google_chat" in source:
                if not validate_tokens:
                    self.send_google_chat_message(self.event.body["space_name"], "Alcanzaste el limite de tokens por el dia de hoy...")
                    return True

                self.logger.info("[SOURCE] Evento recibido desde Google Chat")
                conversation_history = self.event.body["text"]
                last_message = self.event.body["text"]
            else:
                self.logger.info("[SOURCE] Evento recibido desde FrontEnd")
                conversation_history = self.event.body.get('conversationHistory', [])
                last_message = conversation_history[-1]['content']

            if not conversation_history:
                return {
                    "statusCode": 400,
                    "headers": CORS_HEADERS,
                    "body": json.dumps({"error": "No se proporcionó historial."})
                }
        
            
            output_text, total_ms, total_tokens, total_steps, input_to_metrics = self.process_conversation_with_bedrock(conversation_history, self.user_context.session_id)

            if "whatsapp" in source:
                self.send_whatsapp(output_text)
            elif "google_chat" in source:
                output_text = output_text.replace("**", "*")
                self.send_google_chat_message(self.event.body["space_name"], output_text)
            else:                
                return {
                    "statusCode": 200,
                    "headers": CORS_HEADERS,
                    "body": json.dumps({
                        "outputText": output_text,
                        "sessionId": self.user_context.session_id,
                    })
                }
            
            end = time.perf_counter()

            total_ms_lambda = (end - start) * 1000

            self.user_context.metrics_questions(total_ms, total_tokens, total_steps, input_to_metrics, str(total_ms_lambda).split(".")[0], output_text)

        except Exception as e:
            self.logger.error(f"Error en API Gateway: {str(e)}")
            return {
                "statusCode": 500,
                "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": str(e)})
            } 