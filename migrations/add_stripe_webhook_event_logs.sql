CREATE TABLE IF NOT EXISTS stripe_webhook_event_logs (
    id UUID PRIMARY KEY,
    event_id VARCHAR NOT NULL UNIQUE,
    event_type VARCHAR NOT NULL,
    company_id UUID REFERENCES companies(id),
    status VARCHAR NOT NULL DEFAULT 'received',
    customer_id VARCHAR,
    error_message TEXT,
    payload JSON,
    received_at TIMESTAMP NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_stripe_webhook_event_logs_event_id ON stripe_webhook_event_logs(event_id);
CREATE INDEX IF NOT EXISTS ix_stripe_webhook_event_logs_event_type ON stripe_webhook_event_logs(event_type);
CREATE INDEX IF NOT EXISTS ix_stripe_webhook_event_logs_company_id ON stripe_webhook_event_logs(company_id);
CREATE INDEX IF NOT EXISTS ix_stripe_webhook_event_logs_customer_id ON stripe_webhook_event_logs(customer_id);
