import json
import traceback
import uuid
import time as pytime

from datetime import datetime
from functools import lru_cache

from sqlalchemy.inspection import inspect
from sqlalchemy import inspect, extract, select, func, update, text

from core.improved_context_classes import EnhancedUserContext, CustomJSONEncoder
from core.database.db import SessionLocal, engine
from core.database.models import ServiciosTcktsRvas, SuggestedQuestions

from core.request_handler import RequestHandler


ALLOWED_PROCEDURES = ["objetivos_departamentales"]

class BedrockRequestHandler(RequestHandler):
    def __init__(self, logger, req_id, event, lambda_handler):
        super().__init__(logger, req_id, event, lambda_handler)
        self.event_id = str(uuid.uuid4())[:8]
        self.start_time = pytime.time()
        self.bedrock_session_id = self.event.session_id
        self.routes = {
            "/consulta": self.handle_consulta,
            "/schema": self.handle_schema,
            "/system_metrics": self.handle_system_metrics,
            "/diagnostico": self.handle_diagnostico,
            "/diagnostics": self.handle_diagnostics,
            "/stored_procedure": self.handle_stored_procedure,
        }

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
        
        user_context = EnhancedUserContext(session_id=session_id, user_agent=user_agent)
        
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
            servicios_tckts_rvas_mapper = inspect(ServiciosTcktsRvas)
            suggested_questions_mapper = inspect(SuggestedQuestions)

            schema = {
                "servicios_tckts_rvas_schema": {
                    "table_name": ServiciosTcktsRvas.__tablename__,
                    "columns": {}
                },
                "suggested_questions_schema": {
                    "table_name": SuggestedQuestions.__tablename__,
                    "columns": {}
                },
            }

            for column in servicios_tckts_rvas_mapper.columns:
                schema["servicios_tckts_rvas_schema"]["columns"][column.name] = {
                    "type": str(column.type),
                    "nullable": column.nullable,
                    "default": str(column.default.arg) if column.default is not None else None,
                }

            for column in suggested_questions_mapper.columns:
                schema["suggested_questions_schema"]["columns"][column.name] = {
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
            
        try:
            schema = self.get_schema()

            self.logger.info(f"[{self.event_id}] Esquema obtenido: {json.dumps(schema)}")
                
            response_data = {"schema": schema}
            total_time = pytime.time() - self.start_time

                
            return self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                data=response_data,
                session_attributes=self.event.session_attributes,
                prompt_session_attributes=self.event.prompt_session_attributes
            )
        except Exception as schema_error:
                
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
            
        diag_start_time = pytime.time()
        diagnostics = self.quick_database_diagnostics(full_check=True)
        diag_time = pytime.time() - diag_start_time
        self.logger.info(f"[{self.event_id}] Diagnóstico completado en {diag_time:.2f}s")
            
        response_data = {"diagnostico": diagnostics}
            
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
                
            # Verificar conexión directa
            conn_test = self.direct_diagnostic()
                
            response_data = {"connection_test": conn_test}
                
            return self.format_response_for_bedrock(
                action_group=self.event.action_group,
                api_path=self.event.api_path,
                http_method=self.event.http_method,
                data=response_data,
                session_attributes=self.event.session_attributes,
                prompt_session_attributes=self.event.prompt_session_attributes
            )
        except Exception as diag_error:
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


        try:
            path = self.event.api_path
            handler = self.routes.get(path)
            if handler is None:
                self.logger.warning(f"[{self.event_id}] Endpoint no reconocido: {self.event.api_path}")
            
                
                response = self.format_response_for_bedrock(
                    action_group=self.event.action_group,
                    api_path=self.event.api_path,
                    http_method=self.event.http_method,
                    data={"error": "Endpoint no reconocido"},
                    status_code=404,
                    session_attributes=self.event.session_attributes,
                    prompt_session_attributes=self.event.prompt_session_attributes
                )

            response = handler()
        
        except Exception as e:
            total_time = pytime.time() - self.start_time
            stack_trace = traceback.format_exc()
            
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
        
        self.logger.info(f"[{self.req_id}][{self.user_context.get_user_hash()}] Completado: {execution_time:.2f}ms")
        return response