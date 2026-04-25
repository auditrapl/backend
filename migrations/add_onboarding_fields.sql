-- ============================================================
-- Migracja: pola onboardingu w tabeli companies
-- Addytywna — nie niszczy istniejących danych
-- ============================================================

ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS onboarding_status VARCHAR(20) NOT NULL DEFAULT 'pending_nip',
    ADD COLUMN IF NOT EXISTS address VARCHAR(500),
    ADD COLUMN IF NOT EXISTS pkd_code VARCHAR(10),
    ADD COLUMN IF NOT EXISTS pkd_description VARCHAR(300);
