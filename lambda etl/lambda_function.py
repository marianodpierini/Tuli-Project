from datetime import date, timedelta
import os
import json
import logging
import sys
import hashlib
from typing import Dict, Iterator, Tuple
from dataclasses import dataclass
from io import StringIO

import boto3
import pandas as pd
import psycopg2
from sqlalchemy import create_engine, text, Table, Column, MetaData
from sqlalchemy import Integer, Float, Text, Boolean, Date, DateTime, BigInteger
import pyarrow.dataset as ds
import pyarrow.parquet as pq

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    force=True,
    format='%(asctime)s %(levelname)s %(name)s %(message)s'
)
logger = logging.getLogger(__name__)

s3 = boto3.client("s3")


def get_secret() -> dict:
    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=os.environ["DB_SECRET_ARN"])
    return json.loads(resp["SecretString"])


def get_engine():
    s = get_secret()
    conn_str = (
        f"postgresql+psycopg2://{s['username']}:{s['password']}@"
        f"{s['host']}:{s['port']}/{s['dbInstanceIdentifier']}"
    )
    return create_engine(conn_str, pool_pre_ping=True, pool_size=10, connect_args={"options": "-csearch_path=aptour"})


@dataclass
class Config:
    bucket: str
    base_prefix: str
    table: str
    pk: str = "id"
    apply: bool = False
    parquet_batch_size: int = 128000
    pg_batch_size: int = 50000
    exclude_from_hash: Tuple[str, ...] = tuple()
    staging_table: str = "staging_tmp"

def list_parquet_keys(cfg: Config):
    yesterday = date.today() - timedelta(days=1)
    prefix = f"{cfg.base_prefix}/dt={yesterday.isoformat()}/"
    logger.info(f"Buscando archivos parquet en s3://{cfg.bucket}/{prefix}")

    keys = []
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=cfg.bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(".parquet"):
                keys.append(obj["Key"])

    logger.info(f"Encontrados {len(keys)} parquet(s)")
    return keys


def ensure_table_exists(table_name: str, parquet_path: str, pk: str, engine):
    logger.info(f"Inicia proceso de validacion de existencia de tabla")
    meta = MetaData()
    meta.reflect(bind=engine)

    if table_name in meta.tables:
        return

    dataset = ds.dataset(parquet_path, format="parquet")
    schema = dataset.schema

    cols = []
    for field in schema:
        t = str(field.type)

        if t == "int64":
            col_type = BigInteger
        elif t == "int32":
            col_type = Integer
        elif t in ("double", "float64"):
            col_type = Float
        elif t in ("string", "large_string"):
            col_type = Text
        elif t == "bool":
            col_type = Boolean
        elif t in ("date32", "date64"):
            col_type = Date
        elif t.startswith("timestamp"):
            col_type = DateTime
        elif t.startswith("struct") and any("timestamp" in str(child.type) for child in field.type):
            col_type = DateTime
        else:
            col_type = Text

        if field.name == pk:
            cols.append(Column(field.name, col_type, primary_key=True))
        else:
            cols.append(Column(field.name, col_type))

    table = Table(table_name, meta, *cols)
    table.create(engine)


