import json
import os
from typing import List
import unicodedata
import boto3
import botocore
import uuid
import jwt
import hashlib
from boto3.dynamodb.conditions import Attr

from datetime import datetime
from logging import Logger

from sqlalchemy import any_, and_, text
from twilio.rest import Client

from core.improved_context_classes import EnhancedUserContext, UserActivityTracker, CustomJSONEncoder
from core.request_handler import APIGatewayModel, RequestHandler

from core.database.db import SessionLocal
from core.database.models import SuggestedQuestions

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "https://front-app-ia.s3.us-east-1.amazonaws.com",
    "Access-Control-Allow-Credentials": "true",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST"
}

STOP_WORDS = {"en", "la", "el", "los", "las", "que", "de", "a", "y", "o", "un", "una", "?"}

CONTEXT_DEPENDENT_PHRASES = {
        "y después", "y ahora", "qué más", "seguí", "continúa",
        "cual de esos", "ese", "esa", "eso", "esas", "esos",
        "ahora", "después", "también", "además"
    }

dynamodb = boto3.resource('dynamodb')
user_questions_table = dynamodb.Table('user_questions_table')
user_table = dynamodb.Table('users_notifications_table')

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

        if token:
            decoded = jwt.decode(token, options={"verify_signature": False})

            user_email = decoded.get("email")
            username = decoded.get("cognito:username")
            sub = decoded.get("sub", "")
            session_id = f"{sub}_frontend"
            work_area = decoded.get("custom:work_area")
            canal = "frontend"

            response = user_table.scan(
                ProjectionExpression="apodo, contexto_usuario",
                FilterExpression=Attr("email").eq(user_email)
            )

            items = response.get("Items", [])

            nickname = items[0]["apodo"]

        source = self.event.source
        if "whatsapp" in source and session_id is None:
            from_num = self.event.body["from"]
            key = f"{from_num}_whatsapp"
            session_id = hashlib.sha256(key.encode("utf-8")).hexdigest()
            canal = "whatsapp"

            response = user_table.scan(
                ProjectionExpression="apodo, contexto_usuario",
                FilterExpression=Attr("num_telefono").eq(from_num)
            )

            items = response.get("Items", [])

            nickname = items[0]["apodo"]

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
        
        user_context = EnhancedUserContext(user_id, session_id_validated, ip_address, user_agent, user_email, username, nickname, work_area, UserActivityTracker(self.logger))
        
        # Registrar inicio de sesión
        self.user_logger.log_user_session_event(user_context, "SESSION_START", {
            "session_duration_intent": "unknown",
            "initial_request_time": user_context.session_start_time.isoformat()
        })
        
        return user_context

    def is_context_independent_heuristic(self, question: str) -> bool | None:
        """
        Devuelve:
        True  -> seguro independiente
        False -> seguro dependiente
        None  -> dudoso, hay que consultar al LLM
        """
        q = question.strip().lower()

        if len(q.split()) < 3:
            return False

        for phrase in CONTEXT_DEPENDENT_PHRASES:
            if phrase in q:
                return False

        if q.endswith("?") and len(q) < 10:
            return False

        if q.startswith(("divididas", "separadas", "por ")):
            return False

        verbos_comunes = {"son", "fueron", "hubo", "hay", "serán", "tiene", "mostrar", "listar"}
        if any(v in q for v in verbos_comunes):
            return True

        return None

    def classify_with_bedrock(self, question: str) -> bool:
        """
        Usa un LLM en Bedrock para decidir.
        Devuelve True si es independiente, False si es dependiente.
        """
        client = boto3.client("bedrock-runtime", region_name="us-east-1")

        prompt = f"""
        Dada esta frase del usuario: "{question}"

        Responde SOLO con una palabra: 'independiente' si puede entenderse sola,
        o 'dependiente' si necesita contexto previo.
        """

        response = client.invoke_model(
            modelId="anthropic.claude-3-5-haiku-20241022-v1:0",
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "inputText": prompt,
                "maxTokens": 10,
                "temperature": 0.0
            })
        )

        result = json.loads(response["body"].read())
        output = result["outputText"].strip().lower()

        return output.startswith("independiente")

    
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
    
    def valite_existing_response(self, session_id: str, keywords: List[str], user_input: str):

        conditions = [kw == any_(SuggestedQuestions.keywords) for kw in keywords]

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
            existing_question = (
                session.query(SuggestedQuestions)
                .filter(SuggestedQuestions.activa.is_(True), and_(*conditions))
                .order_by(SuggestedQuestions.prioridad)
                .first()
            )

            if existing_question and existing_question.sql_query is not None:
                sql_query = existing_question.sql_query
                query_results = session.execute(text(sql_query))

                query_result_dicts = [dict(row._mapping) for row in query_results]


                input_text = f"""
                    El usuario preguntó: "{user_input}"

                    La consulta SQL asociada (ID {existing_question.id}) devolvió estos resultados:
                    {json.dumps(query_result_dicts, ensure_ascii=False, indent=2)}

                    Por favor responde al usuario en lenguaje natural, breve y clara,
                    usando los resultados de la consulta.
                    """
                
                self.logger.info(f"[INFO] Se ha utilizado una pregunta precargada")
                self.logger.info(f"Mensaje enviado {input_text}")

                params = {
                    'agentId': 'DRSOAFDOTR',         # Reemplazá con tu agente real si hace falta
                    'agentAliasId': 'XKJTFFEMPC',    # Reemplazá si tenés otro alias
                    'sessionId': session_id,
                    'inputText': input_text,
                    'enableTrace': False,
                }

                response = client.invoke_agent(**params)

                return response

            else:
                return None
    
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

        # 1. Extrae el último mensaje
        source = self.event.source
        if "whatsapp" in source:
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
        
        self.save_user_question(last_message)

        keywords = [kw for kw in last_message.split() if kw.lower() not in STOP_WORDS]

        validation = self.valite_existing_response(session_id, keywords, last_message)
        
        new_q_id = None

        # if validation:
        #     assistant_response = ""
        #     event_stream = validation.get('completion')
            
        #     if isinstance(event_stream, botocore.eventstream.EventStream):
        #         for event in event_stream:
        #             if 'chunk' in event:
        #                 chunk_data = event['chunk']['bytes'].decode('utf-8')
        #                 assistant_response += chunk_data

        #     self.logger.info(f"[AGENT RESPONSE] Respuesta del agente: {assistant_response.strip()}")
        #     return assistant_response.strip()
        # else:
        #     save_question = False

        #     decision = self.is_context_independent_heuristic(last_message)

        #     if decision is True:
        #         save_question = True
        #     elif decision is None:
        #         if self.classify_with_bedrock(last_message):
        #             save_question = True

        #     if save_question:
        #         with SessionLocal() as session:
        #             new_q = SuggestedQuestions(
        #                 nombre=last_message,
        #                 activa=True,
        #                 keywords=keywords,
        #             )
        #             session.add(new_q)
        #             session.commit()

        #             new_q_id = new_q.id

        # 6. Obtiene session_id o genera uno nuevo
        #session_id = conversation_history[-1].get('sessionId', str(uuid.uuid4()))
        self.logger.info(f"sessionId enviado a Bedrock: {session_id}")

        # 7. Construcción de parámetros - CAMBIO: Agregamos parámetros de control

        last_message += f"""
            Utiliza el apodo del usuario para responder, el cual es {self.user_context.nickname}
        """
        
        params = {
            'agentId': 'DRSOAFDOTR',         # Reemplazá con tu agente real si hace falta
            'agentAliasId': 'XKJTFFEMPC',    # Reemplazá si tenés otro alias
            'sessionId': session_id,
            'inputText': last_message,
            'enableTrace': False,
            # 'sessionState': {
            #     "sessionAttributes": {
            #         "suggestion_id": str(new_q_id) if new_q_id else None
            #     }
            # }
        }

        try:
            self.logger.info(f"Enviando solicitud a Bedrock con parámetros: {params}")
            response = client.invoke_agent(**params)

            # 8. Procesar EventStream correctamente
            assistant_response = ""
            event_stream = response.get('completion')
            
            if isinstance(event_stream, botocore.eventstream.EventStream):
                for event in event_stream:
                    if 'chunk' in event:
                        try:
                            chunk_data = event['chunk']['bytes'].decode('utf-8')
                            assistant_response += chunk_data
                        except Exception as decode_error:
                            self.logger.error(f"Error decodificando chunk: {decode_error}")
            else:
                self.logger.error(f"Tipo inesperado en 'completion': {type(event_stream)}")

            self.logger.info(f"[AGENT RESPONSE] Respuesta del agente: {assistant_response.strip()}")
            return assistant_response.strip()    
        
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
            if "whatsapp" in source:
                self.logger.info("[SOURCE] Evento recibido desde WhatsApp")
                conversation_history = self.event.body["message"]
                last_message = self.event.body["message"]
            else:
                self.logger.info("[SOURCE] Evento recibido desde FrontEnd")
                conversation_history = self.event.body.get('conversationHistory', [])
                last_message = conversation_history[-1]['content']

            self.user_logger.log_user_request(
                self.user_context,
                "INFO_USER_REQUEST",
                {
                    "api_path": self.event.resource,
                    "http_method": self.event.http_method,
                    "event_type": "API_GATEWAY",
                    "lambda_version": "complete_user_logging_system",
                    "user question": last_message,
                }
            )

            if not conversation_history:
                return {
                    "statusCode": 400,
                    "headers": CORS_HEADERS,
                    "body": json.dumps({"error": "No se proporcionó historial."})
                }
        
            
            output_text = self.process_conversation_with_bedrock(conversation_history, self.user_context.session_id)
            suggested_questions = self.get_active_suggestions(last_message)

            if "whatsapp" in source:
                self.send_whatsapp(output_text)
            else:                
                return {
                    "statusCode": 200,
                    "headers": CORS_HEADERS,
                    "body": json.dumps({
                        "outputText": output_text,
                        "sessionId": self.user_context.session_id,
                        "suggestions": suggested_questions
                    })
                }

        except Exception as e:
            self.logger.error(f"Error en API Gateway: {str(e)}")
            return {
                "statusCode": 500,
                "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": str(e)})
            } 