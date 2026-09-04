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
            "numero_factura",
            "tipo_comprobante",
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
    exento = Column(Numeric(precision=12, scale=2))
    no_computable = Column(Numeric(precision=12, scale=2))
    gravado_21 = Column(Numeric(precision=12, scale=2))
    gravado_105 = Column(Numeric(precision=12, scale=2))
    percepcion_iva = Column(Numeric(precision=12, scale=2))
    subtotal_control = Column(Numeric(precision=12, scale=2))
    descuento_control = Column(Numeric(precision=12, scale=2))
    total_sin_iva_control = Column(Numeric(precision=12, scale=2))
    total_control = Column(Numeric(precision=12, scale=2))
    voucher = Column(Text)
    case_id = Column(UUID(as_uuid=True), ForeignKey("facturas_bot.invoice_cases.case_id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    services = relationship(
        "ServicesExtractedEmails",
        back_populates="invoice",
        cascade="all, delete-orphan",
    )

    percepciones_iibb = relationship(
        "PercepcionesIIBB",
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
    desc_neto = Column(Numeric(12, 2))
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


class PercepcionesIIBB(Base):
    __tablename__ = "percepciones_iibb"
    __table_args__ = {"schema": "facturas_bot"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    invoice_id = Column(
        Integer, ForeignKey("facturas_bot.invoices_extracted_emails.id"), nullable=False
    )
    provincia = Column(Text)
    monto = Column(Numeric(precision=12, scale=2))
    id_provincia = Column(Integer)
    created_at = Column(DateTime, server_default=func.now())

    invoice = relationship("InvoicesExtractedEmails", back_populates="percepciones_iibb")


class PMysqlPagoproveedoresproductionInvoices(Base):
    __tablename__ = "p_mysql_pagoproveedoresproduction_invoices"
    __table_args__ = {"schema": "public"}

    _airbyte_raw_id = Column(Text)
    _airbyte_extracted_at = Column(TIMESTAMP(timezone=True))
    _airbyte_meta = Column(JSON)
    _airbyte_generation_id = Column(Integer)
    id = Column(Integer)
    branch = Column(Text)
    number = Column(Text)
    id_comp = Column(Text)
    reserve_id = Column(Integer)
    operator_aptour_id = Column(Integer)


class PMysqlProductionmotoursReserves(Base):
    __tablename__ = "p_mysql_productionmotours_reserves"
    __table_args__ = {"schema": "public"}

    _airbyte_raw_id = Column(Text)
    _airbyte_extracted_at = Column(TIMESTAMP(timezone=True))
    _airbyte_meta = Column(JSON)
    _airbyte_generation_id = Column(Integer)

    id = Column(Integer)
    uuid = Column(Text)
    group = Column(Boolean)
    status = Column(Text)
    cell_id = Column(Integer)
    date_in = Column(Date)
    details = Column(Text)
    migrate = Column(Boolean)
    visible = Column(Boolean)
    canceled = Column(Boolean)
    currency = Column(Text)
    date_out = Column(Date)
    invoiced = Column(Boolean)
    priority = Column(Text)
    rentable = Column(Boolean)
    taken_at = Column(TIMESTAMP(timezone=True))
    agency_id = Column(Integer)
    by_agency = Column(Boolean)
    pax_count = Column(Integer)
    seller_id = Column(Integer)
    status_ok = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True))
    creator_id = Column(Integer)
    deleted_at = Column(TIMESTAMP(timezone=True))
    process_at = Column(TIMESTAMP(timezone=True))
    updated_at = Column(TIMESTAMP(timezone=True))
    voucher_at = Column(TIMESTAMP(timezone=True))
    agency_mail = Column(Text)
    agency_name = Column(Text)
    archived_at = Column(TIMESTAMP(timezone=True))
    ars_balance = Column(Numeric(14, 2))
    assigned_by = Column(Integer)
    invoice_iva = Column(Boolean)
    location_id = Column(Integer)
    modified_at = Column(Date)
    promotor_id = Column(Integer)
    usd_balance = Column(Numeric(14, 2))
    white_label = Column(Boolean)
    aero_invoice = Column(Boolean)
    balance_date = Column(TIMESTAMP(timezone=True))
    has_only_gds = Column(Boolean)
    observations = Column(Text)
    operative_id = Column(Integer)
    payment_info = Column(Text)
    people_count = Column(Integer)
    processor_id = Column(Integer)
    solicited_at = Column(TIMESTAMP(timezone=True))
    solicitor_id = Column(Integer)
    guaranteed_at = Column(TIMESTAMP(timezone=True))
    original_iibb = Column(Numeric(14, 2))
    tax_pais_type = Column(Text)
    voucher_close = Column(Boolean)
    admin_expenses = Column(Numeric(14, 2))
    aptour_details = Column(Text)
    destination_id = Column(Integer)
    emission_state = Column(Text)
    new_cotization = Column(Numeric(14, 2))
    pre_liquidated = Column(Boolean)
    responsible_id = Column(Integer)
    value_tax_pais = Column(Numeric(14, 2))
    vouchers_count = Column(Integer)
    close_automatic = Column(Boolean)
    expiration_date = Column(Date)
    payment_details = Column(Text)
    services_number = Column(Integer)
    voucher_user_id = Column(Integer)
    voucher_visible = Column(Boolean)
    auto_liquidation = Column(Boolean)
    responsible_uuid = Column(Text)
    score_operations = Column(Text)
    second_seller_id = Column(Integer)
    voucher_comments = Column(Text)
    voucher_priority = Column(Text)
    white_label_code = Column(Text)
    aptour_reserve_id = Column(Integer)
    asked_for_closing = Column(Boolean)
    bsp_exchange_rate = Column(Numeric(14, 6))
    close_description = Column(Text)
    cotizador_cart_id = Column(Integer)
    eur_exchange_rate = Column(Numeric(14, 6))
    promotional_codes = Column(Text)
    usd_exchange_rate = Column(Numeric(14, 6))
    admin_expenses_iva = Column(Numeric(14, 2))
    liquidations_count = Column(Integer)
    tax_pais_value_eur = Column(Numeric(14, 2))
    tax_pais_value_usd = Column(Numeric(14, 2))
    flights_information = Column(Text)
    liquidation_visible = Column(Boolean)
    orange_date_scoring = Column(TIMESTAMP(timezone=True))
    reason_to_highlight = Column(Text)
    surfer_client_email = Column(Text)
    voucher_priority_at = Column(TIMESTAMP(timezone=True))
    asked_for_closing_at = Column(TIMESTAMP(timezone=True))
    original_archived_at = Column(TIMESTAMP(timezone=True))
    responsible_name_pax = Column(Text)
    taken_process_bag_at = Column(TIMESTAMP(timezone=True))
    with_expiration_date = Column(Boolean)
    auto_liquidation_date = Column(TIMESTAMP(timezone=True))
    notify_administration = Column(Boolean)
    processed_white_label = Column(Boolean)
    vouchers_observations = Column(Text)
    ars_expenses_aptour_id = Column(Integer)
    asked_for_guarantee_at = Column(TIMESTAMP(timezone=True))
    eur_expenses_aptour_id = Column(Integer)
    eur_tax_pais_aptour_id = Column(Integer)
    main_aptour_reserve_id = Column(Integer)
    usd_expenses_aptour_id = Column(Integer)
    usd_tax_pais_aptour_id = Column(Integer)
    agency_voucher_solicite = Column(Boolean)
    multiliquidations_count = Column(Integer)
    admin_expenses_aptour_id = Column(Integer)
    autovoucher_comment = Column(Text)
    payment_anticipation_type = Column(Text)
    payment_anticipation_date = Column(Date)
    payment_anticipation_amount = Column(Numeric(14, 2))
    payment_anticipation_currency = Column(Text)
    payment_anticipation_details = Column(Text)
    payment_anticipation_aptour_id = Column(Integer)
    exchange_rates_updated_at = Column(TIMESTAMP(timezone=True))
    bonus_admin_expenses_percentages = Column(Numeric(14, 6))
    last_invoice_iva_reliquidation_at = Column(TIMESTAMP(timezone=True))
    invoiced_total_tax_pais_usd = Column(Numeric(14, 2))
    agency_authorization_code = Column(Text)
    taken_of_voucher_bag_at = Column(TIMESTAMP(timezone=True))
    voucher_administrative_approved = Column(Boolean)
    integration_dates_expansion_notice_pending = Column(Boolean)
    historic_closing_automation = Column(Text)


class SMysqlProductionmotoursServices(Base):
    __tablename__ = "s_mysql_productionmotours_services"
    __table_args__ = {"schema": "public"}

    _airbyte_raw_id = Column(Text)
    _airbyte_extracted_at = Column(TIMESTAMP(timezone=True))
    _airbyte_meta = Column(JSON)
    _airbyte_generation_id = Column(Integer)

    id = Column(Integer)
    reserve_id = Column(Integer)
    number = Column(Integer)
    aptour_reserve_id = Column(Integer)
    aptour_service_number = Column(Integer)
    cart_id = Column(Integer)
    kind = Column(Text)
    name = Column(Text)
    status_rentability = Column(Text)
    status_strategy = Column(Text)
    status = Column(Text)
    operator_id = Column(Integer)
    operator_name = Column(Text)
    provider_id = Column(Integer)
    provider_hotel_id = Column(Integer)
    provider_name = Column(Text)
    provider_info = Column(Text)
    location_id = Column(Integer)
    destination_id = Column(Integer)
    destination_name = Column(Text)
    origin_name = Column(Text)
    date_in = Column(Date)
    date_out = Column(Date)
    deadline = Column(Date)
    expires_at = Column(TIMESTAMP(timezone=True))
    modified = Column(Date)
    created_at = Column(TIMESTAMP(timezone=True))
    updated_at = Column(TIMESTAMP(timezone=True))
    deleted_at = Column(TIMESTAMP(timezone=True))
    balance = Column(Numeric(14, 2))
    down_payment = Column(Numeric(14, 2))
    exchange_rate = Column(Numeric(14, 6))
    new_cotization = Column(Numeric(14, 6))
    emition = Column(Numeric(14, 2))
    created_from = Column(Text)
    creator_id = Column(Integer)
    responsible_passenger_uuid = Column(Text)
    operator_mask = Column(Text)
    operator_ph_code = Column(Text)
    emergency_phone = Column(Text)
    emergency_telephone = Column(Text)
    telephone = Column(Text)
    phones = Column(Text)
    address = Column(Text)
    confirmation_code = Column(Text)
    booking_result = Column(Text)
    booking_service_id = Column(Text)
    external_id = Column(Text)
    fastx_code = Column(Text)
    surfer_code = Column(Text)
    surfer_status = Column(Text)
    source = Column(Text)
    condition = Column(Text)
    condition_date = Column(Date)
    tariff_conditions = Column(Text)
    observations = Column(Text)
    voucher_observations = Column(Text)
    prevision_details = Column(Text)
    prevision_details_eur = Column(Text)
    payment_details = Column(Text)
    by_agency = Column(Boolean)
    visible = Column(Boolean)
    checked = Column(Boolean)
    pay_checked = Column(Boolean)
    sent_query = Column(Boolean)
    informed_at = Column(TIMESTAMP(timezone=True))
    solicited_at = Column(TIMESTAMP(timezone=True))
    received_response = Column(TIMESTAMP(timezone=True))
    rebooking_at = Column(TIMESTAMP(timezone=True))
    rebooking_code = Column(Text)
    rebooking_code_from = Column(Text)
    rebooking_engine = Column(Text)
    express_confirmation = Column(Boolean)
    send_email_conflicting_operator = Column(Boolean)
    scheduled_cancellation_date = Column(Date)
    status_ok = Column(Text)
    package = Column(Boolean)
    disney = Column(Boolean)
    operative_id = Column(Integer)
    data_id = Column(Integer)
    data_type = Column(Text)
    reserve_non_operational_id = Column(Integer)
    taxes_aptour_id = Column(Integer)
    fee_aptour_id = Column(Integer)
    adjustment_aptour_id = Column(Integer)
    operator_charge_aptour_id = Column(Integer)
    aerial_id = Column(Integer)
    zk_aptour_id = Column(Integer)
    lock_fare_reserves_id = Column(Integer)
    nemo_operator = Column(Boolean)
    argentine_mode = Column(Boolean)
    initial_argentine_mode_set = Column(Boolean)
    modification = Column(Text)
    down_payment_date = Column(Date)
    balance_date = Column(TIMESTAMP(timezone=True))
    ebb = Column(Boolean)
    ebb_date = Column(Date)
    price_state = Column(Text)
