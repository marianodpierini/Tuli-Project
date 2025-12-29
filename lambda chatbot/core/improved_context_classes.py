import hashlib
import re
import traceback
import uuid
import json
import time as pytime
import statistics
import threading
import boto3

from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional
from logging import Logger

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from core.database.db import SessionLocal

PROHIBITED_KEYWORDS = ["insert", "update", "delete", "drop", "alter", "truncate", "create"]
LIMITE_TOKENS_DIARIO = 550000
dynamodb = boto3.resource('dynamodb')
usage_table = dynamodb.Table("user_usage_tokens")
metrics_table = dynamodb.Table("questions_metrics")


class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (datetime, date, pytime.struct_time)):
            return obj.isoformat()
        elif isinstance(obj, Decimal):
            return float(obj)
        elif isinstance(obj, timedelta):
            return obj.total_seconds()
        elif isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)
    

class EnhancedQueryManager:
    def __init__(self, logger: Logger, allowed_tables, max_query_length=2000, query_timeout_ms=10000):
        self.logger = logger
        self.allowed_tables = allowed_tables
        self.max_query_length = max_query_length
        self.query_timeout_ms = query_timeout_ms

    def preprocess_query(self, query):
        """Preprocesa y limpia la consulta SQL para su ejecución"""
        try:
            query_info = {
                "evento": "QUERY CONSULT",
                "message": "Query preprocesamiento",
                "query": " ".join(query.split())
            }

            self.logger.info(json.dumps(query_info))
            
            if isinstance(query, dict):
                if 'sql' in query:
                    query = query['sql']
                elif 'query' in query:
                    query = query['query']
                else:
                    query = str(query)
            elif isinstance(query, str):
                if query.strip().startswith('{') and query.strip().endswith('}'):
                    try:
                        parsed = json.loads(query)
                        if isinstance(parsed, dict):
                            if 'sql' in parsed:
                                query = parsed['sql']
                            elif 'query' in parsed:
                                query = parsed['query']
                    except json.JSONDecodeError:
                        query = query.replace('{sql=', '').replace('{query=', '').replace('}', '')
            
            query = query.strip()
            query = query.replace('\"', '"').replace("\\'", "'")
            query = query.replace('\\n', ' ').replace('\\r', ' ')
            query = ' '.join(query.split())

            query_info["query"] = query
            query_info["message"] = "Query despues del preprocesamiento"
            
            self.logger.info(json.dumps(query_info))
            return query
        except Exception as e:
            self.logger.error(f"Error preprocessing query: {str(e)}\n{traceback.format_exc()}")
            raise ValueError(f"Invalid query format: {str(e)}")

    def validate_stage1(self, query):
        """Enhanced first-stage validation"""
        try:
            processed_query = self.preprocess_query(query)

            if not processed_query or len(processed_query.strip()) == 0:
                self.logger.warning("Se recibió una consulta vacía")
                raise ValueError("La consulta no puede estar vacía")

            if re.search(rf"\b({'|'.join(PROHIBITED_KEYWORDS)})\b", processed_query.lower()):
                self.logger.warning("Se detectaron palabras prohibidas en la consulta")
                return "SELECT NULL AS resultado"

            return processed_query

        except Exception as e:
            self.logger.error(f"Error en validate_stage1: {str(e)}")
            raise
    
    def add_schema_prefix_if_missing(self, query):
        """Añade el prefijo de esquema si falta en la consulta"""
        for allowed_table in self.allowed_tables:
            if '.' in allowed_table:
                schema, table_name = allowed_table.split('.')
                pattern = rf'\bFROM\s+{table_name}\b'
                if re.search(pattern, query, re.IGNORECASE):
                    query = re.sub(pattern, f'FROM {schema}.{table_name}', query, flags=re.IGNORECASE)
                pattern = rf'\bJOIN\s+{table_name}\b'
                if re.search(pattern, query, re.IGNORECASE):
                    query = re.sub(pattern, f'JOIN {schema}.{table_name}', query, flags=re.IGNORECASE)
        return query

    def validate_stage2(self, query):
        """Versión optimizada para rendimiento con corrección automática de esquema"""
        query = self.add_schema_prefix_if_missing(query)
        
        table_check_passed = False
        for table in self.allowed_tables:
            table_name = table.split('.')[-1]
            if table_name.lower() in query.lower() or table.lower() in query.lower():
                table_check_passed = True
                break
        
        if not table_check_passed:
            self.logger.warning(f"Tabla no permitida: {query}")
            raise Exception("Consulta intenta acceder a tablas no permitidas.")
        
        query = f"SET statement_timeout = {self.query_timeout_ms};\n{query}"
        
        if "limit" not in query.lower() and "count(" not in query.lower():
            query += " LIMIT 500"
        
        return query

