"""
smoke_test_hallucination_fixes.py — weryfikuje Tier 1 fixy anty-halucynacyjne.

Bez DB, bez Gemini. Czysto strukturalne:
1. Czy agent_tools.execute_sql_query zwraca truncation metadata.
2. Czy MAJOR_CFO_PROMPT ma CHART_CONDITIONS_MET / SQL TRUNCATION / GROUP-BY COVERAGE.
3. Czy sub-agenci (Pesymist/Optymist/Aktuariusz) mają "NIE HALUCYNUJ" i nie mają confirmation bias.

Wynik: wysyłka raportu HTML do podanego adresu przez Resend.
"""
import os
import sys
import json
import asyncio
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

RECIPIENT = "ziemowit.milosz56@outlook.com"


def check(name: str, passed: bool, detail: str = "") -> dict:
    return {"name": name, "passed": passed, "detail": detail}


def run_static_checks() -> list[dict]:
    results = []

    # ─── agent_tools.py ───
    agent_tools_path = ROOT / "app" / "services" / "agent_tools.py"
    agent_tools_src = agent_tools_path.read_text(encoding="utf-8")

    results.append(check(
        "agent_tools: 'truncated' field in SQL output",
        '"truncated":' in agent_tools_src or '"truncated"' in agent_tools_src,
        "Sprawdzam czy execute_sql_query dodaje pole truncated do payloadu.",
    ))
    results.append(check(
        "agent_tools: 'applied_limit' signalling",
        "applied_limit" in agent_tools_src,
        "Pole informujące model o nałożonym LIMIT (50 lub user-supplied).",
    ))
    results.append(check(
        "agent_tools: 'warning' gdy truncated=true",
        "NIE licz SUM/AVG/TOP/COUNT na tej próbce" in agent_tools_src,
        "Ostrzeżenie dla modelu żeby nie liczył agregatów na ucitych danych.",
    ))
    results.append(check(
        "agent_tools: get_category_coverage istnieje",
        "async def get_category_coverage" in agent_tools_src
        and "coverage_pct" in agent_tools_src
        and "can_show_structure" in agent_tools_src,
        "Deterministyczny coverage check dla struktury kosztów.",
    ))

    # ─── agentic_cfo.py prompts ───
    agentic_path = ROOT / "app" / "services" / "agentic_cfo.py"
    src = agentic_path.read_text(encoding="utf-8")

    results.append(check(
        "MAJOR_CFO_PROMPT: CHART_CONDITIONS_MET zamiast 'OBOWIĄZKOWY'",
        "CHART_CONDITIONS_MET" in src and "KIEDY CHART JEST OBOWIĄZKOWY" not in src,
        "Stary enforcement wyrzucony, nowe conditions_met() zastępuje.",
    ))
    results.append(check(
        "MAJOR_CFO_PROMPT: SQL TRUNCATION AWARENESS",
        "SQL TRUNCATION AWARENESS" in src and "truncated=true" in src,
        "Model uczony czytać pole truncated z tool output.",
    ))
    results.append(check(
        "MAJOR_CFO_PROMPT: GROUP-BY COVERAGE rule",
        "GROUP-BY COVERAGE" in src and "coverage < 50%" in src,
        "Zakaz wykresu per kategoria gdy coverage <50%.",
    ))
    results.append(check(
        "MAJOR_CFO_PROMPT: ZAKAZ CHARTU (data_insufficient)",
        "ZAKAZ CHARTU (data_insufficient)" in src,
        "Chart zablokowany gdy 0/1-2 punkty lub >50% null.",
    ))

    # ─── Sub-agenci ───
    # AGENT_PESSIMIST
    pess_start = src.find("AGENT_PESSIMIST = ")
    pess_end = src.find('"""', pess_start + 30)
    pess_block = src[pess_start:pess_end + 3] if pess_start > 0 else ""
    results.append(check(
        "AGENT_PESSIMIST: NIE HALUCYNUJ",
        "NIE HALUCYNUJ" in pess_block,
    ))
    results.append(check(
        "AGENT_PESSIMIST: reakcja na truncated=true",
        "truncated=true" in pess_block,
    ))

    # AGENT_OPTIMIST
    opt_start = src.find("AGENT_OPTIMIST = ")
    opt_end = src.find('"""', opt_start + 30)
    opt_block = src[opt_start:opt_end + 3] if opt_start > 0 else ""
    results.append(check(
        "AGENT_OPTIMIST: NIE HALUCYNUJ",
        "NIE HALUCYNUJ" in opt_block,
    ))
    results.append(check(
        "AGENT_OPTIMIST: confirmation bias usunięty",
        "udowodnić, że inwestycja się opłaci" not in opt_block
        and "ZBADAĆ czy inwestycja może się opłacić" in opt_block,
        "Było: 'udowodnić'. Teraz: 'ZBADAĆ czy może się opłacić'.",
    ))

    # AGENT_ACTUARY
    act_start = src.find("AGENT_ACTUARY = ")
    act_end = src.find('"""', act_start + 30)
    act_block = src[act_start:act_end + 3] if act_start > 0 else ""
    results.append(check(
        "AGENT_ACTUARY: NIE HALUCYNUJ",
        "NIE HALUCYNUJ" in act_block,
    ))
    results.append(check(
        "AGENT_ACTUARY: <3 msc = low confidence",
        "confidence: low" in act_block or "< 3 miesięcy" in act_block,
    ))

    # ─── Schemat już był (Tier 1d zamknięty) ───
    schema_path = ROOT / "app" / "schemas_financial.py"
    schema_src = schema_path.read_text(encoding="utf-8")
    results.append(check(
        "ExpenseMetrics.by_department: Optional (schema)",
        "by_department: Optional[List[DepartmentExpense]] = None" in schema_src,
    ))
    results.append(check(
        "ExpenseMetrics.department_coverage_pct (schema)",
        "department_coverage_pct: float" in schema_src,
    ))

    return results


