-- Migration: Rozszerzenie ProactiveInsight o Tax Eye + CFO Radar
-- Data: 2026-04-15

ALTER TABLE proactive_insights
  ADD COLUMN IF NOT EXISTS insight_type VARCHAR NOT NULL DEFAULT 'morning',
  ADD COLUMN IF NOT EXISTS legal_basis TEXT,
  ADD COLUMN IF NOT EXISTS estimated_savings NUMERIC(12, 2),
  ADD COLUMN IF NOT EXISTS source_invoice_id UUID REFERENCES invoices(id),
  ADD COLUMN IF NOT EXISTS is_dismissed BOOLEAN NOT NULL DEFAULT false;

-- Index na type + company dla szybkiego filtrowania
CREATE INDEX IF NOT EXISTS ix_proactive_insights_type_company
  ON proactive_insights (company_id, insight_type, created_at DESC);

-- Index na source_invoice_id
CREATE INDEX IF NOT EXISTS ix_proactive_insights_source_invoice
  ON proactive_insights (source_invoice_id)
  WHERE source_invoice_id IS NOT NULL;
