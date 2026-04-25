-- ============================================================
-- Migracja: Dodanie pola category do tabeli conversations
-- Kategorie: analiza, raportowanie, strategia, anomalie, podatki
-- ============================================================

ALTER TABLE conversations ADD COLUMN IF NOT EXISTS category VARCHAR(32);
CREATE INDEX IF NOT EXISTS ix_conversations_category ON conversations (category);