class UserActivityTracker:
    """Tracker avanzado de actividad por usuario con métricas y agregación"""
    
    def __init__(self, logger: Logger):
        self.logger = logger
        self.user_activities = defaultdict(lambda: {
            'queries': deque(maxlen=100),  # Últimas 100 queries
            'sessions': defaultdict(dict),
            'daily_stats': defaultdict(lambda: {
                'query_count': 0,
                'total_time_ms': 0,
                'error_count': 0,
                'cache_hits': 0,
                'unique_queries': set()
            }),
            'hourly_pattern': defaultdict(int),  # Patrón de uso por hora
            'query_patterns': defaultdict(int),  # Tipos de consultas más frecuentes
            'performance_stats': {
                'avg_query_time': 0,
                'fastest_query': float('inf'),
                'slowest_query': 0,
                'total_queries': 0
            }
        })
        self.lock = threading.Lock()
        self.cloudwatch = boto3.client('cloudwatch')
        
        # Cargar datos persistentes al inicializar
        self._load_persistent_data()
    
    def _load_persistent_data(self):
        """Carga datos persistentes para usuarios activos"""
        # En una implementación real, cargarías desde DynamoDB los usuarios más activos
        pass
    
    def log_query_activity(self, user_context, query: str, execution_time_ms: float, 
                          results_count: int, cache_hit: bool = False, error: str = None):
        """Registra actividad de consulta de usuario"""
        user_hash = user_context.get_user_hash()
        session_id = getattr(user_context, 'bedrock_session', {}).get('session_id', user_context.session_id)
        
        with self.lock:
            user_data = self.user_activities[user_hash]
            current_time = datetime.now()
            today = current_time.date().isoformat()
            hour = current_time.hour
            
            # Registrar consulta individual
            query_record = {
                'timestamp': current_time.isoformat(),
                'session_id': session_id,
                'query_preview': query[:100],
                'query_hash': hashlib.md5(query.encode()).hexdigest()[:8],
                'execution_time_ms': execution_time_ms,
                'results_count': results_count,
                'cache_hit': cache_hit,
                'error': error,
                'query_type': self._classify_query(query)
            }
            
            user_data['queries'].append(query_record)
            
            # Actualizar estadísticas diarias
            daily_stats = user_data['daily_stats'][today]
            daily_stats['query_count'] += 1
            daily_stats['total_time_ms'] += execution_time_ms
            if error:
                daily_stats['error_count'] += 1
            if cache_hit:
                daily_stats['cache_hits'] += 1
            daily_stats['unique_queries'].add(query_record['query_hash'])
            
            # Actualizar patrón horario
            user_data['hourly_pattern'][hour] += 1
            
            # Actualizar patrones de consulta
            query_type = query_record['query_type']
            user_data['query_patterns'][query_type] += 1
            
            # Actualizar estadísticas de rendimiento
            perf_stats = user_data['performance_stats']
            perf_stats['total_queries'] += 1
            perf_stats['fastest_query'] = min(perf_stats['fastest_query'], execution_time_ms)
            perf_stats['slowest_query'] = max(perf_stats['slowest_query'], execution_time_ms)
            
            # Calcular tiempo promedio
            all_times = [q['execution_time_ms'] for q in user_data['queries'] if not q['error']]
            if all_times:
                perf_stats['avg_query_time'] = statistics.mean(all_times)
            
            # Actualizar información de sesión
            user_data['sessions'][session_id].update({
                'last_activity': current_time.isoformat(),
                'query_count': user_data['sessions'][session_id].get('query_count', 0) + 1,
                'user_agent': user_context.user_agent,
                'ip_address': user_context.ip_address
            })
        
        # Enviar métricas a CloudWatch
        self._send_user_metrics(user_hash, execution_time_ms, cache_hit, error is not None)
        
        # Guardar periódicamente en DynamoDB
        if user_data['performance_stats']['total_queries'] % 10 == 0:  # Cada 10 consultas
            self._persist_user_data(user_hash)
    
    def _classify_query(self, query: str) -> str:
        """Clasifica el tipo de consulta SQL"""
        query_lower = query.lower().strip()
        
        if query_lower.startswith('select count('):
            return 'COUNT'
        elif query_lower.startswith('select') and 'group by' in query_lower:
            return 'AGGREGATION'
        elif query_lower.startswith('select') and 'where' in query_lower:
            return 'FILTERED_SELECT'
        elif query_lower.startswith('select'):
            return 'SIMPLE_SELECT'
        elif 'join' in query_lower:
            return 'JOIN'
        else:
            return 'OTHER'
    
    def _send_user_metrics(self, user_hash: str, execution_time_ms: float, cache_hit: bool, has_error: bool):
        """Envía métricas específicas por usuario a CloudWatch"""
        try:
            metrics = [
                {
                    'MetricName': 'QueryExecutionTime',
                    'Dimensions': [
                        {'Name': 'UserHash', 'Value': user_hash},
                        {'Name': 'Environment', 'Value': 'production'}
                    ],
                    'Value': execution_time_ms,
                    'Unit': 'Milliseconds',
                    'Timestamp': datetime.now()
                },
                {
                    'MetricName': 'QueryCount',
                    'Dimensions': [
                        {'Name': 'UserHash', 'Value': user_hash},
                        {'Name': 'CacheHit', 'Value': str(cache_hit)}
                    ],
                    'Value': 1.0,
                    'Unit': 'Count',
                    'Timestamp': datetime.now()
                }
            ]
            
            if has_error:
                metrics.append({
                    'MetricName': 'QueryErrors',
                    'Dimensions': [{'Name': 'UserHash', 'Value': user_hash}],
                    'Value': 1.0,
                    'Unit': 'Count',
                    'Timestamp': datetime.now()
                })
            
            # Enviar en lotes para mejor rendimiento
            self.logger.info(f"[METRIC] Lambda/UserActivity: {json.dumps(metrics)}")

        except Exception as e:
            self.logger.error(f"Error enviando métricas de usuario: {str(e)}")
    
    def _persist_user_data(self, user_hash: str):
        """Persiste datos de usuario en DynamoDB"""
        try:
            user_data = self.user_activities[user_hash]
            
            # Preparar datos para persistencia (convertir sets a listas, etc.)
            persistent_data = {
                'daily_stats': {},
                'hourly_pattern': dict(user_data['hourly_pattern']),
                'query_patterns': dict(user_data['query_patterns']),
                'performance_stats': user_data['performance_stats'].copy(),
                'recent_queries': list(user_data['queries'])[-20:],  # Últimas 20 consultas
                'active_sessions': len(user_data['sessions'])
            }
            
            # Convertir sets en daily_stats
            for date, stats in user_data['daily_stats'].items():
                persistent_data['daily_stats'][date] = {
                    'query_count': stats['query_count'],
                    'total_time_ms': stats['total_time_ms'],
                    'error_count': stats['error_count'],
                    'cache_hits': stats['cache_hits'],
                    'unique_queries_count': len(stats['unique_queries'])
                }
            
        except Exception as e:
            self.logger.error(f"Error persistiendo datos de usuario {user_hash}: {str(e)}")
    
    def get_user_summary(self, user_hash: str) -> dict:
        """Obtiene resumen completo de actividad del usuario"""
        user_data = self.user_activities.get(user_hash, {})
        
        # Calcular estadísticas en tiempo real
        recent_queries = list(user_data['queries'])
        today = datetime.now().date().isoformat()
        
        summary = {
            'user_hash': user_hash,
            'total_queries': user_data['performance_stats']['total_queries'],
            'avg_query_time_ms': round(user_data['performance_stats']['avg_query_time'], 2),
            'fastest_query_ms': user_data['performance_stats']['fastest_query'] if user_data['performance_stats']['fastest_query'] != float('inf') else 0,
            'slowest_query_ms': user_data['performance_stats']['slowest_query'],
            'active_sessions': len(user_data['sessions']),
            'today_queries': user_data['daily_stats'][today]['query_count'],
            'today_errors': user_data['daily_stats'][today]['error_count'],
            'today_cache_hits': user_data['daily_stats'][today]['cache_hits'],
            'most_active_hour': max(user_data['hourly_pattern'].items(), key=lambda x: x[1])[0] if user_data['hourly_pattern'] else None,
            'top_query_types': dict(sorted(user_data['query_patterns'].items(), key=lambda x: x[1], reverse=True)[:5]),
            'recent_activity': recent_queries[-10:],  # Últimas 10 consultas
            'last_activity': recent_queries[-1]['timestamp'] if recent_queries else None
        }
        
        return summary
    
    def get_all_users_summary(self) -> dict:
        """Obtiene resumen de todos los usuarios activos"""
        summaries = {}
        for user_hash in self.user_activities.keys():
            summaries[user_hash] = self.get_user_summary(user_hash)
        return summaries
    
    def cleanup_old_data(self, days_to_keep: int = 7):
        """Limpia datos antiguos para optimizar memoria"""
        cutoff_date = datetime.now() - timedelta(days=days_to_keep)
        
        with self.lock:
            for user_hash, user_data in self.user_activities.items():
                # Limpiar estadísticas diarias antiguas
                old_dates = [date for date in user_data['daily_stats'].keys() 
                           if datetime.fromisoformat(date).date() < cutoff_date.date()]
                for date in old_dates:
                    del user_data['daily_stats'][date]
                
                # Limpiar sesiones inactivas
                inactive_sessions = []
                for session_id, session_info in user_data['sessions'].items():
                    if datetime.fromisoformat(session_info['last_activity']) < cutoff_date:
                        inactive_sessions.append(session_id)
                
                for session_id in inactive_sessions:
                    del user_data['sessions'][session_id]


