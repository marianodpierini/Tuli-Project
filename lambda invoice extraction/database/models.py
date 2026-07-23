import uuid

from sqlalchemy import (
    Column,
    Text,
    DateTime,
    Date,
    Integer,
    func,
    Numeric,
    ForeignKey,
    Boolean,
    TIMESTAMP,
    JSON,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql.schema import UniqueConstraint

Base = declarative_base()


class InvoicesExtractedEmails(Base):
    __tablename__ = "invoices_extracted_emails"
    __table_args__ = (
        UniqueConstraint(
            "cuit",
            "s3_key",
            "numero_factura",
            name="_invoice_unique_constraint_",
        ),
        {"schema": "facturas_bot"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    cuit = Column(Text, nullable=False)
    ids_operadores = Column(ARRAY(Integer))
    s3_key = Column(Text)
    numero_factura = Column(Text)
    fecha_factura = Column(Date)
    razon_social = Column(Text)
    moneda = Column(Text)
    importe_total = Column(Numeric(precision=12, scale=2))
    tipo_comprobante = Column(Text)
    punto_venta = Column(Text)
    numero_comprobante = Column(Text)
    cotizacion = Column(Numeric(precision=12, scale=4))
    case_id = Column(UUID(as_uuid=True), ForeignKey("facturas_bot.invoice_cases.case_id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    services = relationship(
        "ServicesExtractedEmails",
        back_populates="invoice",
        cascade="all, delete-orphan",
    )

    case = relationship("InvoiceCases", back_populates="invoices")


class ServicesExtractedEmails(Base):
    __tablename__ = "services_extracted_emails"
    __table_args__ = {"schema": "facturas_bot"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    invoice_id = Column(
        Integer, ForeignKey("facturas_bot.invoices_extracted_emails.id"), nullable=False
    )
    codigo = Column(Text)
    pasajero = Column(Text)
    importe = Column(Numeric(12, 2))
    vinculado = Column(Boolean, default=False)
    id_servicio = Column(Integer)
    id_reserva_aptour = Column(Integer)
    id_reserva_mo = Column(Integer)
    id_operador = Column(Integer)
    importe_usd = Column(Numeric(12, 2))
    ya_facturado = Column(Boolean, default=False)
    factura = Column(Text)
    pending = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    invoice = relationship("InvoicesExtractedEmails", back_populates="services")


class IncomingEmails(Base):
    __tablename__ = "incoming_emails"
    __table_args__ = {"schema": "facturas_bot"}
    email_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id = Column(Text, unique=True)
    received_at = Column(TIMESTAMP(timezone=True), nullable=False)
    sender = Column(Text)
    subject = Column(Text)
    has_attachments = Column(Boolean, default=False)
    attachment_count = Column(Integer, default=0)
    s3_key = Column(Text)
    processing_state = Column(Text, nullable=False)
    processing_reason = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    cases = relationship("InvoiceCases", back_populates="email")


class InvoiceCases(Base):
    __tablename__ = "invoice_cases"
    __table_args__ = (
        UniqueConstraint("attachment_hash"),
        {"schema": "facturas_bot"},
    )
    case_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email_id = Column(
        UUID(as_uuid=True), ForeignKey("facturas_bot.incoming_emails.email_id")
    )
    attachment_hash = Column(Text, nullable=False)
    attachment_name = Column(Text)
    operator_cuit = Column(Text)
    operator_id = Column(Integer)
    state = Column(Text, nullable=False)
    state_reason = Column(Text)
    extraction_method = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    email = relationship("IncomingEmails", back_populates="cases")
    transitions = relationship("InvoiceTransitions", back_populates="case")
    invoices = relationship("InvoicesExtractedEmails", back_populates="case")


class InvoiceTransitions(Base):
    __tablename__ = "invoice_transitions"
    __table_args__ = {"schema": "facturas_bot"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(
        UUID(as_uuid=True),
        ForeignKey("facturas_bot.invoice_cases.case_id"),
        nullable=False,
    )
    from_state = Column(Text)
    to_state = Column(Text, nullable=False)
    reason = Column(Text)
    actor = Column(Text)
    metadata_ = Column("metadata", JSON)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    case = relationship("InvoiceCases", back_populates="transitions")
