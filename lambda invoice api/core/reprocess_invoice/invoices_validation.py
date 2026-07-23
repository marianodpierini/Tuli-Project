import re
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
            with self.conn_mysql.cursor() as cursor:
                placeholders_op = ",".join(["%s"] * len(self.operator_ids))

                condiciones_codigo = " OR ".join(
                    ["s.confirmation_code LIKE %s"] * len(codigos)
                )

                query = f"""
                    SELECT s.id, s.confirmation_code, s.reserve_id, s.aptour_reserve_id,
                        s.date_in, s.balance, s.operator_id, s.operator_name
                    FROM production_mo_tours.services s
                    LEFT JOIN production_mo_tours.reserves r ON r.id = s.reserve_id
                    WHERE s.operator_id IN ({placeholders_op})
                    AND (s.balance > 0 OR s.balance IS NULL)
                    AND s.date_in >= DATE_SUB(NOW(), INTERVAL 6 MONTH)
                    AND s.date_in <= DATE_ADD(NOW(), INTERVAL 11 MONTH)
                    AND ({condiciones_codigo})
                    LIMIT 50
                """

                params = self.operator_ids + [f"%{self.normalizar_codigo(codigo)}%" for codigo in codigos]

                cursor.execute(query, params)

                rows = cursor.fetchall()

                indice = {}

                for row in rows:
                    codigos_en_campo = re.split(
                        r"[\s,;\/]+", row["confirmation_code"] or ""
                    )

                    for cod in codigos_en_campo:
                        cod = cod.strip()
                        if not cod:
                            continue

                        if cod not in indice:
                            indice[cod] = row
                            
                self.logger.info(f"Servicios encontrados: {rows}")
                return indice

        except Exception as e:
            self.logger.error(f"Error al buscar servicios: {e}")
            return {}

    def verificar_facturas(self, reserve_ids):
        self.logger.info(f"Verificando facturas para reservas: {reserve_ids}")
        try:
            with self.conn_mysql.cursor() as cursor:
                placeholders = ",".join(["%s"] * len(reserve_ids))

                query = f"""
                    SELECT reserve_id, CONCAT(branch, "-", number) as factura
                    FROM pago_proveedores_production.invoices
                    WHERE reserve_id IN ({placeholders})
                """

                cursor.execute(query, reserve_ids)

                result = {}

                for row in cursor.fetchall():
                    result[row["reserve_id"]] = row["factura"]

                
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
