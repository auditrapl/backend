-- Migracja: dodanie kolumny summary do proactive_insights
-- Data: 2026-03-21
-- Powód: kolumna wymagana przez model ProactiveInsight dla porrannego briefingu

ALTER TABLE proactive_insights ADD COLUMN IF NOT EXISTS summary TEXT;
