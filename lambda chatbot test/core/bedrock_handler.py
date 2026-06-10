import json
import traceback
import uuid
import time as pytime
import botocore

from datetime import datetime

from sqlalchemy import select, func, update, text, or_

from core.improved_context_classes import EnhancedUserContext, CustomJSONEncoder
from core.database.db import SessionLocal, engine
from core.database.models import (
    ServiciosTcktsRvas,
    SuggestedQuestions,
    TableMetadata,
    ColumnMetadata,
    Glossary,
    BusinessRules,
    TipsOperadores,
)

from core.helpers.helpers import (
    cohere_embed,
)


from core.request_handler import RequestHandler

ALLOWED_PROCEDURES = ["objetivos_departamentales"]

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


class BedrockRequestHandler(RequestHandler):
    def __init__(self, logger, req_id, event, lambda_handler):
        super().__init__(logger, req_id, event, lambda_handler)
        self.event_id = str(uuid.uuid4())[:8]
        self.start_time = pytime.time()
        self.bedrock_session_id = self.event.session_id
        self.routes = {
            "/consulta": self.handle_consulta,
            "/schema": self.handle_schema,
            "/stored_procedure": self.handle_stored_procedure,
            "/discover_tables": self.handle_discover_tables,
            "/buscar_operadores": self.handle_buscar_operadores,
        }

    def format_response_for_bedrock(
        self,
        action_group,
        api_path,
        http_method,
        data,
        status_code=200,
        session_attributes=None,
        prompt_session_attributes=None,
    ):
        if session_attributes is None:
            session_attributes = {}
        if prompt_session_attributes is None:
            prompt_session_attributes = {}

        # Verificar si la respuesta es muy grande
        try:
            json_body = json.dumps(data, cls=CustomJSONEncoder)
            response_size = len(json_body)

            if response_size > 20000:  # 20KB para dejar margen
                self.logger.info(
                    f"Respuesta grande detectada: {response_size} bytes. Aplicando compresión."
                )
                compressed_data = self.compress_data(data)
                data = {
                    "compressed_data": compressed_data,
                    "compression_method": "gzip_base64",
                    "original_size": response_size,
                    "message": "Datos comprimidos debido al tamaño de respuesta",
                }
                json_body = json.dumps(data, cls=CustomJSONEncoder)
                self.logger.info(
                    f"Tamaño después de compresión: {len(json_body)} bytes"
                )
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
                "responseBody": {"application/json": {"body": json_body}},
            },
            "sessionAttributes": session_attributes,
            "promptSessionAttributes": prompt_session_attributes,
        }

    def get_user_context(self):

        session_id = self.event.session_id

        user_agent = self.event.agent

        user_context = EnhancedUserContext(session_id=session_id, user_agent=user_agent)

        return user_context

    def extract_query_fast(self):
        """Extrae la consulta SQL de la manera más rápida posible"""
        try:
            properties = (
                self.event.request_body.get("content", {})
                .get("application/json", {})
                .get("properties", [])
            )

            for prop in properties:
                if prop.get("name") in ["content", "query", "sql"]:
                    value = prop.get("value")

                    if isinstance(value, dict) and ("query" in value or "sql" in value):
                        return value.get("query", value.get("sql", ""))

                    if isinstance(value, str):
                        if value.startswith("{query="):
                            return value[7:].rstrip("}")
                        if value.startswith("{sql="):
                            return value[5:].rstrip("}")
                        if value.startswith("{") and value.endswith("}"):
                            try:
                                content_dict = json.loads(value)
                                if isinstance(content_dict, dict):
                                    return content_dict.get(
                                        "query", content_dict.get("sql", "")
                                    )
                            except:
                                pass
                        return value

                    return str(value)
        except Exception as e:
            self.logger.error(f"Error extrayendo consulta: {str(e)}")
            return None

    def handle_consulta(self):
        self.logger.info(
            f"[{self.event_id}] Procesando consulta con sessionId: {self.bedrock_session_id}"
        )

        # Extraer consulta
        query = self.extract_query_fast()

        if not query:
            self.logger.error(f"[{self.event_id}] No se encontró consulta SQL")
            return self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                data={"error": "No se encontró consulta SQL en la solicitud"},
                status_code=400,
                session_attributes=self.event.session_attributes,
                prompt_session_attributes=self.event.prompt_session_attributes,
            )

        # Ejecutar consulta
        sql_start = pytime.time()
        results = self.executor.execute(query, self.user_context)
        sql_time = pytime.time() - sql_start

        new_id = self.event.session_attributes.get("suggestion_id")

        if new_id != "":
            with SessionLocal() as session:
                stmt = (
                    update(SuggestedQuestions)
                    .where(SuggestedQuestions.id == new_id)
                    .values(sql_query=query)
                )

                session.execute(stmt)
                session.commit()

        self.logger.info(
            f"[{self.event_id}] Consulta ejecutada en {sql_time:.2f}s con session: {self.bedrock_session_id}"
        )

        if isinstance(results, dict) and "error" in results:
            return self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                data=results,
                status_code=400,
                session_attributes=self.event.session_attributes,
                prompt_session_attributes=self.event.prompt_session_attributes,
            )

        # Preparar respuesta exitosa
        total_time = pytime.time() - self.start_time

        response_data = {
            "results": (
                results.get("results", []) if isinstance(results, dict) else results
            ),
            "count": (
                len(results.get("results", []))
                if isinstance(results, dict) and "results" in results
                else 0
            ),
            "time_ms": int(total_time * 1000),
            "cache_hit": (
                results.get("cache_hit", False) if isinstance(results, dict) else False
            ),
            "session_info": {
                "bedrock_session_id": self.bedrock_session_id,
                "maintained_context": True,
                # "user_summary": self.user_context.get_session_summary()
            },
        }

        # CRÍTICO: Mantener sessionAttributes para el contexto
        enhanced_session_attributes = self.event.session_attributes.copy()
        enhanced_session_attributes.update(
            {
                "last_query_time": datetime.now().isoformat(),
                "query_count": int(enhanced_session_attributes.get("query_count", 0))
                + 1,
                "lambda_session_id": self.bedrock_session_id,
                "user_hash": self.user_context.get_user_hash(),
            }
        )

        return self.format_response_for_bedrock(
            action_group=self.event.action_group,
            api_path=self.event.api_path,
            http_method=self.event.http_method,
            data=response_data,
            session_attributes=enhanced_session_attributes,
            prompt_session_attributes=self.event.prompt_session_attributes,
        )

    def get_schema(self, table_names=None):
        """
        Obtiene dinámicamente el esquema y metadatos desde el catálogo.
        """
        schema_dict = {}
        try:
            with SessionLocal() as session:
                table_sql = select(
                    TableMetadata.table_name,
                    TableMetadata.esquema,
                    TableMetadata.descripcion_negocio,
                    TableMetadata.granularidad,
                    TableMetadata.dominio,
                    TableMetadata.default_time_column,
                ).filter(TableMetadata.usable_por_turi == True)
                if table_names:
                    table_sql = table_sql.filter(
                        TableMetadata.table_name.in_(table_names)
                    )
                    result = session.execute(table_sql)
                else:
                    result = session.execute(table_sql)

                tables = [dict(row._mapping) for row in result]
                found_table_names = [t["table_name"] for t in tables]

                if table_names:
                    for requested in table_names:
                        if requested not in found_table_names:
                            self.logger.warning(
                                f"La tabla '{requested}' no existe en table_metadata o usable_por_turi es false."
                            )

                if not tables:
                    return {}

                col_sql = select(
                    ColumnMetadata.table_name,
                    ColumnMetadata.esquema,
                    ColumnMetadata.column_name,
                    ColumnMetadata.tipo_dato,
                    ColumnMetadata.descripcion_negocio,
                    ColumnMetadata.sinonimos_usuario,
                    ColumnMetadata.valores_posibles,
                    ColumnMetadata.ejemplo_filtro,
                ).filter(
                    ColumnMetadata.usable_por_turi == True,
                    ColumnMetadata.table_name.in_(found_table_names),
                )
                cols_result = session.execute(col_sql)
                all_columns = [dict(row._mapping) for row in cols_result]

                domains = list({t["dominio"] for t in tables if t["dominio"]})
                glossary_by_domain = {}
                rules_by_domain = {}

                if domains:
                    gloss_sql = select(
                        Glossary.termino,
                        Glossary.dominio,
                        Glossary.significado,
                        Glossary.mapeo_tecnico,
                        Glossary.sinonimos,
                    ).filter(
                        Glossary.usable_por_turi == True, Glossary.dominio.in_(domains)
                    )

                    gloss_result = session.execute(gloss_sql)
                    for row in gloss_result:
                        d = row.dominio
                        if d not in glossary_by_domain:
                            glossary_by_domain[d] = {}
                        glossary_by_domain[d][row.termino] = {
                            "significado": row.significado,
                            "mapeo_tecnico": row.mapeo_tecnico,
                            "sinonimos": row.sinonimos,
                        }

                    rules_sql = select(
                        BusinessRules.nombre_regla,
                        BusinessRules.dominio,
                        BusinessRules.definicion,
                        BusinessRules.rule_sql,
                    ).filter(
                        BusinessRules.estado_metadata == "validado",
                        BusinessRules.dominio.in_(domains),
                    )

                    rules_result = session.execute(rules_sql)
                    for row in rules_result:
                        d = row.dominio
                        if d not in rules_by_domain:
                            rules_by_domain[d] = {}
                        rules_by_domain[d][row.nombre_regla] = {
                            "definicion": row.definicion,
                            "rule_sql": row.rule_sql,
                        }

                for t in tables:
                    full_key = f"{t['esquema']}.{t['table_name']}"
                    dom = t["dominio"]
                    schema_dict[full_key] = {
                        "descripcion_negocio": t["descripcion_negocio"],
                        "granularidad": t["granularidad"],
                        "dominio": dom,
                        "default_time_column": t["default_time_column"],
                        "columns": {
                            c["column_name"]: {
                                "tipo_dato": c["tipo_dato"],
                                "descripcion_negocio": c["descripcion_negocio"],
                                "sinonimos_usuario": c["sinonimos_usuario"],
                                "valores_posibles": c["valores_posibles"],
                                "ejemplo_filtro": c["ejemplo_filtro"],
                            }
                            for c in all_columns
                            if c["table_name"] == t["table_name"]
                            and c["esquema"] == t["esquema"]
                        },
                        "glossary": glossary_by_domain.get(dom, {}),
                        "business_rules": rules_by_domain.get(dom, {}),
                    }
            return schema_dict
        except Exception as e:
            self.logger.error(
                f"Error al obtener el esquema desde el catálogo: {str(e)}"
            )
            raise

    def handle_schema(self):
        self.logger.info(f"[{self.event_id}] Procesando solicitud de esquema")

        table_names = None
        try:
            properties = (
                self.event.request_body.get("content", {})
                .get("application/json", {})
                .get("properties", [])
            )
            for prop in properties:
                if prop.get("name") == "table_names":
                    val = prop.get("value")
                    if val:
                        if isinstance(val, str):
                            try:
                                table_names = json.loads(val.replace("'", '"'))
                            except:
                                table_names = [
                                    t.strip().strip("'\"[]")
                                    for t in val.split(",")
                                    if t.strip()
                                ]
                        elif isinstance(val, list):
                            table_names = val
        except Exception as e:
            self.logger.warning(
                f"Error parseando table_names, se procederá sin filtro: {str(e)}"
            )

        try:
            schema = self.get_schema(table_names)

            response_data = {"schema": schema}
            total_time = pytime.time() - self.start_time

            return self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                data=response_data,
                session_attributes=self.event.session_attributes,
                prompt_session_attributes=self.event.prompt_session_attributes,
            )
        except Exception as schema_error:

            self.logger.error(
                f"[{self.event_id}] Error obteniendo esquema: {str(schema_error)}"
            )
            return self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                data={"error": f"Error obteniendo esquema: {str(schema_error)}"},
                status_code=500,
                session_attributes=self.event.session_attributes,
                prompt_session_attributes=self.event.prompt_session_attributes,
            )

    def handle_stored_procedure(self):
        content = self.event.request_body["content"]["application/json"]["properties"]
        procedure = None
        params = []

        for prop in content:
            if prop["name"] == "procedure":
                procedure = prop["value"]
            elif prop["name"] == "params":
                params = json.loads(prop["value"])

        if procedure not in ALLOWED_PROCEDURES:
            return {"error": "Stored procedure no permitido"}

        placeholders = ",".join([f":p{i}" for i in range(len(params))])
        sql = text(f"CALL {procedure}({placeholders})")

        param_dict = {f"p{i}": params[i] for i in range(len(params))}

        self.logger.info(f"Ejecutando SP {procedure} con params {params}")

        with SessionLocal() as session:
            result = session.execute(sql, param_dict)

            try:
                rows = [dict(r) for r in result]
            except Exception:
                rows = []

            return {"results": rows}

    def handle_buscar_operadores(self):
        """Nueva herramienta para obtener información detallada de un cliente"""
        self.logger.info(f"[{self.event_id}] Ejecutando herramienta buscar_operadores")
    
        properties = self.event.parameters
        
        destino = None
        servicio = None
        marca = None
        categoria = None
        modalidad = None


        for prop in properties:
            if prop.get("name") == "destino":
                destino = prop.get("value")
            elif prop.get("name") == "servicio":
                servicio = prop.get("value")
            elif prop.get("name") == "marca":
                marca = prop.get("value")
            elif prop.get("name") == "categoria":
                categoria = prop.get("value")
            elif prop.get("name") == "modalidad":
                modalidad = prop.get("value")

        if not destino:
            return self.format_response_for_bedrock(
            action_group=self.event.action_group,
            api_path=self.event.api_path,
            http_method=self.event.http_method,
            data={"error": "Para poder responderte necesito que me indiques el destino"},
            session_attributes=self.event.session_attributes,
            prompt_session_attributes=self.event.prompt_session_attributes,
        )

        stmt = select(
            TipsOperadores.operador,
            TipsOperadores.prestador,
            TipsOperadores.tipo_prestador,
            TipsOperadores.region,
            TipsOperadores.pais,
            TipsOperadores.destino,
            TipsOperadores.servicios,
            TipsOperadores.tipo_servicio,
            TipsOperadores.categoria,
            TipsOperadores.moneda,
            TipsOperadores.especificaciones_comision,
            TipsOperadores.comentarios,
            TipsOperadores.prioridad,
        )

        if destino:
            stmt = stmt.where(
                or_(
                    func.f_unaccent(
                        func.array_to_string(TipsOperadores.region, ", ")
                    ).ilike(f"%{destino}%"),
                    func.f_unaccent(
                        func.array_to_string(TipsOperadores.pais, ", ")
                    ).ilike(f"%{destino}%"),
                    func.f_unaccent(
                        func.coalesce(TipsOperadores.destino, "")
                    ).ilike(f"%{destino}%"),
                )
            )

        if servicio:
            stmt = stmt.where(
                func.f_unaccent(
                    func.array_to_string(TipsOperadores.servicios, ", ")
                ).ilike(f"%{servicio}%")
            )

        if marca:
            stmt = stmt.where(
                func.f_unaccent(
                    TipsOperadores.prestador
                ).ilike(f"%{marca}%")
            )

        if categoria:
            stmt = stmt.where(
                func.f_unaccent(
                    func.array_to_string(TipsOperadores.categoria, ", ")
                ).ilike(f"%{categoria}%")
            )

        if modalidad:
            stmt = stmt.where(
                func.f_unaccent(
                    func.array_to_string(TipsOperadores.tipo_servicio, ", ")
                ).ilike(f"%{modalidad}%")
            )

        stmt = (
            stmt
            .order_by(TipsOperadores.prioridad.asc().nulls_last())
            .limit(50)
        )

        with SessionLocal() as session:
            results = session.execute(stmt).fetchall()
            results = [dict(r._mapping) for r in results]

        return self.format_response_for_bedrock(
            action_group=self.event.action_group,
            api_path=self.event.api_path,
            http_method=self.event.http_method,
            data=results,
            session_attributes=self.event.session_attributes,
            prompt_session_attributes=self.event.prompt_session_attributes,
        )

    def handle_discover_tables(self):
        config = botocore.config.Config(
            connect_timeout=30,
            read_timeout=120,
            retries={"max_attempts": 3, "mode": "standard"},
        )

        properties = (
            self.event.request_body.get("content", {})
            .get("application/json", {})
            .get("properties", [])
        )

        for prop in properties:
            if prop.get("name") == "query_intent":
                input_txt = prop.get("value")

                keywords_cache = [
                    kw.lower()
                    for kw in input_txt.split()
                    if kw.lower() not in STOP_WORDS
                ]

                val_embeded = cohere_embed(
                    input_txt, keywords_cache, config, "search_query"
                )

    def handle_event(self):
        """Handler corregido para Action Groups de Bedrock Agent con logging completo"""

        if not self.bedrock_session_id:
            self.logger.error(
                f"[{self.event_id}] CRÍTICO: No se pudo extraer sessionId del evento"
            )

            # Crear un sessionId de emergencia
            self.bedrock_session_id = f"emergency_{uuid.uuid4().hex[:8]}"
            self.logger.warning(
                f"[{self.event_id}] Usando sessionId de emergencia: {self.bedrock_session_id}"
            )

        self.logger.info(
            f"[{self.event_id}] SessionId extraído: {self.bedrock_session_id}"
        )

        # Actualizar información de sesión de Bedrock
        self.user_context.update_bedrock_session(
            {
                "session_id": self.bedrock_session_id,
                "session_attributes": self.event.session_attributes,
                "prompt_session_attributes": self.event.prompt_session_attributes,
            }
        )

        try:
            path = self.event.api_path
            handler = self.routes.get(path)
            if handler is None:
                self.logger.warning(
                    f"[{self.event_id}] Endpoint no reconocido: {self.event.api_path}"
                )

                response = self.format_response_for_bedrock(
                    action_group=self.event.action_group,
                    api_path=self.event.api_path,
                    http_method=self.event.http_method,
                    data={"error": "Endpoint no reconocido"},
                    status_code=404,
                    session_attributes=self.event.session_attributes,
                    prompt_session_attributes=self.event.prompt_session_attributes,
                )

            response = handler()

        except Exception as e:
            total_time = pytime.time() - self.start_time
            stack_trace = traceback.format_exc()

            self.logger.error(
                f"[{self.event_id}] Error con session {self.bedrock_session_id}: {str(e)}"
            )

            response = self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                data={"error": str(e)},
                status_code=500,
                session_attributes=self.event.session_attributes,
                prompt_session_attributes=self.event.prompt_session_attributes,
            )

        execution_time = (pytime.time() - self.start_time) * 1000

        self.logger.info(
            f"[{self.req_id}][{self.user_context.get_user_hash()}] Completado: {execution_time:.2f}ms"
        )
        return response
