import hashlib
import re
import traceback
import uuid
import json
import time as pytime
import boto3

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional, TypedDict
from logging import Logger

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from langgraph.graph import StateGraph
from langgraph.graph import END

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
    

class AgentState(TypedDict):
    question: str
    needs_sql: bool
    needs_rag: bool
    sql_result: Optional[str]
    rag_result: Optional[str]
    final_answer: Optional[str]
    

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

class EnhancedUserContext:
    """UserContext extendido con capacidades de sesión avanzadas"""
    
    def __init__(self, user_id=None, session_id=None, ip_address=None, user_agent=None, user_email: str=None, username: str=None, nickname: str=None, phone_number: str=None, name: str=None, work_area: str=None):
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
            "timestamp": self.request_timestamp
        }
    
    def update_bedrock_session(self, session_info: dict):
        """Actualiza información de sesión de Bedrock"""
        self.bedrock_session = session_info
    
    def update_use_tokens(self, source: str, trace_data: Optional[dict] = None, total_tokens: Optional[int] = None):
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


class EnhancedQueryExecutor:
    """QueryExecutor mejorado con logging completo por usuario"""
    
    def __init__(self, logger: Logger, validator):
        self.logger = logger
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
        results = None
        query_id = str(uuid.uuid4())[:8]

        # Si no hay contexto de usuario, crear uno básico
        if user_context is None:
            user_context = EnhancedUserContext()

        try:

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
                
                
                return {"results": results, "cache_hit": False, "db_time_ms": db_execution_time}

        except SQLAlchemyError as db_error:
            error_details = {
            "error_type": type(db_error).__name__,
            "error_message": str(db_error),
            "query": query
        }
            
            
            self.logger.error(f"[{query_id}][{user_context.get_user_hash()}] ERROR DB: {json.dumps(error_details)}")
            return {"error": f"Error de base de datos ({error_details['error_type']}): {error_details['error_message']}"}

        except Exception as e:
            stack_trace = traceback.format_exc()
            
            self.logger.error(f"[{query_id}][{user_context.get_user_hash()}] ERROR: {type(e).__name__}: {str(e)}")
            self.logger.error(f"[{query_id}][{user_context.get_user_hash()}] STACK: {stack_trace}")
            return {"error": "Error inesperado en la ejecución de la consulta."}

        finally:
            query_time = (pytime.time() - start_time) * 1000
            self.track_metrics(query_time, cache_hit)
            self.logger.info(f"[{query_id}][{user_context.get_user_hash()}] Tiempo total: {query_time:.2f}ms, Cache: {cache_hit}")


class LanggraphManager:
    def __init__(self, logger: Logger, sql_agent, rag_agent):
            self.logger = logger
            self.sql_agent = sql_agent
            self.rag_agent = rag_agent
            self.client_lggp = boto3.client("bedrock-runtime", region_name="us-east-1")
            self.graph = self.build_graph()

    def invoke_bedrock(self, prompt):

        response = self.client_lggp.invoke_model(
            modelId="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 500,
                "temperature": 0.0,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt
                    }
                ]
            })
        )

        result = json.loads(response["body"].read())

        return result["content"][0]["text"].strip().lower()

    def supervisor_node(self, state: AgentState):
        self.logger.info(f"Supervisor received question: {state['question']}")
        question = state["question"]

        prompt = f"""
            You are a router.

            Decide how to answer the question.

            Return ONLY one of these words:
            SQL
            RAG
            BOTH

            Question:
            {question}
        """

        decision = self.invoke_bedrock(prompt).upper()

        state["needs_sql"] = decision in ["SQL", "BOTH"]
        state["needs_rag"] = decision in ["RAG", "BOTH"]

        return state
    
    def sql_agent_node(self, state: AgentState):
        self.logger.info(f"SQL Agent received question: {state['question']}")
        result = self.sql_agent.execute_sql_agent(state["question"])
        state["sql_result"] = result
        return state
    
    def rag_agent_node(self, state: AgentState):
        self.logger.info(f"RAG Agent received question: {state['question']}")
        result = self.rag_agent.execute_rag_agent(state["question"])
        state["rag_result"] = result
        return state
    
    def synthesis_node(self, state: AgentState):
        self.logger.info(f"Synthesis node received state: {state}")
        context = f"""
        SQL Result:
        {state.get("sql_result")}

        RAG Result:
        {state.get("rag_result")}
        """

        prompt = f"Generate a final answer using this context:\n{context}"

        final = self.invoke_bedrock(prompt)

        state["final_answer"] = final
        return state
    
    def route_from_supervisor(self, state: AgentState):
        if state["needs_sql"] and state["needs_rag"]:
            return "both"
        if state["needs_sql"]:
            return "sql"
        if state["needs_rag"]:
            return "rag"
        return "end"
    
    def build_graph(self):
        builder = StateGraph(AgentState)

        builder.add_node("supervisor", self.supervisor_node)
        builder.add_node("sql_agent", self.sql_agent_node)
        builder.add_node("rag_agent", self.rag_agent_node)
        builder.add_node("synthesis", self.synthesis_node)

        builder.set_entry_point("supervisor")

        builder.add_conditional_edges(
            "supervisor",
            self.route_from_supervisor,
            {
                "sql": "sql_agent",
                "rag": "rag_agent",
                "both": "sql_agent",  # primero SQL, luego RAG
                "end": END
            }
        )

        builder.add_edge("sql_agent", "synthesis")
        builder.add_edge("rag_agent", "synthesis")

        return builder.compile()