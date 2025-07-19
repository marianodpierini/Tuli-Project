import base64
import gzip
import json
import unicodedata
import boto3
import botocore
import traceback
import uuid
import time as pytime
import jwt

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional, Union, Callable, Any
from cachetools import TTLCache
from logging import Logger
from functools import lru_cache

from sqlalchemy.inspection import inspect
from sqlalchemy import inspect, extract, select, func

from core.improved_context_classes import EnhancedUserContext, UserActivityTracker, StructuredUserLogger, CustomJSONEncoder, EnhancedQueryManager, EnhancedQueryExecutor
from core.database.db import SessionLocal, engine
from core.database.models import ServiciosTcktsRvas

CACHE_MAX_SIZE = 100
CACHE_TTL = 3600  # 1 hora
QUERY_CACHE = TTLCache(maxsize=CACHE_MAX_SIZE, ttl=CACHE_TTL)
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "https://front-app-ia.s3.us-east-1.amazonaws.com",
    "Access-Control-Allow-Credentials": "true",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST"
}

@dataclass
class APIGatewayModel():
    http_method: Optional[str]
    resource: Optional[str]
    authorization: Optional[str]
    request_context: Optional[Dict[str, str]]
    headers: Optional[Dict[str, str]]
    body: Optional[Dict[str, str]]

@dataclass
class BedrockEvent():
    http_method: Optional[str]
    session_id: Optional[str]
    action_group: Optional[str]
    input_text: Optional[str]
    api_path: Optional[str]
    request_body: Optional[Dict[str, str]]
    session_attributes: Optional[Dict[str, str]]
    prompt_session_attributes: Optional[Dict[str, str]]
    agent: Optional[Dict[str, str]]

class RequestHandler:
    def __init__(self, logger: Logger, req_id: str, event: Union[APIGatewayModel, BedrockEvent], lambda_handler: Callable[[dict, Any], Any]):
        self.logger = logger
        self.start_time = pytime.time()
        self.activity_tracker = UserActivityTracker(self.logger)
        self.event = event
        self.req_id = req_id
        self.user_logger = StructuredUserLogger(self.logger, UserActivityTracker(self.logger))
        self.user_context = self.get_user_context()
        self.clean_user_data(lambda_handler)
        self.query_manager, self.executor = self.initialize_managers()

    def format_general_response(self, data, status_code, http_method):
        json_body = json.dumps(data, cls=CustomJSONEncoder)
        return {
            "messageVersion": "1.0",
            "response": {
                "httpMethod": http_method,
                "httpStatusCode": status_code,
                "responseBody": {
                    "application/json": {
                        "body": json_body
                    }
                }
            },
        }

    def initialize_managers(self):
        # Configuración del gestor de consultas
        allowed_tables = [
            "aptour.servicios_tckts_rvas",
            "servicios_tckts_rvas"
        ]
            
        query_manager = EnhancedQueryManager(
            logger=self.logger,
            allowed_tables=allowed_tables, 
            max_query_length=3000,
            query_timeout_ms=30000
        )
            
        executor = EnhancedQueryExecutor(self.logger, self.user_logger, query_manager, QUERY_CACHE)

        return query_manager, executor

    def get_user_context(self) -> EnhancedUserContext:
        raise NotImplementedError
    
    def handle_event(self):
        raise NotImplementedError
    
    def compress_data(self, data):
        """Comprime la respuesta JSON en gzip y la codifica en Base64"""
        compressed_bytes = gzip.compress(json.dumps(data, cls=CustomJSONEncoder).encode('utf-8')) 
        return base64.b64encode(compressed_bytes).decode('utf-8')
    
    def cleanup_user_data(self):
        """Función de limpieza que se puede llamar periódicamente"""
        self.activity_tracker.cleanup_old_data(days_to_keep=7)
        self.logger.info("Limpieza de datos de usuario completada")
    
    
    def clean_user_data(self, lambda_handler: Callable[[dict, Any], Any]):
        try:

            # Limpieza periódica de datos de usuario
            if not hasattr(lambda_handler, 'cleanup_counter'):
                lambda_handler.cleanup_counter = 0
            
            lambda_handler.cleanup_counter += 1
            if lambda_handler.cleanup_counter % 50 == 0:  # Cada 50 requests
                cleanup_start = pytime.time()
                self.cleanup_user_data()
                cleanup_time = pytime.time() - cleanup_start
                self.logger.info(f"[{self.req_id}] Limpieza automática completada en {cleanup_time:.2f}s")

        except Exception as e:
            execution_time = (pytime.time() - self.start_time) * 1000
            stack_trace = traceback.format_exc()
            
            # Log estructurado del error principal
            self.user_logger.log_user_error(
                self.user_context,
                "LAMBDA_ERROR",
                str(e),
                {
                    "request_id": self.req_id,
                    "execution_time_ms": execution_time,
                    "stack_trace": stack_trace
                }
            )
            
            self.logger.error(f"[{self.req_id}][{self.user_context.get_user_hash()}] Error general en lambda_handler: {str(e)}\n{stack_trace}")
            
            return self.format_general_response(
                        http_method=self.event.http_method,
                        data={"error": f"Error en el servidor: {str(e)}"},
                        status_code=500
                    )


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

        if token:
            decoded = jwt.decode(token, options={"verify_signature": False})

            user_email = decoded.get("email")
            username = decoded.get("cognito:username")
            session_id = decoded.get("sub")

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
        
        user_context = EnhancedUserContext(user_id, session_id, ip_address, user_agent, user_email, username, UserActivityTracker(self.logger))
        
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
    
    def is_message_valid(self, text):
        prohibited_words = ['palabra_prohibida1', 'palabra_prohibida2']
        return not any(word in text.lower() for word in prohibited_words)
    
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
    
    def handle_event(self):
        try:
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

            return {
                "statusCode": 200,
                "headers": CORS_HEADERS,
                "body": json.dumps({
                    "outputText": output_text,
                    "sessionId": self.user_context.session_id
                })
            }

        except Exception as e:
            self.logger.error(f"Error en API Gateway: {str(e)}")
            return {
                "statusCode": 500,
                "headers": {"Access-Control-Allow-Origin": "*"},
                "body": json.dumps({"error": str(e)})
            } 


