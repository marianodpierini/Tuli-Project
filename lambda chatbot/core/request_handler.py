import base64
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import boto3
import traceback
import time as pytime

from dataclasses import dataclass
from typing import Dict, Optional, Union, Callable, Any
#from cachetools import TTLCache
from logging import Logger

from core.improved_context_classes import EnhancedUserContext, UserActivityTracker, StructuredUserLogger, CustomJSONEncoder, EnhancedQueryManager, EnhancedQueryExecutor

CACHE_MAX_SIZE = 100
CACHE_TTL = 3600  # 1 hora
#QUERY_CACHE = TTLCache(maxsize=CACHE_MAX_SIZE, ttl=CACHE_TTL)
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "https://front-app-ia.s3.us-east-1.amazonaws.com",
    "Access-Control-Allow-Credentials": "true",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,POST"
}

dynamodb = boto3.resource('dynamodb')
user_questions_table = dynamodb.Table('user_questions_table')
users_sessions_table = dynamodb.Table('users_sessions_table')

@dataclass
class APIGatewayModel():
    http_method: Optional[str]
    resource: Optional[str]
    authorization: Optional[str]
    request_context: Optional[Dict[str, str]]
    headers: Optional[Dict[str, str]]
    body: Optional[Dict[str, str]]
    source: Optional[str]

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
            "servicios_tckts_rvas",
            "aptour.suggested_questions",
            "suggested_questions"
        ]
            
        query_manager = EnhancedQueryManager(
            logger=self.logger,
            allowed_tables=allowed_tables, 
            max_query_length=3000,
            query_timeout_ms=30000
        )
            
        executor = EnhancedQueryExecutor(self.logger, self.user_logger, query_manager)

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
        
    def validate_user_session(self, session_id, canal, ttl_hours=30):
        response = users_sessions_table.get_item(Key={"session_id": session_id})
        item = response.get("Item")

        now = datetime.now(timezone.utc)
        current_time = int(now.timestamp())

        expires_at = int((now + timedelta(minutes=ttl_hours)).timestamp())

        if item and item["expires_at"] > current_time:
            return item["session_id"]

        users_sessions_table.put_item(
            Item={
                "session_id": session_id,
                "channel": canal,
                "expires_at": expires_at
            }
        )
        return session_id
