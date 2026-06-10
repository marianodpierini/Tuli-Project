import hashlib
import re
import traceback
import uuid
import json
import time as pytime
import boto3

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional
from logging import Logger

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.dialects import postgresql

from core.database.db import SessionLocal

PROHIBITED_KEYWORDS = [
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "truncate",
    "create",
]
LIMITE_TOKENS_DIARIO = 550000
dynamodb = boto3.resource("dynamodb")
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
    def __init__(
        self,
        logger: Logger,
        allowed_tables,
        max_query_length=2000,
        query_timeout_ms=10000,
    ):
        self.logger = logger
        self.allowed_tables = allowed_tables
        self.max_query_length = max_query_length
        self.query_timeout_ms = query_timeout_ms

    def add_schema_prefix_if_missing(self, query):
        """Añade el prefijo de esquema si falta en la consulta"""
        for allowed_table in self.allowed_tables:
            if "." in allowed_table:
                schema, table_name = allowed_table.split(".")
                pattern = rf"\bFROM\s+{table_name}\b"
                if re.search(pattern, query, re.IGNORECASE):
                    query = re.sub(
                        pattern,
                        f"FROM {schema}.{table_name}",
                        query,
                        flags=re.IGNORECASE,
                    )
                pattern = rf"\bJOIN\s+{table_name}\b"
                if re.search(pattern, query, re.IGNORECASE):
                    query = re.sub(
                        pattern,
                        f"JOIN {schema}.{table_name}",
                        query,
                        flags=re.IGNORECASE,
                    )
        return query


class EnhancedUserContext:
    """UserContext extendido con capacidades de sesión avanzadas"""

    def __init__(
        self,
        user_id=None,
        session_id=None,
        ip_address=None,
        user_agent=None,
        user_email: str = None,
        username: str = None,
        nickname: str = None,
        phone_number: str = None,
        name: str = None,
        work_area: str = None,
    ):
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
            "timestamp": self.request_timestamp,
        }

    def update_bedrock_session(self, session_info: dict):
        """Actualiza información de sesión de Bedrock"""
        self.bedrock_session = session_info

    def update_use_tokens(
        self,
        source: str,
        trace_data: Optional[dict] = None,
        total_tokens: Optional[int] = None,
    ):
        user_id = self.user_email if source == "google_chat" else self.phone_number

        if trace_data is not None:
            usage = (
                trace_data.get("modelInvocationOutput", {})
                .get("metadata", {})
                .get("usage", {})
            )
            total = usage.get("inputTokens", 0) + usage.get("outputTokens", 0)

        if total_tokens is not None:
            total = total_tokens

        response = usage_table.get_item(Key={"user_id": user_id})
        item = response.get("Item")
        if not item:
            usage_table.put_item(
                Item={
                    "user_id": user_id,
                    "tokens_usados": total,
                    "fecha_ultimo_uso": str(date.today()),
                    "limite_tokens": LIMITE_TOKENS_DIARIO,
                    "channel": source,
                }
            )
        else:
            nuevo_total = item["tokens_usados"] + total

            usage_table.update_item(
                Key={"user_id": user_id},
                UpdateExpression="SET tokens_usados = :nuevo, fecha_ultimo_uso = :fecha",
                ExpressionAttributeValues={
                    ":nuevo": nuevo_total,
                    ":fecha": date.today().isoformat(),
                },
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
                    ":fecha": item["fecha_ultimo_uso"],
                },
            )

        limite = item.get("limite_tokens", LIMITE_TOKENS_DIARIO)

        if item["tokens_usados"] > limite:
            return False

        return True

    def metrics_questions(
        self,
        total_ms: int,
        total_tokens: int,
        total_steps: int,
        input: str,
        total_ms_lambda: int,
        output: str,
    ):
        metrics_table.put_item(
            Item={
                "question": input,
                "tokens": total_tokens,
                "date_questions": datetime.now().isoformat(),
                "steps": total_steps,
                "total_ms": total_ms,
                "total_ms_lambda": total_ms_lambda,
                "output": output,
                "user_id": self.user_email,
            }
        )


class EnhancedQueryExecutor:
    """QueryExecutor mejorado con logging completo por usuario"""

    def __init__(self, logger: Logger, validator):
        self.logger = logger
        self.validator = validator
        self.cloudwatch = boto3.client("cloudwatch")
        self.query_corrections = {}

    def track_metrics(self, query_time, cache_hit):
        self.logger.info(
            f"Métricas: Tiempo consulta: {query_time:.2f} ms, Cache hit: {cache_hit}"
        )

    def logg_querys(self, query, message, query_id):
        sql = query.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True}
        )
        query_info = {
            "evento": "QUERY CONSULT",
            "message": message,
            "query_id": query_id,
            "query": " ".join(str(sql).split()),
        }

        self.logger.info(json.dumps(query_info))

    def execute(
        self, query: str, user_context: EnhancedUserContext = None
    ) -> Dict[str, Any]:
        start_time = pytime.time()
        cache_hit = False
        conn = None
        results = None
        query_id = str(uuid.uuid4())[:8]
        original_query = query
        error_occurred = None

        if user_context is None:
            user_context = EnhancedUserContext()

        try:
            with SessionLocal() as session:
                db_start_time = pytime.time()
                self.logg_querys(query, "Ejecutando SQL", query_id)

                result = session.execute(query)

                try:
                    rows = result.fetchall()
                    results = [dict(row._mapping) for row in rows]
                except Exception:
                    results = []

                db_execution_time = (pytime.time() - db_start_time) * 1000

                self.logger.info(
                    f"[{query_id}][{user_context.get_user_hash()}] Ejecutada. Filas: {len(results)}"
                )

                # Limitar tamaño de resultados
                if len(results) > 500:
                    self.logger.warning(
                        f"[{query_id}][{user_context.get_user_hash()}] Resultados limitados a 500 de {len(results)}"
                    )
                    results = results[:500]

                # Truncar campos largos
                for row in results:
                    for key, value in row.items():
                        if isinstance(value, str) and len(value) > 1000:
                            row[key] = value[:1000] + "..."

                total_execution_time = (pytime.time() - start_time) * 1000

                return {
                    "results": results,
                    "cache_hit": False,
                    "db_time_ms": db_execution_time,
                }

        except SQLAlchemyError as db_error:
            error_occurred = db_error
            execution_time = (pytime.time() - start_time) * 1000
            error_details = {
                "error_type": type(db_error).__name__,
                "error_message": str(db_error),
                "query": query,
            }

            self.logger.error(
                f"[{query_id}][{user_context.get_user_hash()}] ERROR DB: {json.dumps(error_details)}"
            )
            return {
                "error": f"Error de base de datos ({error_details['error_type']}): {error_details['error_message']}"
            }

        except Exception as e:
            error_occurred = e
            execution_time = (pytime.time() - start_time) * 1000
            stack_trace = traceback.format_exc()

            self.logger.error(
                f"[{query_id}][{user_context.get_user_hash()}] ERROR: {type(e).__name__}: {str(e)}"
            )
            self.logger.error(
                f"[{query_id}][{user_context.get_user_hash()}] STACK: {stack_trace}"
            )
            return {"error": "Error inesperado en la ejecución de la consulta."}

        finally:
            query_time = (pytime.time() - start_time) * 1000
            self.track_metrics(query_time, cache_hit)
            self.logger.info(
                f"[{query_id}][{user_context.get_user_hash()}] Tiempo total: {query_time:.2f}ms, Cache: {cache_hit}"
            )