class BedrockRequestHandler(RequestHandler):
    def __init__(self, logger, req_id, event, lambda_handler):
        super().__init__(logger, req_id, event, lambda_handler)
        self.event_id = str(uuid.uuid4())[:8]
        self.start_time = pytime.time()
        self.bedrock_session_id = self.event.session_id

    def format_response_for_bedrock(self, action_group, api_path, http_method, data, status_code=200, session_attributes=None, prompt_session_attributes=None):
        if session_attributes is None:
            session_attributes = {}
        if prompt_session_attributes is None:
            prompt_session_attributes = {}

        # Verificar si la respuesta es muy grande
        try:
            json_body = json.dumps(data, cls=CustomJSONEncoder)
            response_size = len(json_body)
            
            if response_size > 20000:  # 20KB para dejar margen
                self.logger.info(f"Respuesta grande detectada: {response_size} bytes. Aplicando compresión.")
                compressed_data = self.compress_data(data)
                data = {
                    "compressed_data": compressed_data,
                    "compression_method": "gzip_base64",
                    "original_size": response_size,
                    "message": "Datos comprimidos debido al tamaño de respuesta"
                }
                json_body = json.dumps(data, cls=CustomJSONEncoder)
                self.logger.info(f"Tamaño después de compresión: {len(json_body)} bytes")
        except Exception as e:
            self.logger.error(f"Error durante compresión: {str(e)}")
            json_body = json.dumps(data, cls=CustomJSONEncoder)

        return {
            "messageVersion": "1.0",
            "response": {
                "actionGroup": action_group,
                "apiPath": api_path,
                "httpMethod": http_method,
                "httpStatusCode": status_code,
                "responseBody": {
                    "application/json": {
                        "body": json_body
                    }
                }
            },
            "sessionAttributes": session_attributes,
            "promptSessionAttributes": prompt_session_attributes
        }

    def get_user_context(self):

        session_id = self.event.session_id
        
        user_agent = self.event.agent
        
        user_context = EnhancedUserContext(session_id=session_id, user_agent=user_agent, activity_tracker=UserActivityTracker(self.logger))
        
        # Registrar inicio de sesión
        self.user_logger.log_user_session_event(user_context, "SESSION_START", {
            "session_duration_intent": "unknown",
            "initial_request_time": user_context.session_start_time.isoformat()
        })
        
        return user_context
    
    def extract_query_fast(self):
        """Extrae la consulta SQL de la manera más rápida posible"""
        try:
            properties = self.event.request_body.get('content', {}).get('application/json', {}).get('properties', [])
            
            for prop in properties:
                if prop.get('name') in ['content', 'query', 'sql']:
                    value = prop.get('value')
                    
                    if isinstance(value, dict) and ('query' in value or 'sql' in value):
                        return value.get('query', value.get('sql', ''))
                    
                    if isinstance(value, str):
                        if value.startswith('{query='):
                            return value[7:].rstrip('}')
                        if value.startswith('{sql='):
                            return value[5:].rstrip('}')
                        if value.startswith('{') and value.endswith('}'):
                            try:
                                content_dict = json.loads(value)
                                if isinstance(content_dict, dict):
                                    return content_dict.get('query', content_dict.get('sql', ''))
                            except:
                                pass
                        return value
                    
                    return str(value)
        except Exception as e:
            self.logger.error(f"Error extrayendo consulta: {str(e)}")
            return None
    
    def handle_consulta(self):
        self.logger.info(f"[{self.event_id}] Procesando consulta con sessionId: {self.bedrock_session_id}")
            
        # Extraer consulta
        query = self.extract_query_fast()
            
        if not query:
            self.logger.error(f"[{self.event_id}] No se encontró consulta SQL")
            self.user_logger.log_user_error(
                self.user_context,
                "NO_QUERY_FOUND",
                "No se encontró consulta SQL en la solicitud",
                {"event_id": self.event_id}
            )
            return self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                data={"error": "No se encontró consulta SQL en la solicitud"},
                status_code=400,
                session_attributes=self.event.session_attributes,
                prompt_session_attributes=self.event.prompt_session_attributes
            )
            
        # Ejecutar consulta
        sql_start = pytime.time()
        results = self.executor.execute(query, self.user_context)
        sql_time = pytime.time() - sql_start
            
        self.logger.info(f"[{self.event_id}] Consulta ejecutada en {sql_time:.2f}s con session: {self.bedrock_session_id}")
            
        if isinstance(results, dict) and "error" in results:
            return self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                data=results,
                status_code=400,
                session_attributes=self.event.session_attributes,
                prompt_session_attributes=self.event.prompt_session_attributes
            )
            
        # Preparar respuesta exitosa
        total_time = pytime.time() - self.start_time
            
        response_data = {
            "results": results.get('results', []) if isinstance(results, dict) else results,
            "count": len(results.get('results', [])) if isinstance(results, dict) and 'results' in results else 0,
            "time_ms": int(total_time * 1000),
            "cache_hit": results.get('cache_hit', False) if isinstance(results, dict) else False,
            "session_info": {
                "bedrock_session_id": self.bedrock_session_id,
                "maintained_context": True,
                #"user_summary": self.user_context.get_session_summary()
            }
        }
            
        # CRÍTICO: Mantener sessionAttributes para el contexto
        enhanced_session_attributes = self.event.session_attributes.copy()
        enhanced_session_attributes.update({
            'last_query_time': datetime.now().isoformat(),
            'query_count': int(enhanced_session_attributes.get('query_count', 0)) + 1,
            'lambda_session_id': self.bedrock_session_id,
            'user_hash': self.user_context.get_user_hash()
        })
            
        # Log final de respuesta exitosa
        self.user_logger.log_user_request(
            self.user_context,
            "BEDROCK_RESPONSE_SUCCESS",
            {
                "event_id": self.event_id,
                "response_time_ms": total_time * 1000,
                "results_count": response_data["count"],
                "cache_hit": response_data["cache_hit"]
            }
        )
            
        return self.format_response_for_bedrock(
            action_group=self.event.action_group,
            api_path=self.event.api_path,
            http_method=self.event.http_method,
            data=response_data,
            session_attributes=enhanced_session_attributes,
            prompt_session_attributes=self.event.prompt_session_attributes
        )
    
    @lru_cache(maxsize=1)
    def get_schema(self):
        try:     
            mapper = inspect(ServiciosTcktsRvas)
            schema = {
                "table_name": ServiciosTcktsRvas.__tablename__,
                "columns": {}
            }

            for column in mapper.columns:
                schema["columns"][column.name] = {
                    "type": str(column.type),
                    "nullable": column.nullable,
                    "default": str(column.default.arg) if column.default is not None else None,
                }
                
            return schema
        
        except Exception as e:
            self.logger.error(f"Error al obtener el esquema: {str(e)}")
            raise
    
    def handle_schema(self):
        self.logger.info(f"[{self.event_id}] Procesando solicitud de esquema")
            
        self.user_logger.log_user_request(
            self.user_context,
            "SCHEMA_REQUEST",
            {"event_id": self.event_id}
        )
            
        try:
            schema = self.get_schema()

            self.logger.info(f"[{self.event_id}] Esquema obtenido: {json.dumps(schema)}")
                
            response_data = {"schema": schema}
            total_time = pytime.time() - self.start_time
                
            self.user_logger.log_user_request(
                self.user_context,
                "SCHEMA_RESPONSE_SUCCESS",
                {
                    "event_id": self.event_id,
                    "response_time_ms": total_time * 1000
                }
            )
                
            return self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                data=response_data,
                session_attributes=self.event.session_attributes,
                prompt_session_attributes=self.event.prompt_session_attributes
            )
        except Exception as schema_error:
            self.user_logger.log_user_error(
                self.user_context,
                "SCHEMA_ERROR",
                str(schema_error),
                {
                    "event_id": self.event_id,
                    "stack_trace": traceback.format_exc()
                }
            )
                
            self.logger.error(f"[{self.event_id}] Error obteniendo esquema: {str(schema_error)}")
            return self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                data={"error": f"Error obteniendo esquema: {str(schema_error)}"},
                status_code=500,
                session_attributes=self.event.session_attributes,
                prompt_session_attributes=self.event.prompt_session_attributes
            )
        
    def handle_system_metrics(self):
        """Endpoint para métricas del sistema"""
        self.logger.info(f"[{self.event_id}] Procesando solicitud de métricas del sistema")
                
        self.user_logger.log_user_request(
            self.user_context,
            "SYSTEM_METRICS_REQUEST",
            {"event_id": self.event_id}
        )
                
        try:
            #metrics = get_system_metrics()
            
            return self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                #data={"system_metrics": metrics},
                session_attributes=self.event.session_attributes,
                prompt_session_attributes=self.event.prompt_session_attributes
            )
        except Exception as e:
            self.logger.error(f"Error en endpoint de métricas del sistema: {str(e)}")
            return self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                data={"error": str(e)},
                status_code=500
            )
        
    def quick_database_diagnostics(self, full_check=False):
        """Ejecuta diagnósticos críticos para identificar problemas comunes"""
        diagnostics = {}
        # 1. Test conexión: simplemente intentamos abrir una sesión y hacer una query mínima
        try:
            with SessionLocal() as session:
                session.execute(select(1))
                diagnostics["connection"] = True
        except Exception:
            diagnostics["connection"] = False
            return diagnostics
        
        # 2. Verificar si existe la tabla
        inspector = inspect(engine)
        table_name = ServiciosTcktsRvas.__tablename__
        schema = getattr(ServiciosTcktsRvas.__table__, 'schema', 'public')

        table_exists = table_name in inspector.get_table_names(schema=schema)
        diagnostics["table_exists"] = table_exists

        if not table_exists:
            return diagnostics
        
        # 3. Obtener metadatos de la columna fec_ape
        columns = inspector.get_columns(table_name, schema=schema)
        fec_ape_info = next((col for col in columns if col["name"] == "fec_ape"), None)

        diagnostics["fec_ape_column"] = {
            "data_type": str(fec_ape_info["type"]) if fec_ape_info else None,
            "is_nullable": fec_ape_info["nullable"] if fec_ape_info else None
        }

        # 4. Si full_check, contar registros del año 2024 usando ORM puro
        if full_check:
            with SessionLocal() as session:
                count_2024 = session.query(func.count()).select_from(ServiciosTcktsRvas).filter(
                    extract("year", ServiciosTcktsRvas.fec_ape) == 2024
                ).scalar()
                diagnostics["records_2024"] = count_2024

        return diagnostics
        
    def handle_diagnostico(self):
        self.logger.info(f"[{self.event_id}] Petición a endpoint /diagnostico")
            
        self.user_logger.log_user_request(
            self.user_context,
            "DIAGNOSTICS_REQUEST",
            {"event_id": self.event_id}
        )
            
        diag_start_time = pytime.time()
        diagnostics = self.quick_database_diagnostics(full_check=True)
        diag_time = pytime.time() - diag_start_time
        self.logger.info(f"[{self.event_id}] Diagnóstico completado en {diag_time:.2f}s")
            
        response_data = {"diagnostico": diagnostics}
            
        self.user_logger.log_user_request(
            self.user_context,
            "DIAGNOSTICS_RESPONSE_SUCCESS",
            {
                "event_id": self.event_id,
                "response_time_ms": diag_time * 1000
            }
        )
            
        return self.format_response_for_bedrock(
            action_group=self.event.action_group,
            api_path=self.event.api_path,
            http_method=self.event.http_method,
            data=response_data,
            session_attributes=self.event.session_attributes,
            prompt_session_attributes=self.event.prompt_session_attributes
        )
    
    def direct_diagnostic(self):
        """Realiza un diagnóstico directo de la conexión a la base de datos"""
        try:
            diagnostics = {}

            # Iniciar sesión SQLAlchemy
            with SessionLocal() as session:
                # 1. Obtener info de conexión
                result = session.execute(
                    select(
                        func.current_database(),
                        func.current_schema(),
                        func.current_user()
                    )
                ).first()

                connection_info = {
                    "current_database": result[0],
                    "current_schema": result[1],
                    "current_user": result[2]
                }

                self.logger.info(
                    f"Conexión a: DB={connection_info['current_database']}, "
                    f"Schema={connection_info['current_schema']}, "
                    f"User={connection_info['current_user']}"
                )

            # 2. Verificar si la tabla existe
            inspector = inspect(engine)
            schema = getattr(ServiciosTcktsRvas.__table__, 'schema', 'public')
            table_name = ServiciosTcktsRvas.__tablename__

            table_exists = table_name in inspector.get_table_names(schema=schema)
            self.logger.info(f"¿La tabla existe? {table_exists}")

            # Inicializar contadores
            count = 0
            count_2024 = 0

            # 3. Si la tabla existe, hacer los conteos
            if table_exists:
                with SessionLocal() as session:
                    # Total de registros
                    count = session.query(func.count()).select_from(ServiciosTcktsRvas).scalar()
                    self.logger.info(f"Cantidad real de registros: {count}")

                    # Registros del año 2024
                    count_2024 = session.query(func.count()).select_from(ServiciosTcktsRvas).filter(
                        extract("year", ServiciosTcktsRvas.fec_ape) == 2024
                    ).scalar()
                    self.logger.info(f"Registros de 2024: {count_2024}")

            return {
                "connection": connection_info,
                "table_exists": table_exists,
                "record_count": count,
                "records_2024": count_2024
            }
        
        except Exception as e:
            self.logger.error(f"Error en diagnóstico: {str(e)}")
            return {"error": str(e)}

    def handle_diagnostics(self):
        try:
            self.logger.info(f"[{self.event_id}] Petición a endpoint /diagnostics")
                
            self.user_logger.log_user_request(
                self.user_context,
                "DIAGNOSTICS_DETAILED_REQUEST",
                {"event_id": self.event_id}
            )
                
            # Verificar conexión directa
            conn_test = self.direct_diagnostic()
                
            response_data = {"connection_test": conn_test}
                
            self.user_logger.log_user_request(
                self.user_context,
                "DIAGNOSTICS_DETAILED_RESPONSE_SUCCESS",
                {"event_id": self.event_id}
            )
                
            return self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                data=response_data,
                session_attributes=self.event.session_attributes,
                prompt_session_attributes=self.event.prompt_session_attributes
            )
        except Exception as diag_error:
            self.user_logger.log_user_error(
                self.user_context,
                "DIAGNOSTICS_DETAILED_ERROR",
                str(diag_error),
                {
                    "event_id": self.event_id,
                    "stack_trace": traceback.format_exc()
                }
            )
            self.logger.error(f"[{self.event_id}] Error en diagnósticos: {str(diag_error)}")
            return self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                data={"error": str(diag_error)},
                status_code=500,
                session_attributes=self.event.session_attributes,
                prompt_session_attributes=self.event.prompt_session_attributes
            )
    
    def handle_event(self):
        """Handler corregido para Action Groups de Bedrock Agent con logging completo"""

        if not self.bedrock_session_id:
            self.logger.error(f"[{self.event_id}] CRÍTICO: No se pudo extraer sessionId del evento")
            
            # Crear un sessionId de emergencia
            self.bedrock_session_id = f"emergency_{uuid.uuid4().hex[:8]}"
            self.logger.warning(f"[{self.event_id}] Usando sessionId de emergencia: {self.bedrock_session_id}")

        self.logger.info(f"[{self.event_id}] SessionId extraído: {self.bedrock_session_id}")

        # Actualizar información de sesión de Bedrock
        self.user_context.update_bedrock_session({
            'session_id': self.bedrock_session_id,
            'session_attributes': self.event.session_attributes,
            'prompt_session_attributes': self.event.prompt_session_attributes,
        })

        # Log de la petición
        self.user_logger.log_user_request(
            self.user_context,
            "BEDROCK_ACTION_GROUP",
            {
                "event_id": self.event_id,
                "api_path": self.event.api_path,
                "action_group": self.event.action_group,
                "bedrock_session_id": self.bedrock_session_id
            }
        )

        try:
            if self.event.api_path == '/consulta':
                response = self.handle_consulta()
            elif self.event.api_path == '/schema':
                response = self.handle_schema()
            elif self.event.api_path == '/system_metrics':
                response = self.handle_system_metrics()
            elif self.event.api_path == '/diagnostico':
                response = self.handle_diagnostico()
            elif self.event.api_path == '/diagnostics':
                response = self.handle_diagnostics()
            else:
                self.logger.warning(f"[{self.event_id}] Endpoint no reconocido: {self.event.api_path}")
            
                self.user_logger.log_user_error(
                    self.user_context,
                    "UNKNOWN_ENDPOINT",
                    f"Endpoint no reconocido: {self.event.api_path}",
                    {"event_id": self.event_id}
                )
                
                response = self.format_response_for_bedrock(
                    action_group=self.event.action_group,
                    api_path=self.event.api_path,
                    http_method=self.event.http_method,
                    data={"error": "Endpoint no reconocido"},
                    status_code=404,
                    session_attributes=self.event.session_attributes,
                    prompt_session_attributes=self.event.prompt_session_attributes
                )
        
        except Exception as e:
            total_time = pytime.time() - self.start_time
            stack_trace = traceback.format_exc()
            
            self.user_logger.log_user_error(
                self.user_context,
                "BEDROCK_ACTION_GROUP_ERROR",
                str(e),
                {
                    "event_id": self.event_id,
                    "bedrock_session_id": self.bedrock_session_id,
                    "execution_time_ms": total_time * 1000,
                    "stack_trace": stack_trace
                }
            )
            
            self.logger.error(f"[{self.event_id}] Error con session {self.bedrock_session_id}: {str(e)}")

            response = self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                data={"error": str(e)},
                status_code=500,
                session_attributes=self.event.session_attributes,
                prompt_session_attributes=self.event.prompt_session_attributes
            )
        
        execution_time = (pytime.time() - self.start_time) * 1000

        # Log final de respuesta exitosa del Lambda
        self.user_logger.log_user_request(
            self.user_context,
            "LAMBDA_RESPONSE_SUCCESS",
            {
                "request_id": self.req_id,
                "execution_time_ms": execution_time,
                "response_type": type(response).__name__,
                "active_users_count": len(self.activity_tracker.user_activities),
                #"user_total_queries": activity_tracker.get_user_summary(user_context.get_user_hash()).get('total_queries', 0)
            }
        )
        
        self.logger.info(f"[{self.req_id}][{self.user_context.get_user_hash()}] Completado: {execution_time:.2f}ms")
        return response