class EnhancedUserContext:
    """UserContext extendido con capacidades de sesión avanzadas"""
    
    def __init__(self, user_id=None, session_id=None, ip_address=None, user_agent=None, user_email: str=None, username: str=None, nickname: str=None, phone_number: str=None, name: str=None, work_area: str=None, activity_tracker: UserActivityTracker = None):
        self.user_id = user_id or "anonymous"
        self.session_id = session_id
        self.user_email = user_email
        self.username = username
        self.work_area = work_area
        self.ip_address = ip_address or "unknown"
        self.user_agent = user_agent or "unknown"
        self.request_timestamp = datetime.now().isoformat()
        self.bedrock_session = {}
        self.session_start_time = datetime.now()
        self.activity_tracker = activity_tracker
        self.nickname = nickname
        self.name = name
        self.phone_number = phone_number
        
    def get_user_hash(self):
        """Genera un hash del usuario para privacidad"""
        if self.user_id != "anonymous":
            return hashlib.sha256(self.user_id.encode()).hexdigest()[:8]
        return "anon"
    
    def to_dict(self):
        """Convierte el contexto a diccionario para logging"""
        return {
            "user_hash": self.get_user_hash(),
            "session_id": self.session_id,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent[:50],
            "timestamp": self.request_timestamp
        }
    
    def update_bedrock_session(self, session_info: dict):
        """Actualiza información de sesión de Bedrock"""
        self.bedrock_session = session_info
    
    def get_session_summary(self) -> dict:
        """Obtiene resumen de la sesión actual"""
        return self.activity_tracker.get_user_summary(self.get_user_hash())
    
    def update_use_tokens(self, trace_data: dict, source: str):
        user_id = self.user_email if source == "google_chat" else self.phone_number

        usage = (
            trace_data.get("modelInvocationOutput", {})
            .get("metadata", {})
            .get("usage", {})
        )
        total = usage.get("inputTokens", 0) + usage.get("outputTokens", 0)

        response = usage_table.get_item(Key={"user_id": user_id})
        item = response.get("Item")
        if not item:
            usage_table.put_item(Item={
                "user_id": user_id,
                "tokens_usados": total,
                "fecha_ultimo_uso": str(date.today()),
                "limite_tokens": LIMITE_TOKENS_DIARIO,
                "channel": source
            })
        else:
            nuevo_total = item["tokens_usados"] + total

            usage_table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET tokens_usados = :nuevo, fecha_ultimo_uso = :fecha",
                ExpressionAttributeValues={
                    ":nuevo": nuevo_total,
                    ":fecha": date.today().isoformat()
                }
            )

    def validate_use_tokens(self, source: str):

        user_id = self.user_email if source == "google_chat" else self.phone_number

        response = usage_table.get_item(Key={"user_id": user_id})
        item = response.get("Item")

        if not item:
            return True
        
        hoy = date.today().isoformat()
        if item["fecha_ultimo_uso"] != hoy:
            item["tokens_usados"] = 0
            item["fecha_ultimo_uso"] = hoy

            usage_table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET tokens_usados = :nuevo, fecha_ultimo_uso = :fecha",
                ExpressionAttributeValues={
                    ":nuevo": item["tokens_usados"],
                    ":fecha": item["fecha_ultimo_uso"]
                }
            )

        limite = item.get("limite_tokens", LIMITE_TOKENS_DIARIO)

        if item["tokens_usados"] > limite:
            return False
        
        return True
    
    def metrics_questions(self, total_ms: int, total_tokens: int, total_steps: int, input: str, total_ms_lambda: int):
        metrics_table.put_item(Item={
                "question": input,
                "tokens": total_tokens,
                "date_questions": str(date.today()),
                "steps": total_steps,
                "total_ms": total_ms,
                "total_ms_lambda": total_ms_lambda,
            })

    
