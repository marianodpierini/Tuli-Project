from database.db_mysql import get_connection


class InvoicesValidation:
    def __init__(self, data_agent, operator_ids):
        self.conn_mysql = get_connection()
        self.data_agent = data_agent
        self.operator_ids = operator_ids

    def normalizar_codigo(self, codigo: str) -> str:
        if codigo.startswith("540"):
            return codigo[3:]
        return codigo

    def buscar_servicios(self, codigos):
        try:
            with self.conn_mysql.cursor() as cursor:
                placeholders_op = ",".join(["%s"] * len(self.operator_ids))
                placeholders_cod = " OR ".join(
                    ["s.confirmation_code LIKE %s"] * len(codigos)
                )

                query = f"""
                    SELECT s.id, s.confirmation_code, s.reserve_id, s.aptour_reserve_id,
                        s.date_in, s.balance, s.operator_id, s.operator_name
                    FROM production_mo_tours.services s
                    WHERE s.operator_id IN ({placeholders_op})
                    AND s.balance > 0
                    AND s.date_in >= DATE_SUB(NOW(), INTERVAL 6 MONTH)
                    AND s.date_in <= DATE_ADD(NOW(), INTERVAL 6 MONTH)
                    AND ({placeholders_cod})
                    LIMIT 50
                """

                params = self.operator_ids + [f"%{c}%" for c in codigos]

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

                return indice

        finally:
            self.conn_mysql.close()

    def verificar_facturas(self, reserve_ids):

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

                return result

        finally:
            self.conn_mysql.close()

    def vincular_servicios(self):
        servicios = self.data_agent.get("servicios", [])

        codigos = list({self.normalizar_codigo(s.get("voucher")) for s in servicios if s.get("voucher")})

        if not codigos:
            return self.data_agent

        resultados = self.buscar_servicios(codigos)

        reserve_ids = list(
            {
                r.get("reserve_id") or r.get("aptour_reserve_id")
                for r in resultados.values()
                if r
            }
        )

        facturas = self.verificar_facturas(reserve_ids) if reserve_ids else {}

        servicios_enriquecidos = []

        for s in servicios:
            codigo = self.normalizar_codigo(s.get("voucher", ""))

            encontrado = resultados.get(codigo)

            if not encontrado:
                s["vinculado"] = False
                continue

            rid = encontrado.get("aptour_reserve_id") or encontrado.get("reserve_id")

            s["vinculado"] = True
            s["service_id"] = encontrado["id"]
            s["reserve_id"] = rid
            s["importeUSD"] = encontrado["balance"]

            if rid in facturas:
                s["ya_facturado"] = True
                s["factura"] = facturas[rid]
            else:
                s["pendiente"] = True

            servicios_enriquecidos.append(s)

        if servicios_enriquecidos > 0:
            self.data_agent["servicios"] = servicios_enriquecidos

        return self.data_agent
