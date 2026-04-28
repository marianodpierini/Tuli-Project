from sqlalchemy import Column, Text, DateTime, Integer, func, Numeric, ForeignKey, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.orm import relationship

Base = declarative_base()


class InvoicesExtractedEmails(Base):
    __tablename__ = "invoices_extracted_emails"
    __table_args__ = {"schema": "facturas_bot"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    cuit = Column(Text, nullable=False)
    ids_operadores = Column(ARRAY(Integer))
    s3_key = Column(Text)
    numero_factura = Column(Text)
    fecha_factura = Column(Text)
    razon_social = Column(Text)
    moneda = Column(Text)
    importe_total = Column(Numeric(precision=12, scale=2))
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    services = relationship(
        "ServicesExtractedEmails",
        back_populates="invoice",
        cascade="all, delete-orphan"
    )


class ServicesExtractedEmails(Base):
    __tablename__ = "services_extracted_emails"
    __table_args__ = {"schema": "facturas_bot"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    invoice_id = Column(
        Integer,
        ForeignKey("facturas_bot.invoices_extracted_emails.id"),
        nullable=False
    )
    codigo = Column(Text)
    pasajero = Column(Text)
    importe = Column(Numeric(12, 2))
    vinculado = Column(Boolean)
    id_servicio = Column(Integer)
    id_reserva = Column(Integer)
    importeUSD = Column(Numeric(12, 2))
    ya_facturado = Column(Boolean)
    factura = Column(Text)
    pending = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    invoice = relationship(
        "InvoicesExtractedEmails",
        back_populates="services"
    )