def load_parquet_to_staging(cfg: Config, engine, keys):
    PG_TYPE_CASTS = {
        "integer": "int",
        "bigint": "int",
        "smallint": "int",
        "double precision": "float",
        "real": "float",
        "numeric": "float",
        "decimal": "float",
        "text": "string",
        "character varying": "string",
        "boolean": "bool",
        "date": "date",
        "timestamp without time zone": "datetime",
        "timestamp with time zone": "datetime",
    }

    def unpack_ts(x):
        """Desempaqueta timestamps que vienen como dict {'member0': datetime, 'member1': None}"""
        if pd.isnull(x):
            return None
        if isinstance(x, dict):
            for v in x.values():
                if v is not None:
                    return v
            return None
        return x

    pg_cols = get_table_columns(cfg.table, engine)

    raw_conn = engine.raw_connection()
    cur = raw_conn.cursor()

    cur.execute(f"DROP TABLE IF EXISTS {cfg.staging_table}")
    cur.execute(f"CREATE TEMP TABLE {cfg.staging_table} (LIKE {cfg.table} INCLUDING ALL)")

    total_rows = 0

    for key in keys:
        tmp_path = f"/tmp/{os.path.basename(key)}"
        logger.info(f"Descargando {key} → {tmp_path}")
        s3.download_file(cfg.bucket, key, tmp_path)

        pq_file = pq.ParquetFile(tmp_path)

        for batch in pq_file.iter_batches(batch_size=cfg.parquet_batch_size):
            df = batch.to_pandas()

            for col in df.columns:
                dtype = pg_cols.get(col)
                if not dtype:
                    continue 

                target = PG_TYPE_CASTS.get(dtype, "string")

                if target == "int":
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("Int64")

                elif target == "float":
                    df[col] = pd.to_numeric(df[col], errors="coerce")

                elif target == "datetime":
                    df[col] = pd.to_datetime(df[col].map(unpack_ts), errors="coerce")

                elif target == "date":
                    df[col] = pd.to_datetime(df[col], errors="coerce").dt.date

                elif target == "bool":
                    df[col] = df[col].astype(bool)

                else:  # fallback a string
                    df[col] = df[col].astype(str)

            csv_buf = StringIO()
            df.to_csv(
                csv_buf,
                index=False,
                header=False,
                date_format="%Y-%m-%d %H:%M:%S"
            )
            csv_buf.seek(0)
            cur.copy_expert(f"COPY {cfg.staging_table} FROM STDIN WITH CSV", csv_buf)

            total_rows += len(df)
            logger.info(f"Cargado batch con {len(df)} filas (acumulado {total_rows})")

    raw_conn.commit()
    cur.close()
    raw_conn.close()
    logger.info(f"Cargadas {total_rows} filas en staging")



def merge_staging_into_target(cfg: Config, engine):
    merge_sql = f"""
    INSERT INTO {cfg.table} AS t
    SELECT * FROM {cfg.staging_table}
    ON CONFLICT ({cfg.pk}) DO UPDATE
    SET {', '.join([f"{col}=EXCLUDED.{col}" for col in get_table_columns(cfg.table, engine) if col != cfg.pk])}
    """
    with engine.begin() as conn:
        conn.execute(text(merge_sql))
    logger.info("Merge finalizado")


def get_table_columns(table_name: str, engine):
    with engine.connect() as conn:
        result = conn.execute(text(f"""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = '{table_name.split('.')[-1]}'
            ORDER BY ordinal_position
        """))
        return {r[0]: r[1] for r in result}


def process(cfg: Config, engine):
    keys = list_parquet_keys(cfg)
    if not keys:
        logger.warning("No se encontraron archivos parquet")
        return

    tmp_path = f"/tmp/{os.path.basename(keys[0])}"
    s3.download_file(cfg.bucket, keys[0], tmp_path)

    ensure_table_exists(cfg.table, tmp_path, cfg.pk, engine)

    load_parquet_to_staging(cfg, engine, keys)

    if cfg.apply:
        merge_staging_into_target(cfg, engine)


def lambda_handler(event, context):
    cfg = Config(
        bucket="rvas-svicios-parquet",
        base_prefix="rvas-svicios",
        table=os.environ["TABLE_NAME"],
        pk=os.getenv("PK", "_airbyte_ab_id"),
        apply=os.getenv("APPLY", "false").lower() == "true",
        exclude_from_hash=tuple(os.getenv("EXCLUDE_FROM_HASH", "").split(",")) if os.getenv("EXCLUDE_FROM_HASH") else tuple(),
    )

    engine = get_engine()
    process(cfg, engine)

    return {
        "status": "ok"
    }
