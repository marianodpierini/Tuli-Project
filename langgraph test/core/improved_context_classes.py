import hashlib
import re
import traceback
import concurrent.futures
import os
import uuid
import json
import time as pytime
import boto3

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional, TypedDict
from logging import Logger
from langsmith import traceable, get_current_run_tree

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from botocore.exceptions import ClientError
from langgraph.graph import StateGraph
from langgraph.graph import END

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


class AgentState(TypedDict):
    question: str
    needs_sql: bool
    needs_rag: bool
    sql_result: Optional[str]
    rag_result: Optional[str]
    final_answer: Optional[str]


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

    def preprocess_query(self, query):
        """Preprocesa y limpia la consulta SQL para su ejecución"""
        try:
            query_info = {
                "evento": "QUERY CONSULT",
                "message": "Query preprocesamiento",
                "query": " ".join(query.split()),
            }

            self.logger.info(json.dumps(query_info))

            if isinstance(query, dict):
                if "sql" in query:
                    query = query["sql"]
                elif "query" in query:
                    query = query["query"]
                else:
                    query = str(query)
            elif isinstance(query, str):
                if query.strip().startswith("{") and query.strip().endswith("}"):
                    try:
                        parsed = json.loads(query)
                        if isinstance(parsed, dict):
                            if "sql" in parsed:
                                query = parsed["sql"]
                            elif "query" in parsed:
                                query = parsed["query"]
                    except json.JSONDecodeError:
                        query = (
                            query.replace("{sql=", "")
                            .replace("{query=", "")
                            .replace("}", "")
                        )

            query = query.strip()
            query = query.replace('"', '"').replace("\\'", "'")
            query = query.replace("\\n", " ").replace("\\r", " ")
            query = " ".join(query.split())

            query_info["query"] = query
            query_info["message"] = "Query despues del preprocesamiento"

            self.logger.info(json.dumps(query_info))
            return query
        except Exception as e:
            self.logger.error(
                f"Error preprocessing query: {str(e)}\n{traceback.format_exc()}"
            )
            raise ValueError(f"Invalid query format: {str(e)}")

    def validate_stage1(self, query):
        """Enhanced first-stage validation"""
        try:
            processed_query = self.preprocess_query(query)

            if not processed_query or len(processed_query.strip()) == 0:
                self.logger.warning("Se recibió una consulta vacía")
                raise ValueError("La consulta no puede estar vacía")

            if re.search(
                rf"\b({'|'.join(PROHIBITED_KEYWORDS)})\b", processed_query.lower()
            ):
                self.logger.warning("Se detectaron palabras prohibidas en la consulta")
                return "SELECT NULL AS resultado"

            return processed_query

        except Exception as e:
            self.logger.error(f"Error en validate_stage1: {str(e)}")
            raise

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

    def validate_stage2(self, query):
        """Versión optimizada para rendimiento con corrección automática de esquema"""
        query = self.add_schema_prefix_if_missing(query)

        table_check_passed = False
        for table in self.allowed_tables:
            table_name = table.split(".")[-1]
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

        try:
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
        except ClientError as e:
            self.logger.error(f"DynamoDB ClientError in update_use_tokens for user {user_id}: {e}")
        except Exception as e:
            self.logger.error(f"Unexpected error in update_use_tokens for user {user_id}: {e}")

    def validate_use_tokens(self, source: str):

        user_id = self.user_email if source == "google_chat" else self.phone_number

        try:
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
                    ExpressionAttributeValues={":nuevo": item["tokens_usados"], ":fecha": item["fecha_ultimo_uso"]},
                )

            limite = item.get("limite_tokens", LIMITE_TOKENS_DIARIO)
            return item["tokens_usados"] <= limite
        except ClientError as e:
            self.logger.error(f"DynamoDB ClientError in validate_use_tokens for user {user_id}: {e}")
            return False # Assume token validation fails on error
        except Exception as e:
            self.logger.error(f"Unexpected error in validate_use_tokens for user {user_id}: {e}")
            return False # Assume token validation fails on error

    def metrics_questions(
        self,
        total_ms: int,
        total_tokens: int,
        total_steps: int,
        input: str,
        total_ms_lambda: int,
    ):
        metrics_table.put_item(
            Item={
                "question": input,
                "tokens": total_tokens,
                "date_questions": str(date.today()),
                "steps": total_steps,
                "total_ms": total_ms,
                "total_ms_lambda": total_ms_lambda,
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
        query_info = {
            "evento": "QUERY CONSULT",
            "message": message,
            "query_id": query_id,
            "query": " ".join(query.split()),
        }

        self.logger.info(json.dumps(query_info))

    def execute(
        self, query: str, user_context: EnhancedUserContext = None
    ) -> Dict[str, Any]:
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
            if not hasattr(self, "query_corrections"):
                self.query_corrections = {}
            self.query_corrections[query] = corrected_query
            query = corrected_query

            self.logg_querys(query, "Query SQL Validada", query_id)

            self.logger.info(
                f"[{query_id}][{user_context.get_user_hash()}] Conexión obtenida."
            )

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

                return {
                    "results": results,
                    "cache_hit": False,
                    "db_time_ms": db_execution_time,
                }

        except SQLAlchemyError as db_error:
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


class LanggraphManager:
    def __init__(
        self,
        logger: Logger,
        sql_agent: Any,
        rag_agent: Any,
        bedrock_model_id: Optional[str] = None,
    ):
        self.logger = logger
        self.sql_agent = sql_agent
        self.rag_agent = rag_agent
        self.bedrock_model_id = bedrock_model_id or os.getenv(
            "LANGGRAPH_BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        )
        self.client_lggp = boto3.client("bedrock-runtime", region_name="us-east-1")
        self.graph = self.build_graph()

    @traceable(run_type="llm", name="Bedrock Direct Invoke")
    def invoke_bedrock(self, prompt):

        response = self.client_lggp.invoke_model(
            modelId=self.bedrock_model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 500,
                    "temperature": 0.0,
                    "messages": [{"role": "user", "content": prompt}],
                }
            ),
        )

        try:
            result = json.loads(response["body"].read())
            usage = result.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)

            run_tree = get_current_run_tree()
            if run_tree:
                run_tree.end(outputs={"output": result}, usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens})

            llm_output = result["content"][0]["text"].strip().lower()
            self.logger.info(f"LLM Prompt: {prompt[:200]}...")
            self.logger.info(f"LLM Response: {llm_output[:200]}...")
            return llm_output
        except ClientError as e:
            self.logger.error(f"Bedrock Client Error: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Error parsing LLM response or general error: {e}")
            raise

    def supervisor_node(self, state: AgentState):
        self.logger.info(f"Supervisor received question: {state['question']}")
        question = state["question"]

        prompt = [
            {
                "type": "text",
                "text": f"""
            You are a router.

            Decide how to answer the question.

            Return ONLY one of these words:
            SQL
            RAG
            BOTH

            Question:
            {question}
            """,
            }
        ]

        decision = self.invoke_bedrock(prompt).strip().upper()

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

    def parallel_agent_executor_node(self, state: AgentState):
        self.logger.info("Executing SQL and RAG agents in parallel.")
        question = state["question"]

        sql_result = None
        rag_result = None

        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_sql = executor.submit(self.sql_agent.execute_sql_agent, question)
            future_rag = executor.submit(self.rag_agent.execute_rag_agent, question)

            sql_result = future_sql.result()
            rag_result = future_rag.result()

        state["sql_result"] = sql_result
        state["rag_result"] = rag_result
        return state

    def synthesis_node(self, state: AgentState):
        self.logger.info(f"Synthesis node received state: {state}")

        needs_sql = state.get("needs_sql")
        needs_rag = state.get("needs_rag")

        if needs_sql and not state.get("sql_result"):
            return state

        if needs_rag and not state.get("rag_result"):
            return state

        context_text = f"""
        You are an expert assistant.

        Combine the following sources into a single clear answer.

        If both sources are present:
        - Merge them coherently, prioritizing SQL data if it directly answers the question.
        - Do not repeat information, but ensure all relevant details are included.

        If only one exists:
        - Answer using that source

        If neither SQL nor RAG results are available, state that you cannot find relevant information.

        SQL Results:
        {state.get("sql_result")}

        RAG:
        {state.get("rag_result")}
        """

        final = self.invoke_bedrock(
            [
                {
                    "type": "text",
                    "text": f"Generate a final answer using this context:\n{context_text}",
                }
            ]
        )

        state["final_answer"] = final
        return state

    def route_from_supervisor(self, state: AgentState):
        """Determines the next step from the supervisor."""
        if state["needs_sql"] and state["needs_rag"]:
            return "parallel_execution_path"
        elif state["needs_sql"]:
            return "sql_only_path"
        elif state["needs_rag"]:
            return "rag_only_path"
        else:
            return "end"

    def build_graph(self):
        builder = StateGraph(AgentState)

        builder.add_node("supervisor", self.supervisor_node)
        builder.add_node("sql_agent", self.sql_agent_node)
        builder.add_node("rag_agent", self.rag_agent_node)
        builder.add_node("parallel_agent_executor", self.parallel_agent_executor_node)
        builder.add_node("synthesis", self.synthesis_node)

        builder.set_entry_point("supervisor")

        builder.add_conditional_edges(
            "supervisor",
            self.route_from_supervisor,
            {
                "parallel_execution_path": "parallel_agent_executor",
                "sql_only_path": "sql_agent",
                "rag_only_path": "rag_agent",
                "end": END,
            },
        )

        builder.add_edge("parallel_agent_executor", "synthesis")
        builder.add_edge("sql_agent", "synthesis")
        builder.add_edge("rag_agent", "synthesis")
        builder.add_edge("synthesis", END)

        return builder.compile()