def build_html_report(results: list[dict], started_at: str) -> str:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed
    status_color = "#10b981" if failed == 0 else "#ef4444"
    status_label = "ALL GREEN" if failed == 0 else f"{failed} FAILED"

    rows_html = ""
    for r in results:
        mark = "✓" if r["passed"] else "✗"
        color = "#10b981" if r["passed"] else "#ef4444"
        detail = f'<div style="color:#6b7280;font-size:12px;margin-top:4px;">{r["detail"]}</div>' if r["detail"] else ""
        rows_html += f"""
        <tr>
            <td style="padding:10px 12px;border-bottom:1px solid #f3f4f6;vertical-align:top;width:28px;">
                <span style="color:{color};font-weight:700;font-size:16px;">{mark}</span>
            </td>
            <td style="padding:10px 12px;border-bottom:1px solid #f3f4f6;font-size:14px;color:#111827;">
                <div style="font-weight:500;">{r["name"]}</div>
                {detail}
            </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<div style="max-width:640px;margin:0 auto;padding:32px 16px;">
    <div style="background:#ffffff;border-radius:12px;padding:28px;border:1px solid #e5e7eb;">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;">
            <div style="font-size:20px;font-weight:700;color:#111827;">Auditra — Smoke Test</div>
            <div style="background:{status_color};color:#ffffff;padding:4px 12px;border-radius:6px;font-size:12px;font-weight:600;">{status_label}</div>
        </div>
        <div style="color:#6b7280;font-size:13px;margin-bottom:16px;">
            Fixy anty-halucynacyjne Tier 1 — {started_at}
        </div>
        <div style="background:#f9fafb;border-radius:8px;padding:14px 16px;margin-bottom:20px;font-size:13px;color:#374151;">
            <strong style="color:#111827;">{passed}/{total}</strong> checków przeszło.
            {"Wszystkie fixy są w kodzie i gotowe do testów funkcjonalnych." if failed == 0 else f"<strong style='color:#ef4444;'>{failed} failed</strong> — sprawdź szczegóły poniżej."}
        </div>
        <table style="width:100%;border-collapse:collapse;">{rows_html}</table>
        <div style="margin-top:24px;padding-top:16px;border-top:1px solid #f3f4f6;font-size:12px;color:#9ca3af;">
            Repo: d:/cost-effective/auditra · Fix: MAJOR_CFO_PROMPT + sub-agenci + execute_sql_query truncation metadata · Doc ClickUp: 2kyr0j49-1115
        </div>
    </div>
</div>
</body></html>"""


def send_email(html: str, subject: str, to_email: str) -> tuple[bool, str]:
    import resend
    api_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("RESEND_FROM_EMAIL", "cfo@auditra.pl")
    if not api_key:
        return False, "RESEND_API_KEY not set in .env"
    resend.api_key = api_key
    try:
        result = resend.Emails.send({
            "from": f"Auditra CFO <{from_email}>",
            "to": [to_email],
            "subject": subject,
            "html": html,
        })
        return True, f"id={result.get('id', '?')}"
    except Exception as e:
        return False, str(e)


def main():
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    results = run_static_checks()

    # Print to stdout
    passed = sum(1 for r in results if r["passed"])
    failed = len(results) - passed
    print(f"\n=== SMOKE TEST: Tier 1 Hallucination Fixes ===")
    print(f"Started: {started_at}\n")
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"  [{mark}] {r['name']}")
        if r["detail"]:
            print(f"         {r['detail']}")
    print(f"\nSummary: {passed}/{len(results)} passed, {failed} failed.\n")

    # Send email
    html = build_html_report(results, started_at)
    subject = f"Auditra Smoke Test — {passed}/{len(results)} pass · {started_at}"
    ok, info = send_email(html, subject, RECIPIENT)
    if ok:
        print(f"[EMAIL] Sent to {RECIPIENT} ({info})")
    else:
        print(f"[EMAIL] FAILED: {info}")

    return 0 if failed == 0 and ok else 1


if __name__ == "__main__":
    sys.exit(main())
