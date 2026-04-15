from datetime import datetime
from io import BytesIO
from PyPDF2 import PdfReader

from database.models import InvoicesExtractedEmails


class EmailProcessor:
    def __init__(self, msg, operadores, s3_bucket_destino, s3_client, db_session):
        self.msg = msg
        self.operadores = operadores
        self.s3_bucket_destino = s3_bucket_destino
        self.s3_client = s3_client
        self.db_session = db_session

    
    def generate_s3_key(self, filename, now):
        return (
            f"facturas/"
            f"Año={now.year}/"
            f"Mes={now.month:02d}/"
            f"Dia={now.day:02d}/"
            f"{filename}"
        )


    def is_valid_invoice(self, content_type, filename):
        allowed_types = [
            "application/pdf",
            "text/xml",
            "application/xml"
        ]

        allowed_extensions = (".pdf", ".xml")

        return (
            content_type in allowed_types or
            filename.lower().endswith(allowed_extensions)
        )

    def buscar_operador_por_cuit(self, cuit):
        for cuit_ops, operadores in self.operadores.items():
            cuit_limpio = cuit_ops.replace("-", "")
            if cuit_limpio == cuit:
                return operadores

        return None

    def extraer_cuit_de_pdf(self, file_bytes):
        reader = PdfReader(BytesIO(file_bytes))

        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""

        import re
        match = re.search(r"\b\d{2}-?\d{8}-?\d{1}\b", text)

        if match:
            return match.group(0)

        return None

    def process_email(self):
        attachments_saved = []
        data_to_insert = []
        for part in self.msg.iter_attachments():
            filename = part.get_filename()

            if not filename:
                continue

            content_type = part.get_content_type()

            print(f"Encontrado adjunto: {filename} ({content_type})")

            if not self.is_valid_invoice(content_type, filename):
                print(f"Archivo ignorado: {filename}")
                continue

            file_bytes = part.get_payload(decode=True)

            cuit = None

            cuit = self.extraer_cuit_de_pdf(file_bytes)

            if not cuit:
                print("No se pudo extraer CUIT, se ignora archivo")
                continue

            operadores = self.buscar_operador_por_cuit(cuit) 

            if not operadores:
                print(f"CUIT {cuit} no encontrado")
                continue

            now = datetime.now()
            dest_key = self.generate_s3_key(filename, now)

            self.s3_client.put_object(
                Bucket=self.s3_bucket_destino,
                Key=dest_key,
                Body=file_bytes,
                ContentType=content_type
            )

            print(f"Guardado en: {self.s3_bucket_destino}/{dest_key}")

            operadores_ids = [op["id"] for op in operadores]

            invoice = InvoicesExtractedEmails(
                cuit=cuit,
                ids_operadores=operadores_ids,
                s3_key=dest_key
            )

            data_to_insert.append(invoice)


            attachments_saved.append({
                "filename": filename,
                "s3_key": dest_key
            })
        
        with self.db_session() as session:
            session.add_all(data_to_insert)
            session.commit()

        return attachments_saved