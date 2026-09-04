import re
from datetime import date, timedelta
from sqlalchemy import or_
from database.db import SessionLocal
from database.models import (
    PMysqlPagoproveedoresproductionInvoices,
    PMysqlProductionmotoursReserves,
    SMysqlProductionmotoursServices,
)
from core.parser_helpers.parser_functions import PARSERS_DICT

class InvoicesValidation:
    """Validates and enriches extracted invoice data by linking services and checking for existing invoices."""
    def __init__(self, data_agent, operadores, conn_mysql, logger):
        self.data_agent = data_agent
        self.operadores = operadores
        self.operator_ids = [op["id"] for op in operadores]
        self.conn_mysql = conn_mysql
        self.logger = logger

    def normalizar_codigo(self, codigo: str) -> str:
        if not codigo:
            self.logger.info("Código vacío, retornando sin normalizar.")
            return codigo

        transformations = self.operadores[0].get("codigo_config", {}).get("transformations", [])

        for t in transformations:
            t_type = t.get("type")

            parser_fn = PARSERS_DICT.get(t_type)

            if not parser_fn:
                self.logger.warning(f"Parser no soportado: {t_type}")
                continue

            codigo = parser_fn(codigo, t)

        return codigo

    def buscar_servicios(self, codigos):
            """Searches for services in the database based on confirmation codes and operator IDs."""
            try:
                self.logger.info(f"Buscando servicios para códigos: {codigos} y operadores: {self.operator_ids}")
                codigos_normalizados = [
                    self.normalizar_codigo(codigo)
                    for codigo in codigos
                    if codigo
                ]
    
                if not self.operator_ids or not codigos_normalizados:
                    return {}
    
                fecha_hoy = date.today()
                fecha_min = fecha_hoy - timedelta(days=183)
                fecha_max = fecha_hoy + timedelta(days=335)
    
                condiciones_codigo = [
                    SMysqlProductionmotoursServices.confirmation_code.ilike(f"%{codigo}%")
                    for codigo in codigos_normalizados
                ]
    
                with SessionLocal() as session:
                    rows = (
                        session.query(
                            SMysqlProductionmotoursServices.id.label("id"),
                            SMysqlProductionmotoursServices.confirmation_code.label("confirmation_code"),
                            SMysqlProductionmotoursServices.reserve_id.label("reserve_id"),
                            SMysqlProductionmotoursServices.aptour_reserve_id.label("aptour_reserve_id"),
                            SMysqlProductionmotoursServices.date_in.label("date_in"),
                            SMysqlProductionmotoursServices.balance.label("balance"),
                            SMysqlProductionmotoursServices.operator_id.label("operator_id"),
                            SMysqlProductionmotoursServices.operator_name.label("operator_name"),
                        )
                        .outerjoin(
                            PMysqlProductionmotoursReserves,
                            PMysqlProductionmotoursReserves.id == SMysqlProductionmotoursServices.reserve_id,
                        )
                        .filter(SMysqlProductionmotoursServices.operator_id.in_(self.operator_ids))
                        .filter(
                            or_(
                                SMysqlProductionmotoursServices.balance > 0,
                                SMysqlProductionmotoursServices.balance.is_(None),
                            )
                        )
                        .filter(SMysqlProductionmotoursServices.date_in >= fecha_min)
                        .filter(SMysqlProductionmotoursServices.date_in <= fecha_max)
                        .filter(or_(*condiciones_codigo))
                        .all()
                    )
    
                indice = {}
                rows_log = []
    
                for row in rows:
                    row_dict = {
                        "id": row.id,
                        "confirmation_code": row.confirmation_code,
                        "reserve_id": row.reserve_id,
                        "aptour_reserve_id": row.aptour_reserve_id,
                        "date_in": row.date_in,
                        "balance": row.balance,
                        "operator_id": row.operator_id,
                        "operator_name": row.operator_name,
                    }
                    rows_log.append(row_dict)
    
                    codigos_en_campo = re.split(
                        r"[\s,;\/]+", row_dict["confirmation_code"] or ""
                    )
    
                    for cod in codigos_en_campo:
                        cod = cod.strip()
                        if not cod:
                            continue
    
                        if cod not in indice:
                            indice[cod] = row_dict
    
                self.logger.info(f"Servicios encontrados: {rows_log}")
                return indice
    
            except Exception as e:
                self.logger.error(f"Error al buscar servicios: {e}")
                return {}

    def verificar_facturas(self, reserve_ids):
            self.logger.info(f"Verificando facturas para reservas: {reserve_ids}")
            try:
                reserve_ids_validos = [rid for rid in reserve_ids if rid is not None]
    
                if not reserve_ids_validos:
                    return {}
    
                with SessionLocal() as session:
                    rows = (
                        session.query(
                            PMysqlPagoproveedoresproductionInvoices.reserve_id.label("reserve_id"),
                            PMysqlPagoproveedoresproductionInvoices.branch.label("branch"),
                            PMysqlPagoproveedoresproductionInvoices.number.label("number"),
                        )
                        .filter(
                            PMysqlPagoproveedoresproductionInvoices.reserve_id.in_(reserve_ids_validos)
                        )
                        .all()
                    )
    
                result = {}
    
                for row in rows:
                    if row.reserve_id is None:
                        continue
    
                    branch = row.branch or ""
                    number = row.number or ""
                    result[row.reserve_id] = f"{branch}-{number}" if branch or number else ""
    
                self.logger.info(f"Facturas encontradas para reservas: {result}")
                return result
    
            except Exception as e:
                self.logger.error(f"Error al verificar facturas: {e}")
                return {}

    def vincular_servicios(self):
        servicios = self.data_agent.get("servicios", [])

        codigos = list({s.get("voucher") for s in servicios if s.get("voucher")})

        if not codigos:
            return self.data_agent
        resultados = self.buscar_servicios(codigos)

        if not resultados:
            return self.data_agent


        reserve_ids = list(
                r.get("aptour_reserve_id") or r.get("reserve_id")
                for r in resultados.values()
                if r
        )

        facturas = self.verificar_facturas(reserve_ids) if reserve_ids else {}

        servicios_enriquecidos = []

        for s in servicios:
            original_voucher = s.get("voucher", "")
            codigo = self.normalizar_codigo(original_voucher)

            regex = re.compile(
                r'(?:(?<!\d)|(?<=540)|(?<=540[\s\-\/\.\_\:]))'
                + re.escape(original_voucher)
                + r'(?!\d)'
            )

            for key in resultados.keys():
                if regex.search(key):
                    codigo = key
                    break

            s["vinculado"] = False
            encontrado = resultados.get(codigo)
            if not encontrado:
                s["vinculado"] = False
                continue

            rid = encontrado.get("aptour_reserve_id")
            id_reserva_mo = encontrado.get("reserve_id")

            s["vinculado"] = True
            s["service_id"] = encontrado["id"]
            s["reserve_id"] = rid
            s["importeUSD"] = encontrado["balance"]
            s["id_reserva_mo"] = id_reserva_mo
            s["operator_id"] = encontrado["operator_id"]
            s["voucher"] = codigo

            if rid in facturas:
                s["ya_facturado"] = True
                s["factura"] = facturas[rid]
            else:
                s["pending"] = True

            servicios_enriquecidos.append(s)

        
        self.data_agent["servicios"] = servicios

        self.logger.info(f"Servicios enriquecidos: {servicios_enriquecidos}")
        return self.data_agent
