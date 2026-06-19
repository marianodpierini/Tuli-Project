import json
import re
import traceback
import uuid
import time as pytime
import botocore

from sqlalchemy import select, func, text, or_

from core.improved_context_classes import EnhancedUserContext, CustomJSONEncoder
from core.database.db import SessionLocal
from core.database.models import (
    ServiciosTcktsRvas,
    SuggestedQuestions,
    TableMetadata,
    ColumnMetadata,
    Glossary,
    BusinessRules,
    TipsOperadores,
    HotelesRecomendados,
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
            "/stored_procedure": self.handle_stored_procedure,
            "/discover_tables": self.handle_discover_tables,
            "/search_providers": self.handle_buscar_operadores,
            "/search_lodging": self.handle_buscar_hoteles,
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
        
    def _get_parameters(self, dict_parameters: dict):
        properties = self.event.parameters
        
        for prop in properties:
            name = prop.get("name")
            if name in dict_parameters.keys():
                dict_parameters[name] = prop.get("value")
        return dict_parameters

    def handle_buscar_operadores(self):
        """Nueva herramienta para obtener información detallada de operadores"""
        self.logger.info(f"[{self.event_id}] Ejecutando herramienta buscar_operadores")

        dict_parameters = {
            "destino": None,
            "servicio": None,
            "marca": None,
            "categoria": None,
            "modalidad": None,
            "tipo_prestador": None,
        }


        dict_parameters = self._get_parameters(dict_parameters)

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

        if dict_parameters["destino"] is not None:
            destino = dict_parameters["destino"].lower()
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

        if dict_parameters["servicio"] is not None:
            servicio = dict_parameters["servicio"].lower()
            stmt = stmt.where(
                func.f_unaccent(
                    func.array_to_string(TipsOperadores.servicios, ", ")
                ).ilike(f"%{servicio}%")
            )

        if dict_parameters["marca"] is not None:
            marca = dict_parameters["marca"].lower()
            stmt = stmt.where(
                func.f_unaccent(
                    TipsOperadores.prestador
                ).ilike(f"%{marca}%")
            )

        if dict_parameters["categoria"] is not None:
            categoria = dict_parameters["categoria"].lower()
            stmt = stmt.where(
                func.f_unaccent(
                    func.array_to_string(TipsOperadores.categoria, ", ")
                ).ilike(f"%{categoria}%")
            )

        if dict_parameters["modalidad"] is not None:
            modalidad = dict_parameters["modalidad"].lower()
            stmt = stmt.where(
                func.f_unaccent(
                    func.array_to_string(TipsOperadores.tipo_servicio, ", ")
                ).ilike(f"%{modalidad}%")
            )

        if dict_parameters["tipo_prestador"] is not None:
            tipo_prestador = dict_parameters["tipo_prestador"].lower()
            stmt = stmt.where(
                func.f_unaccent(
                    func.array_to_string(TipsOperadores.tipo_prestador, ", ")
                ).ilike(f"%{tipo_prestador}%")
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
    
    def handle_buscar_hoteles(self):
        """Nueva herramienta para obtener información detallada de hoteles"""
        self.logger.info(f"[{self.event_id}] Ejecutando herramienta buscar_hoteles")

        dict_parameters = {
            "destino": None,
            "zona": None,
            "hotel": None,
            "estrellas": None,
        }

        dict_parameters = self._get_parameters(dict_parameters)

        stmt = select(
            HotelesRecomendados.region,
            HotelesRecomendados.pais,
            HotelesRecomendados.ciudad,
            HotelesRecomendados.zona,
            HotelesRecomendados.hotel,
            HotelesRecomendados.categoria_estrellas,
            HotelesRecomendados.nivel_recomendacion,
            HotelesRecomendados.observaciones,
        ).where(
            HotelesRecomendados.activo.is_(True)
        )

        if dict_parameters["destino"] is not None:
            destino = dict_parameters["destino"].lower()
            stmt = stmt.where(
                or_(
                    func.f_unaccent(
                        HotelesRecomendados.region
                    ).ilike(
                        func.concat("%", func.f_unaccent(destino), "%")
                    ),
                    func.f_unaccent(
                        HotelesRecomendados.pais
                    ).ilike(
                        func.concat("%", func.f_unaccent(destino), "%")
                    ),
                    func.f_unaccent(
                        HotelesRecomendados.ciudad
                    ).ilike(
                        func.concat("%", func.f_unaccent(destino), "%")
                    ),
                )
            )

        if dict_parameters["zona"] is not None:
            zona = dict_parameters["zona"].lower()
            stmt = stmt.where(
                func.f_unaccent(
                    HotelesRecomendados.zona
                ).ilike(
                    func.concat("%", func.f_unaccent(zona), "%")
                )
            )

        if dict_parameters["hotel"] is not None:
            hotel = dict_parameters["hotel"].lower()
            stmt = stmt.where(
                func.f_unaccent(
                    HotelesRecomendados.hotel
                ).ilike(
                    func.concat("%", func.f_unaccent(hotel), "%")
                )
            )

        if dict_parameters["estrellas"] is not None:
            estrellas = dict_parameters["estrellas"]
            m = re.search(r'\d', estrellas)
            estrellas = int(m.group()) if m else None
            
            stmt = stmt.where(
                HotelesRecomendados.categoria_estrellas >= estrellas
            )

        stmt = (
            stmt
            .order_by(
                func.coalesce(
                    HotelesRecomendados.nivel_recomendacion,
                    0
                ).desc(),
                func.coalesce(
                    HotelesRecomendados.categoria_estrellas,
                    0
                ).desc(),
                HotelesRecomendados.ciudad,
                HotelesRecomendados.hotel,
            )
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
