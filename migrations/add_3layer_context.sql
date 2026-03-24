-- ============================================================
-- Migracja: 3-warstwowy system zarządzania kontekstem firmy
-- Addytywna — nie niszczy istniejących danych (context_data zachowany)
-- ============================================================

-- Krok 1: Rozszerz tabelę company_contexts o nowe kolumny
ALTER TABLE company_contexts
    ADD COLUMN IF NOT EXISTS canonical_data JSONB,
    ADD COLUMN IF NOT EXISTS canonical_version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS canonical_updated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS operational_data JSONB,
    ADD COLUMN IF NOT EXISTS operational_version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS operational_updated_at TIMESTAMPTZ;

-- Krok 2: Utwórz tabelę historycznych snapshotów (Warstwa 3)
CREATE TABLE IF NOT EXISTS context_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    period_type VARCHAR(10) NOT NULL CHECK (period_type IN ('weekly', 'monthly')),
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    operational_snapshot JSONB NOT NULL,
    ai_summary TEXT,
    key_metrics JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indeks do szybkiego pobierania snapshotów per firma
CREATE INDEX IF NOT EXISTS idx_context_snapshots_company_period
    ON context_snapshots(company_id, period_type, period_start DESC);
