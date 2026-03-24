"""
test_tools.py — Standalone verification that AFC (Automatic Function Calling)
actually executes Python tool functions instead of hallucinating.

Run:  python test_tools.py
Requires:  GEMINI_API_KEY env variable set.

Expected output:
  [AFC_TOOL_EXEC] >>> execute_sql_query ...
  [AFC_TOOL_RESULT] <<< execute_sql_query: [{"count": 42}]
  [AFC_CONFIRMED] Tool 'execute_sql_query' fired 1 time(s)
  [FINAL_ANSWER] ...
"""
import asyncio
import json
import logging
import os
import functools
from google import genai
from google.genai import types

# ── Logging setup (visible in terminal) ──────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_tools")

# ── AFC Config (same as production) ──────────────────────────────────────────
AFC_CONFIG = types.AutomaticFunctionCallingConfig(
    disable=False,
    maximum_remote_calls=10,
)

# ── MOCK TOOLS (no real DB needed) ───────────────────────────────────────────
MOCK_COMPANY_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
tool_call_counter = {"execute_sql_query": 0, "search_corporate_memory": 0}


async def execute_sql_query(query: str) -> str:
    """
    Wykonuje zapytanie PostgreSQL (tylko odczyt - SELECT/WITH).
    WAŻNE: Każde zapytanie MUSI zawierać filtr company_id.

    Tabela `invoices`: id, company_id, invoice_type ('expense'/'income'),
        contractor_name, net_amount, gross_amount, issue_date, is_paid.
    Tabela `departments`: id, company_id, name, monthly_limit.

    Zwraca wyniki jako JSON string.
    """
    tool_call_counter["execute_sql_query"] += 1
    logger.info(f"\n{'='*60}\n[AFC_TOOL_EXEC] >>> execute_sql_query\n  query: {query}\n{'='*60}")

    # Return mock data based on query intent
    q = query.upper()
    if "COUNT" in q:
        result = [{"count": 42}]
    elif "SUM" in q and "EXPENSE" in q:
        result = [{"total_expenses": 187500.00, "currency": "PLN"}]
    elif "SUM" in q:
        result = [{"total_income": 320000.00, "total_expenses": 187500.00}]
    elif "AVG" in q:
        result = [{"avg_invoice_amount": 7500.50}]
    elif "GROUP BY" in q and "CONTRACTOR" in q.replace("_", ""):
        result = [
            {"contractor_name": "Softex Sp. z o.o.", "total": 45000.00, "invoice_count": 6},
            {"contractor_name": "CloudNet S.A.", "total": 32000.00, "invoice_count": 4},
            {"contractor_name": "DataVault Sp.J.", "total": 28500.00, "invoice_count": 3},
        ]
    else:
        result = [
            {"id": 1, "contractor_name": "Softex Sp. z o.o.", "net_amount": 7500.00, "invoice_type": "expense", "is_paid": True},
            {"id": 2, "contractor_name": "CloudNet S.A.", "net_amount": 12000.00, "invoice_type": "income", "is_paid": False},
            {"id": 3, "contractor_name": "DataVault Sp.J.", "net_amount": 9500.00, "invoice_type": "expense", "is_paid": True},
        ]

    result_json = json.dumps(result, ensure_ascii=False)
    logger.info(f"[AFC_TOOL_RESULT] <<< execute_sql_query: {result_json}")
    return result_json


async def search_corporate_memory(query: str) -> str:
    """
    Przeszukuje pamięć korporacyjną firmy (CompanyContext).
    Parametr 'query' opisuje czego szukasz — kontekst firmy zostanie zwrócony jako JSON.
    Zawiera: profil firmy, sytuację finansową, unit economics, ryzyka, cele strategiczne.
    """
    tool_call_counter["search_corporate_memory"] += 1
    logger.info(f"\n{'='*60}\n[AFC_TOOL_EXEC] >>> search_corporate_memory\n  query: {query}\n{'='*60}")

    mock_context = {
        "company_profile": {
            "legal_form": "Sp. z o.o.",
            "industry": "SaaS / B2B",
            "taxation_form": "CIT",
            "employee_count": 12,
        },
        "financial_pulse": {
            "cash_on_hand": 285000.0,
            "mrr": 45000.0,
            "net_burn_rate": -12000.0,
            "credit_line": 100000.0,
        },
        "unit_economics": {
            "ltv": 36000.0,
            "cac": 4500.0,
            "gross_margin_pct": 72.0,
            "churn_pct": 3.2,
        },
        "risk_flags": [
            {"description": "Koncentracja przychodów: 40% od jednego klienta", "severity": "high"},
            {"description": "Brak rezerwy na zobowiązania podatkowe Q2", "severity": "medium"},
        ],
        "strategic_compass": {
            "primary_goal": "Wzrost MRR do 100k w 6 miesięcy",
            "strategy": "Ekspansja na rynek enterprise + upsell istniejących klientów",
        },
    }

    result_json = json.dumps(mock_context, ensure_ascii=False)
    logger.info(f"[AFC_TOOL_RESULT] <<< search_corporate_memory: {result_json[:300]}...")
    return result_json


