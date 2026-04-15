from sqlalchemy import Column, Float, BigInteger, Boolean, Text, DateTime, String, Integer, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.dialects.postgresql import JSONB, ARRAY

Base = declarative_base()


class InvoicesExtractedEmails(Base):
    __tablename__ = "invoices_extracted_emails"
    __table_args__ = {"schema": "facturas_bot"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    cuit = Column(Text, nullable=False)
    ids_operadores = Column(ARRAY(Integer))
    s3_key = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
