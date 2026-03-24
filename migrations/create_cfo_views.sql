-- ============================================================
-- CFO Agent Views: AR/AP Aging + Cash Forecast
-- Uruchom w: Supabase SQL Editor (lub psql)
-- ============================================================

-- ------------------------------------------------------------
-- WIDOK 1: v_ar_ap_aging
-- Aging zatorów płatniczych (Accounts Receivable / Payable)
-- AR = faktury przychodowe od klientów (oni nam winni)
-- AP = faktury kosztowe do dostawców  (my jesteśmy winni)
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW v_ar_ap_aging AS
SELECT
    company_id,
    'AR'                        AS type,           -- należności od klientów
    contractor_name,
    SUM(gross_amount)           AS total_overdue_pln,
    AVG(
        EXTRACT(DAY FROM NOW() - CAST(parsed_data->>'due_date' AS TIMESTAMP))
    )::INTEGER                  AS days_overdue
FROM invoices
WHERE
    invoice_type = 'income'
    AND is_paid   = FALSE
    AND parsed_data->>'due_date' IS NOT NULL
    AND CAST(parsed_data->>'due_date' AS TIMESTAMP) < NOW()
GROUP BY company_id, contractor_name

UNION ALL

SELECT
    company_id,
    'AP'                        AS type,           -- nasze długi wobec dostawców
    contractor_name,
    SUM(gross_amount)           AS total_overdue_pln,
    AVG(
        EXTRACT(DAY FROM NOW() - CAST(parsed_data->>'due_date' AS TIMESTAMP))
    )::INTEGER                  AS days_overdue
FROM invoices
WHERE
    invoice_type = 'expense'
    AND is_paid   = FALSE
    AND parsed_data->>'due_date' IS NOT NULL
    AND CAST(parsed_data->>'due_date' AS TIMESTAMP) < NOW()
GROUP BY company_id, contractor_name;


-- ------------------------------------------------------------
-- WIDOK 2: v_cash_forecast
-- Prognoza gotówkowa na najbliższe 7 dni
-- Pokazuje: ile musimy wypłacić vs ile spłyną w 7 dni
-- ------------------------------------------------------------
CREATE OR REPLACE VIEW v_cash_forecast AS
SELECT
    company_id,
    COALESCE(SUM(
        CASE
            WHEN invoice_type = 'expense'
             AND is_paid = FALSE
             AND parsed_data->>'due_date' IS NOT NULL
             AND CAST(parsed_data->>'due_date' AS TIMESTAMP) BETWEEN NOW() AND NOW() + INTERVAL '7 days'
            THEN gross_amount
            ELSE 0
        END
    ), 0) AS upcoming_outflow_7_days,

    COALESCE(SUM(
        CASE
            WHEN invoice_type = 'income'
             AND is_paid = FALSE
             AND parsed_data->>'due_date' IS NOT NULL
             AND CAST(parsed_data->>'due_date' AS TIMESTAMP) BETWEEN NOW() AND NOW() + INTERVAL '7 days'
            THEN gross_amount
            ELSE 0
        END
    ), 0) AS expected_inflow_7_days

FROM invoices
GROUP BY company_id;


-- ------------------------------------------------------------
-- OPCJONALNIE: Sprawdź czy widoki działają
-- (podmień UUID na swój company_id)
-- ------------------------------------------------------------
-- SELECT * FROM v_ar_ap_aging   WHERE company_id = 'your-uuid-here';
-- SELECT * FROM v_cash_forecast WHERE company_id = 'your-uuid-here';