# ── TEST RUNNER ──────────────────────────────────────────────────────────────
async def run_test(prompt: str, tools: list, label: str):
    """Run a single test case and verify AFC fires."""
    print(f"\n{'#'*70}")
    print(f"# TEST: {label}")
    print(f"# Prompt: {prompt}")
    print(f"{'#'*70}\n")

    # Reset counters
    for k in tool_call_counter:
        tool_call_counter[k] = 0

    client = genai.Client(api_key="AIzaSyB_N6rAyHWLcqfV2zqPvs79GR6pnlroDI8")

    db_context = (
        f"\n\nKONTEKST BAZY DANYCH:\n"
        f"- company_id tej firmy: '{MOCK_COMPANY_ID}'\n"
        f"- KAŻDE zapytanie SQL MUSI zawierać: WHERE company_id = '{MOCK_COMPANY_ID}'\n"
        f"- Tabela invoices: id, company_id, invoice_type ('expense'/'income'), "
        f"contractor_name, net_amount, gross_amount, issue_date, is_paid.\n"
        f"- Tabela departments: id, company_id, name, monthly_limit.\n"
    )

    chat = client.aio.chats.create(
        model="gemini-2.5-flash",
        config=types.GenerateContentConfig(
            system_instruction=(
                "Jesteś ekspertem finansowym. MUSISZ użyć dostępnych narzędzi (execute_sql_query, search_corporate_memory) "
                "by odpowiedzieć na pytanie. NIE ZGADUJ — korzystaj WYŁĄCZNIE z danych zwróconych przez narzędzia."
                + db_context
            ),
            temperature=0.0,
            tools=tools,
            automatic_function_calling=AFC_CONFIG,
        )
    )

    response = await chat.send_message(prompt)

    # ── Verify AFC history ──
    afc_history = getattr(response, "automatic_function_calling_history", None)
    confirmed_calls = []
    if afc_history:
        for content in afc_history:
            if not content.parts:
                continue
            for part in content.parts:
                fc = getattr(part, "function_call", None)
                if fc:
                    confirmed_calls.append(fc.name)

    print(f"\n{'-'*70}")
    print(f"[AFC_CONFIRMED] Tools fired: {confirmed_calls or 'NONE!'}")
    print(f"[CALL_COUNTER] execute_sql_query={tool_call_counter['execute_sql_query']}, "
          f"search_corporate_memory={tool_call_counter['search_corporate_memory']}")

    if confirmed_calls:
        print(f"[STATUS] PASS AFC WORKS -- {len(confirmed_calls)} tool call(s) confirmed")
    else:
        print(f"[STATUS] FAIL AFC FAILED -- model did NOT call any tools (hallucination risk!)")

    print(f"\n[FINAL_ANSWER]\n{response.text[:1000]}")
    print(f"{'-'*70}\n")

    return len(confirmed_calls) > 0


async def main():
    api_key = "AIzaSyB_N6rAyHWLcqfV2zqPvs79GR6pnlroDI8"
    if not api_key:
        print("ERROR: Set GEMINI_API_KEY environment variable first!")
        print("  Windows:  set GEMINI_API_KEY=your_key_here")
        print("  Bash:     export GEMINI_API_KEY=your_key_here")
        return

    print("=" * 70)
    print("AUDITRA — AFC (Automatic Function Calling) Verification Test")
    print(f"SDK: google-genai, Model: gemini-2.5-flash")
    print(f"AFC Config: disable=False, max_remote_calls=10")
    print("=" * 70)

    results = []

    # Test 1: SQL tool only
    ok = await run_test(
        prompt="Ile faktur ma firma? Podaj liczbę.",
        tools=[execute_sql_query],
        label="SQL Tool - COUNT query",
    )
    results.append(("SQL COUNT", ok))

    # Test 2: SQL tool — aggregation
    ok = await run_test(
        prompt="Jaka jest suma wydatków (expense) firmy? Podaj kwotę w PLN.",
        tools=[execute_sql_query],
        label="SQL Tool - SUM EXPENSES",
    )
    results.append(("SQL SUM", ok))

    # Test 3: Both tools
    ok = await run_test(
        prompt="Jaka jest sytuacja finansowa firmy? Sprawdź dane z bazy i pamięć korporacyjną.",
        tools=[execute_sql_query, search_corporate_memory],
        label="SQL + Memory - dual tool usage",
    )
    results.append(("SQL+Memory", ok))

    # ── Summary ──
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for name, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}]  {name}")

    all_passed = all(r[1] for r in results)
    print(f"\n{'ALL TESTS PASSED' if all_passed else 'SOME TESTS FAILED'}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
