CREATE SCHEMA IF NOT EXISTS facturas_bot;

CREATE TABLE facturas_bot.incoming_emails (
	email_id UUID PRIMARY KEY,
	message_id TEXT UNIQUE, -- gmail message id
	received_at TIMESTAMPTZ NOT NULL,
	sender TEXT,
	subject TEXT,
	has_attachments BOOLEAN DEFAULT FALSE,
	attachment_count INTEGER DEFAULT 0,
	s3_key TEXT, -- path al .eml en S3
	processing_state TEXT NOT NULL, -- ver tabla más abajo
	processing_reason TEXT,
	created_at TIMESTAMPTZ DEFAULT now(),
	updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE facturas_bot.invoice_cases (
	case_id UUID PRIMARY KEY,
	email_id UUID REFERENCES facturas_bot.incoming_emails(email_id),
	attachment_hash TEXT NOT NULL,
	attachment_name TEXT,
	operator_cuit TEXT,
	operator_id INTEGER,
	state TEXT NOT NULL,
	state_reason TEXT,
	extraction_method TEXT,
	created_at TIMESTAMPTZ DEFAULT now(),
	updated_at TIMESTAMPTZ DEFAULT now(),
	UNIQUE(attachment_hash)
);

CREATE TABLE facturas_bot.invoice_transitions (
	id SERIAL PRIMARY KEY,
	case_id UUID NOT NULL REFERENCES facturas_bot.invoice_cases(case_id),
	from_state TEXT,
	to_state TEXT NOT NULL,
	reason TEXT,
	actor TEXT,
	metadata JSON,
	created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE facturas_bot.invoices_extracted_emails (
	id SERIAL PRIMARY KEY,
	cuit TEXT NOT NULL,
	ids_operadores INTEGER[],
	s3_key TEXT,
	numero_factura TEXT,
	fecha_factura TEXT,
	razon_social TEXT,
	moneda TEXT,
	importe_total NUMERIC(12,2),
	tipo_comprobante TEXT,
	punto_venta TEXT,
	numero_comprobante TEXT,
	cotizacion TEXT,
	case_id UUID NOT NULL REFERENCES facturas_bot.invoice_cases(case_id),
	created_at TIMESTAMP DEFAULT now(),
	updated_at TIMESTAMP DEFAULT now(),
	CONSTRAINT _invoice_unique_constraint_ UNIQUE (cuit, s3_key, numero_factura)
);

CREATE TABLE facturas_bot.services_extracted_emails (
	id SERIAL PRIMARY KEY,
	invoice_id INTEGER NOT NULL REFERENCES facturas_bot.invoices_extracted_emails(id),
	codigo TEXT,
	pasajero TEXT,
	importe NUMERIC(12,2),
	vinculado BOOLEAN DEFAULT FALSE,
	id_servicio INTEGER,
	id_reserva_aptour INTEGER,
	id_reserva_mo INTEGER,
	id_operador INTEGER,
	importe_usd NUMERIC(12,2),
	ya_facturado BOOLEAN DEFAULT FALSE,
	factura TEXT,
	pending BOOLEAN DEFAULT TRUE,
	created_at TIMESTAMP DEFAULT now()
);

CREATE INDEX idx_emails_received ON facturas_bot.incoming_emails(received_at);
CREATE INDEX idx_emails_sender ON facturas_bot.incoming_emails(sender);
CREATE INDEX idx_emails_processing_state ON facturas_bot.incoming_emails(processing_state);
CREATE INDEX idx_cases_state ON facturas_bot.invoice_cases(state);
CREATE INDEX idx_cases_cuit ON facturas_bot.invoice_cases(operator_cuit);
CREATE INDEX idx_cases_email ON facturas_bot.invoice_cases(email_id);
CREATE INDEX idx_transitions_case ON facturas_bot.invoice_transitions(case_id);
CREATE INDEX idx_invoices_case_id ON facturas_bot.invoices_extracted_emails(case_id);
CREATE INDEX idx_invoices_cuit ON facturas_bot.invoices_extracted_emails(cuit);
CREATE INDEX idx_services_invoice_id ON facturas_bot.services_extracted_emails(invoice_id);