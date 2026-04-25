-- ============================================================
-- Migracja: tabela bank_transactions do matching z fakturami
-- ============================================================

CREATE TABLE IF NOT EXISTS bank_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    transaction_date TIMESTAMPTZ NOT NULL,
    amount NUMERIC(12,2) NOT NULL,
    title TEXT,
    counterparty_name VARCHAR(500),
    counterparty_account VARCHAR(100),
    reference VARCHAR(200),
    balance_after NUMERIC(12,2),
    source_filename VARCHAR(500),
    -- matching
    matched_invoice_id UUID REFERENCES invoices(id) ON DELETE SET NULL,
    match_confidence NUMERIC(4,2),
    match_status VARCHAR(20) NOT NULL DEFAULT 'unmatched',
    matched_at TIMESTAMPTZ,
    matched_by VARCHAR(20),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bank_tx_company ON bank_transactions(company_id);
CREATE INDEX IF NOT EXISTS idx_bank_tx_status ON bank_transactions(company_id, match_status);
CREATE INDEX IF NOT EXISTS idx_bank_tx_invoice ON bank_transactions(matched_invoice_id);