class StructuredUserLogger:
    """Logger estructurado optimizado para CloudWatch con filtros por usuario"""
    
    def __init__(self, logger: Logger, activity_tracker: UserActivityTracker):
        self.logger = logger
        self.activity_tracker = activity_tracker
    
    def log_user_request(self, user_context: EnhancedUserContext, event_type: str, details: dict = None):
        """Log estructurado de request con información de usuario"""
        log_entry = {
            "log_type": "USER_REQUEST",
            "timestamp": datetime.now().isoformat(),
            "user_hash": user_context.get_user_hash(),
            "user_email": user_context.user_email,
            "username": user_context.username,
            "session_id": getattr(user_context, 'bedrock_session', {}).get('session_id', user_context.session_id),
            "event_type": event_type,
            "ip_address": user_context.ip_address,
            "user_agent": user_context.user_agent,
            "details": details or {}
        }
        
        # Log con formato JSON para fácil filtrado en CloudWatch
        self.logger.info(json.dumps(log_entry))
    
    def log_user_sql_execution(self, user_context, query: str, 
                              execution_time_ms: float, result_count: int, 
                              cache_hit: bool = False, error: str = None):
        """Log específico de ejecución SQL con métricas por usuario"""
        user_hash = user_context.get_user_hash()
        session_id = getattr(user_context, 'bedrock_session', {}).get('session_id', user_context.session_id)
        
        log_entry = {
            "log_type": "USER_SQL_EXECUTION",
            "timestamp": datetime.now().isoformat(),
            "user_hash": user_hash,
            "session_id": session_id,
            "query_preview": query[:100],
            "query_type": self.activity_tracker._classify_query(query),
            "execution_time_ms": execution_time_ms,
            "result_count": result_count,
            "cache_hit": cache_hit,
            "success": error is None,
            "error_type": type(error).__name__ if error else None
        }
        
        # Log estructurado
        self.logger.info(json.dumps(log_entry))
        
        # Registrar en el tracker de actividad
        self.activity_tracker.log_query_activity(
            user_context, query, execution_time_ms, result_count, cache_hit, str(error) if error else None
        )
    
    def log_user_error(self, user_context, error_type: str, 
                      error_message: str, additional_info: dict = None):
        """Log estructurado de errores por usuario"""
        log_entry = {
            "log_type": "USER_ERROR",
            "timestamp": datetime.now().isoformat(),
            "user_hash": user_context.get_user_hash(),
            "session_id": getattr(user_context, 'bedrock_session', {}).get('session_id', user_context.session_id),
            "error_type": error_type,
            "error_message": error_message,
            "additional_info": additional_info or {}
        }
        
        self.logger.error(json.dumps(log_entry))
    
    def log_user_session_event(self, user_context: EnhancedUserContext, event_type: str, 
                              session_info: dict = None):
        """Log eventos específicos de sesión"""
        log_entry = {
            "log_type": "USER_SESSION_EVENT",
            "timestamp": datetime.now().isoformat(),
            "user_hash": user_context.get_user_hash(),
            "user_email": user_context.user_email,
            "username": user_context.username,
            "session_id": getattr(user_context, 'bedrock_session', {}).get('session_id', user_context.session_id),
            "event_type": event_type,  # NEW_SESSION, SESSION_RESUME, SESSION_END
            "session_info": session_info or {}
        }
        
        self.logger.info(json.dumps(log_entry))


