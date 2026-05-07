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
	metadata JSONB,
	created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_emails_received ON facturas_bot.incoming_emails(received_at);
CREATE INDEX idx_emails_sender ON facturas_bot.incoming_emails(sender);
CREATE INDEX idx_emails_processing_state ON facturas_bot.incoming_emails(processing_state);
CREATE INDEX idx_cases_state ON facturas_bot.invoice_cases(state);
CREATE INDEX idx_cases_cuit ON facturas_bot.invoice_cases(operator_cuit);
CREATE INDEX idx_cases_email ON facturas_bot.invoice_cases(email_id);
CREATE INDEX idx_transitions_case ON facturas_bot.invoice_transitions(case_id);