import os
import boto3
import json
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

def get_secret() -> dict:
    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId="airbytedestination_airbyte2_postgrs")
    return json.loads(resp["SecretString"])

def get_engine():
    s = get_secret()
    conn_str = (
        f"postgresql+psycopg2://{s['username']}:{s['password']}@"
        f"{s['host']}:{s['port']}/{s['dbInstanceIdentifier']}"
    )
    return create_engine(conn_str, pool_pre_ping=True, pool_size=10, connect_args={"options": "-csearch_path=facturas_bot"})

engine = get_engine()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()