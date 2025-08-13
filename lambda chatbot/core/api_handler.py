import json
import unicodedata
import boto3
import botocore
import uuid
import jwt
import hashlib

from datetime import datetime
from logging import Logger

from core.improved_context_classes import EnhancedUserContext, UserActivityTracker, CustomJSONEncoder
from core.request_handler import APIGatewayModel, RequestHandler

from core.database.db import SessionLocal, engine
from core.database.models import SuggestedQuestions

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "https://front-app-ia.s3.us-east-1.amazonaws.com",
    "Access-Control-Allow-Credentials": "true",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST"
}

STOP_WORDS = {"en", "la", "el", "los", "las", "que", "de", "a", "y", "o", "un", "una"}

dynamodb = boto3.resource('dynamodb')
user_questions_table = dynamodb.Table('user_questions_table')

class ApiRequestHandler(RequestHandler):
    def __init__(self, logger: Logger, req_id: str, event: APIGatewayModel, lambda_handler):
        self.event = event
        super().__init__(logger, req_id, event, lambda_handler)

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

        if token:
            decoded = jwt.decode(token, options={"verify_signature": False})

            user_email = decoded.get("email")
            username = decoded.get("cognito:username")
            session_id = decoded.get("sub")
            work_area = decoded.get("custom:work_area")

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
        
        user_context = EnhancedUserContext(user_id, session_id, ip_address, user_agent, user_email, username, work_area, UserActivityTracker(self.logger))
        
        # Registrar inicio de sesión
        self.user_logger.log_user_session_event(user_context, "SESSION_START", {
            "session_duration_intent": "unknown",
            "initial_request_time": user_context.session_start_time.isoformat()
        })
        
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

        response = user_questions_table.update_item(
            Key={
                'user_id': self.user_context.user_email,
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

        # 1. Extrae el último mensaje
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

        # 6. Obtiene session_id o genera uno nuevo
        #session_id = conversation_history[-1].get('sessionId', str(uuid.uuid4()))
        self.logger.info(f"sessionId enviado a Bedrock: {session_id}")

        # 7. Construcción de parámetros - CAMBIO: Agregamos parámetros de control
        
        params = {
            'agentId': 'DRSOAFDOTR',         # Reemplazá con tu agente real si hace falta
            'agentAliasId': 'XKJTFFEMPC',    # Reemplazá si tenés otro alias
            'sessionId': session_id,
            'inputText': last_message,
            'enableTrace': False,
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