class EnhancedQueryExecutor:
    """QueryExecutor mejorado con logging completo por usuario"""
    
    def __init__(self, logger: Logger, user_logger: StructuredUserLogger, validator):
        self.logger = logger
        self.user_logger = user_logger
        self.validator = validator
        self.cloudwatch = boto3.client('cloudwatch')
        self.query_corrections = {}

    def track_metrics(self, query_time, cache_hit):
        self.logger.info(f"Métricas: Tiempo consulta: {query_time:.2f} ms, Cache hit: {cache_hit}")

    def logg_querys(self, query, message, query_id):
        query_info = {
            "evento": "QUERY CONSULT",
            "message": message,
            "query_id": query_id,
            "query": " ".join(query.split())
        }

        self.logger.info(json.dumps(query_info))

    def execute(self, query: str, user_context: EnhancedUserContext = None) -> Dict[str, Any]:
        start_time = pytime.time()
        cache_hit = False
        conn = None
        results = None
        query_id = str(uuid.uuid4())[:8]
        original_query = query
        error_occurred = None

        # Si no hay contexto de usuario, crear uno básico
        if user_context is None:
            user_context = EnhancedUserContext()

        try:
            # Log inicio de request con el sistema mejorado
            self.user_logger.log_user_request(user_context, "SQL_QUERY_START", {
                "query_id": query_id,
                "query_preview": query[:50]
            })

            self.logg_querys(query, "Query SQL Original", query_id)
        
            # Procesar y validar la consulta
            processed_query = self.validator.validate_stage1(query)
            corrected_query = self.validator.validate_stage2(processed_query)
            
            # Guardar la corrección
            if not hasattr(self, 'query_corrections'):
                self.query_corrections = {}
            self.query_corrections[query] = corrected_query
            query = corrected_query
        
            self.logg_querys(query, "Query SQL Validada", query_id)

            self.logger.info(f"[{query_id}][{user_context.get_user_hash()}] Conexión obtenida.")

            with SessionLocal() as session:
                db_start_time = pytime.time()
                self.logg_querys(query, "Ejecutando SQL", query_id)
                
                result = session.execute(text(query))

                try:
                    rows = result.fetchall()
                    results = [dict(row._mapping) for row in rows]
                except Exception:
                    results = []

                db_execution_time = (pytime.time() - db_start_time) * 1000
                
                self.logger.info(f"[{query_id}][{user_context.get_user_hash()}] Ejecutada. Filas: {len(results)}")
                
                # Limitar tamaño de resultados
                if len(results) > 500:
                    self.logger.warning(f"[{query_id}][{user_context.get_user_hash()}] Resultados limitados a 500 de {len(results)}")
                    results = results[:500]
                
                # Truncar campos largos
                for row in results:
                    for key, value in row.items():
                        if isinstance(value, str) and len(value) > 1000:
                            row[key] = value[:1000] + "..."
                
                total_execution_time = (pytime.time() - start_time) * 1000
                
                # Log con el sistema mejorado
                self.user_logger.log_user_sql_execution(
                    user_context, query, total_execution_time, len(results), cache_hit=False
                )
                
                return {"results": results, "cache_hit": False, "db_time_ms": db_execution_time}

        except SQLAlchemyError as db_error:
            error_occurred = db_error
            execution_time = (pytime.time() - start_time) * 1000
            error_details = {
            "error_type": type(db_error).__name__,
            "error_message": str(db_error),
            "query": query
        }
            
            # Log error con el sistema mejorado
            self.user_logger.log_user_error(
                user_context, 
                "DATABASE_ERROR", 
                error_details["error_message"],
                {
                    "query_id": query_id,
                    "execution_time_ms": execution_time,
                    "query_preview": query[:100]
                }
            )
            
            # También registrar en el tracker de actividad
            self.user_logger.log_user_sql_execution(
                user_context, query, execution_time, 0, cache_hit=False, error=str(db_error)
            )
            
            self.logger.error(f"[{query_id}][{user_context.get_user_hash()}] ERROR DB: {json.dumps(error_details)}")
            return {"error": f"Error de base de datos ({error_details['error_type']}): {error_details['error_message']}"}

        except Exception as e:
            error_occurred = e
            execution_time = (pytime.time() - start_time) * 1000
            stack_trace = traceback.format_exc()
            
            # Log error con el sistema mejorado
            self.user_logger.log_user_error(
                user_context,
                "EXECUTION_ERROR",
                str(e),
                {
                    "query_id": query_id,
                    "execution_time_ms": execution_time,
                    "query_preview": query[:100],
                    "stack_trace": stack_trace
                }
            )
            
            # También registrar en el tracker de actividad
            self.user_logger.log_user_sql_execution(
                user_context, query, execution_time, 0, cache_hit=False, error=str(e)
            )
            
            self.logger.error(f"[{query_id}][{user_context.get_user_hash()}] ERROR: {type(e).__name__}: {str(e)}")
            self.logger.error(f"[{query_id}][{user_context.get_user_hash()}] STACK: {stack_trace}")
            return {"error": "Error inesperado en la ejecución de la consulta."}

        finally:
            query_time = (pytime.time() - start_time) * 1000
            self.track_metrics(query_time, cache_hit)
            self.logger.info(f"[{query_id}][{user_context.get_user_hash()}] Tiempo total: {query_time:.2f}ms, Cache: {cache_hit}")