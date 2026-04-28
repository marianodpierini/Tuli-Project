import pymysql
import os
import json
import boto3


def get_secret() -> dict:
    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId="prod/middleOffice/mysql")
    return json.loads(resp["SecretString"])


def get_connection():
    secret = get_secret()
    return pymysql.connect(
        host=secret["host"],
        user=secret["username"],
        password=secret["password"],
        cursorclass=pymysql.cursors.DictCursor,
    )
