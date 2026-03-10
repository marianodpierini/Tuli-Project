import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from pgvector.psycopg2 import register_vector

host = os.environ['DB_HOST']
dbname = os.environ['DB_NAME']
user = os.environ['DB_USER']
password = os.environ['DB_PASSWORD']
port = 5432

DATABASE_URL = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{dbname}"

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=10, connect_args={"options": "-csearch_path=aptour,public"})

@event.listens_for(engine, "connect")
def register_pgvector(dbapi_connection, connection_record):
    register_vector(dbapi_connection)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()