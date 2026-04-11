ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR,
    ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR,
    ADD COLUMN IF NOT EXISTS stripe_price_id VARCHAR,
    ADD COLUMN IF NOT EXISTS billing_status VARCHAR DEFAULT 'inactive',
    ADD COLUMN IF NOT EXISTS billing_currency VARCHAR,
    ADD COLUMN IF NOT EXISTS billing_interval VARCHAR,
    ADD COLUMN IF NOT EXISTS billing_amount NUMERIC(10, 2),
    ADD COLUMN IF NOT EXISTS billing_current_period_end TIMESTAMP,
    ADD COLUMN IF NOT EXISTS billing_cancel_at_period_end BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS billing_last_event_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS ix_companies_stripe_customer_id ON companies(stripe_customer_id);
CREATE INDEX IF NOT EXISTS ix_companies_stripe_subscription_id ON companies(stripe_subscription_id);
