ALTER TABLE users
    ADD COLUMN IF NOT EXISTS stripe_customer_id VARCHAR,
    ADD COLUMN IF NOT EXISTS stripe_subscription_id VARCHAR;

CREATE INDEX IF NOT EXISTS ix_users_stripe_customer_id ON users(stripe_customer_id);
CREATE INDEX IF NOT EXISTS ix_users_stripe_subscription_id ON users(stripe_subscription_id);

CREATE TABLE IF NOT EXISTS billing_plans (
    id UUID PRIMARY KEY,
    stripe_product_id VARCHAR NOT NULL,
    stripe_price_id VARCHAR NOT NULL UNIQUE,
    name VARCHAR NOT NULL,
    description TEXT,
    currency VARCHAR NOT NULL DEFAULT 'USD',
    interval VARCHAR,
    interval_count INTEGER,
    amount NUMERIC(10, 2),
    metadata_json JSON,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_billing_plans_stripe_product_id ON billing_plans(stripe_product_id);
CREATE INDEX IF NOT EXISTS ix_billing_plans_stripe_price_id ON billing_plans(stripe_price_id);

CREATE TABLE IF NOT EXISTS billing_subscriptions (
    id UUID PRIMARY KEY,
    company_id UUID NOT NULL REFERENCES companies(id),
    user_id UUID REFERENCES users(id),
    plan_id UUID REFERENCES billing_plans(id),
    stripe_subscription_id VARCHAR NOT NULL UNIQUE,
    stripe_customer_id VARCHAR NOT NULL,
    status VARCHAR NOT NULL DEFAULT 'incomplete',
    current_period_start TIMESTAMP,
    current_period_end TIMESTAMP,
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
    canceled_at TIMESTAMP,
    trial_end TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_billing_subscriptions_company_id ON billing_subscriptions(company_id);
CREATE INDEX IF NOT EXISTS ix_billing_subscriptions_user_id ON billing_subscriptions(user_id);
CREATE INDEX IF NOT EXISTS ix_billing_subscriptions_plan_id ON billing_subscriptions(plan_id);
CREATE INDEX IF NOT EXISTS ix_billing_subscriptions_stripe_subscription_id ON billing_subscriptions(stripe_subscription_id);
CREATE INDEX IF NOT EXISTS ix_billing_subscriptions_stripe_customer_id ON billing_subscriptions(stripe_customer_id);

CREATE TABLE IF NOT EXISTS billing_payments (
    id UUID PRIMARY KEY,
    company_id UUID NOT NULL REFERENCES companies(id),
    user_id UUID REFERENCES users(id),
    subscription_id UUID REFERENCES billing_subscriptions(id),
    stripe_invoice_id VARCHAR,
    stripe_payment_intent_id VARCHAR UNIQUE,
    amount NUMERIC(10, 2) NOT NULL,
    currency VARCHAR NOT NULL DEFAULT 'USD',
    status VARCHAR NOT NULL DEFAULT 'pending',
    paid_at TIMESTAMP,
    failure_message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_billing_payments_company_id ON billing_payments(company_id);
CREATE INDEX IF NOT EXISTS ix_billing_payments_user_id ON billing_payments(user_id);
CREATE INDEX IF NOT EXISTS ix_billing_payments_subscription_id ON billing_payments(subscription_id);
CREATE INDEX IF NOT EXISTS ix_billing_payments_stripe_invoice_id ON billing_payments(stripe_invoice_id);
CREATE INDEX IF NOT EXISTS ix_billing_payments_stripe_payment_intent_id ON billing_payments(stripe_payment_intent_id);

CREATE TABLE IF NOT EXISTS billing_invoices (
    id UUID PRIMARY KEY,
    company_id UUID NOT NULL REFERENCES companies(id),
    user_id UUID REFERENCES users(id),
    subscription_id UUID REFERENCES billing_subscriptions(id),
    stripe_invoice_id VARCHAR NOT NULL UNIQUE,
    number VARCHAR,
    status VARCHAR NOT NULL DEFAULT 'draft',
    amount_due NUMERIC(10, 2),
    amount_paid NUMERIC(10, 2),
    currency VARCHAR NOT NULL DEFAULT 'USD',
    invoice_pdf_url VARCHAR,
    hosted_invoice_url VARCHAR,
    period_start TIMESTAMP,
    period_end TIMESTAMP,
    due_date TIMESTAMP,
    paid_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_billing_invoices_company_id ON billing_invoices(company_id);
CREATE INDEX IF NOT EXISTS ix_billing_invoices_user_id ON billing_invoices(user_id);
CREATE INDEX IF NOT EXISTS ix_billing_invoices_subscription_id ON billing_invoices(subscription_id);
CREATE INDEX IF NOT EXISTS ix_billing_invoices_stripe_invoice_id ON billing_invoices(stripe_invoice_id);
