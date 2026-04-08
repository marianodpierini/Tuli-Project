import json
import boto3
import base64
import os
import io
import zipfile

from datetime import datetime

s3 = boto3.client("s3")

BUCKET = os.environ.get("BUCKET", "aero-turi-documents")


def get_facturas():
    now = datetime.now()

    prefix = (
        f"facturas/"
        f"Año={now.year}/"
        f"Mes={now.month:02d}/"
        f"Dia={now.day:02d}/"
    )

    print(f"Buscando facturas en: {prefix}")

    facturas = []

    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(
        Bucket=BUCKET,
        Prefix=prefix
    ):
        contents = page.get("Contents", [])

        for obj in contents:
            key = obj["Key"]

            if key.endswith("/"):
                continue

            filename = key.split("/")[-1]

            facturas.append({
                "id": key,
                "s3_key": key,
                "filename": filename
            })

    print(f"Facturas encontradas: {len(facturas)}")

    return facturas


def ensure_unique_filename(zip_file, filename):
    existing = set(zip_file.namelist())

    if filename not in existing:
        return filename

    base, ext = split_filename(filename)
    counter = 1

    while True:
        new_name = f"{base}_{counter}{ext}"
        if new_name not in existing:
            return new_name
        counter += 1


def split_filename(filename):
    if "." in filename:
        parts = filename.rsplit(".", 1)
        return parts[0], "." + parts[1]
    return filename, ""


def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps(body)
    }


def lambda_handler(event, context):
    print("EVENT:", json.dumps(event))

    try:

        facturas = get_facturas()

        if not facturas:
            return response(204, "")

        buffer = io.BytesIO()

        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            for factura in facturas:
                key = factura["s3_key"]
                filename = key.split("/")[-1]

                print(f"Agregando al ZIP: {key}")

                obj = s3.get_object(Bucket=BUCKET, Key=key)
                file_bytes = obj["Body"].read()

                filename = ensure_unique_filename(z, filename)

                z.writestr(filename, file_bytes)

        zip_bytes = buffer.getvalue()

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/zip",
                "Content-Disposition": "attachment; filename=facturas.zip"
            },
            "body": base64.b64encode(zip_bytes).decode("utf-8"),
            "isBase64Encoded": True
        }

    except Exception as e:
        print("ERROR:", str(e))
        return response(500, {"error": str(e)})