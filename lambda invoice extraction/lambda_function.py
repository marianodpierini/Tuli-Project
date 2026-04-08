import json
import boto3
import email
import os

from email import policy
from email.parser import BytesParser
from datetime import datetime
from io import BytesIO
from PyPDF2 import PdfReader

from email_processor import EmailProcessor

s3 = boto3.client("s3")

OPERADORES_BUCKET = os.environ.get("OPERADORES_BUCKET", "aero-turi-documents")
OPERADORES_KEY = os.environ.get("OPERADORES_KEY", "lambda-files/operadores.json")

DEST_BUCKET = os.environ.get("DEST_BUCKET", "aero-turi-documents")


def cargar_operadores():
    response = s3.get_object(
        Bucket=OPERADORES_BUCKET,
        Key=OPERADORES_KEY
    )
    data = json.loads(response["Body"].read())
    return data


def lambda_handler(event, context):
    print("EVENT:", json.dumps(event))

    try:
        record = event['Records'][0]
        ses = record['ses']
        mail = ses['mail']

        message_id = mail['messageId']

        s3_info = record['s3']
        bucket = s3_info['bucket']['name']
        key = s3_info['object']['key']

        print(f"Procesando mail: {message_id}")
        print(f"S3 source: {bucket}/{key}")

        response = s3.get_object(Bucket=bucket, Key=key)
        raw_email = response['Body'].read()

        msg = BytesParser(policy=policy.default).parsebytes(raw_email)

        subject = msg.get('subject', '')
        from_email = msg.get('from', '')
        date = msg.get('date', '')

        print(f"Subject: {subject}")
        print(f"From: {from_email}")

        operadores = cargar_operadores()

        email_processor = EmailProcessor(msg, operadores, DEST_BUCKET, s3)

        attachments_saved = email_processor.process_email()

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Procesado correctamente",
                "attachments": attachments_saved
            })
        }

    except Exception as e:
        print("ERROR:", str(e))
        raise e