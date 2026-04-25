"""
smoke_test_functional.py — prawdziwy smoke test fixów anty-halucynacyjnych.

Wywołuje agentic_cfo_stream() na realnej firmie (Gemini + DB).
Analizuje outputy pod kątem red-flag patterns: Google Ads bar chart,
fake donut oddziałów (przy niskim coverage), sprzeczne znaki delt,
Optymista wymyśla "kredyt potem skalować".

Porównanie vs baseline: test_outputs/cfo_v2_full_output_20260411_175834.json

Usage: python smoke_test_functional.py
"""
import asyncio
import json
import os
import sys
import time
import logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

from app.schemas import CfoRequest
from app.services.agentic_cfo import agentic_cfo_stream

COMPANY_ID = "4a00cc9b-0aac-4d55-8873-06696b7188b3"
RECIPIENT = "ziemowit.milosz56@outlook.com"
BASELINE_FILE = ROOT / "test_outputs" / "cfo_v2_full_output_20260411_175834.json"

# Trzy pytania — dwa z baseline (P5-A/B) + jedno świeże (tempting J-Curve + Google Ads)
TEST_PROMPTS = [
    ("P5-A", "Czy możemy otworzyć nowe biuro za 25 000 PLN miesięcznie?"),
    ("P5-B", "Porównaj scenariusz zatrudnienia 1 vs 3 nowych osób po 10 000 PLN każda."),
    ("NEW-ADS", "Czy warto wydać 30 000 PLN miesięcznie na reklamy Google Ads? Pokaż scenariusze."),
    ("NEW-STRUCT", "Pokaż mi strukturę kosztów per oddział."),
    ("P5-C", "Co jest lepsze: plan A (zatrudnienie handlowca 8k/msc) czy plan B (outsourcing sprzedaży 15k/msc)?"),
]


async def run(test_id: str, prompt: str) -> dict:
    t0 = time.time()
    report, meta, errs = {}, {}, []
    route = None
    try:
        req = CfoRequest(prompt=prompt)
        async for chunk in agentic_cfo_stream(req, COMPANY_ID):
            try:
                data = json.loads(chunk)
                s = data.get("status")
                if s == "progress" and data.get("type") == "phase_end" and data.get("phase") == "triage":
                    route = data.get("result")
                elif s == "success":
                    report = data.get("report", {})
                    meta = data.get("meta", {})
                elif s == "error":
                    errs.append(data)
            except json.JSONDecodeError:
                pass
    except Exception as e:
        errs.append({"error": str(e)})

    layout = report.get("layout", [])
    return {
        "test_id": test_id,
        "prompt": prompt,
        "duration_sec": round(time.time() - t0, 2),
        "route": route or meta.get("route"),
        "response_type": report.get("response_type"),
        "tool_calls": meta.get("total_tool_calls", 0),
        "tools_used": meta.get("tools_used", []),
        "blocks": [b.get("block_type") for b in layout],
        "layout": layout,
        "hero_recommendation": report.get("hero_recommendation"),
        "executive_summary": report.get("executive_summary"),
        "message": report.get("message"),
        "errors": errs,
    }


def detect_red_flags(r: dict) -> list[dict]:
    """Pattern-matching hallucination red flags."""
    flags = []
    layout = r.get("layout", [])
    blocks = r.get("blocks", [])
    text_blob = json.dumps(r, ensure_ascii=False).lower()
    hero = (r.get("hero_recommendation") or "").lower()
    exec_sum = (r.get("executive_summary") or "").lower()
    full_text = hero + " " + exec_sum + " " + text_blob

    # Red flag 1: Optymista fabricates loan/scale-up at cash-negative company
    if any(kw in full_text for kw in ["można wziąć kredyt", "weź kredyt", "zacznij skalować", "skaluj kampanię", "zwiększ bud"]):
        if any(kw in full_text for kw in ["niewypłacalno", "brak płynno", "-85 968", "luka gotówk", "zaległ"]):
            flags.append({"severity": "HIGH", "type": "Optymista loan hallucination",
                          "detail": "Optymista rekomenduje kredyt/skalowanie na firmie z luką płynności."})

    # Red flag 2: Chart with <3 data points (nieuzasadniona wizualizacja)
    for b in layout:
        if b.get("block_type") == "chart":
            pts = len(b.get("data") or [])
            ctype = b.get("chart_type", "")
            if ctype in ("bar_vertical", "bar_horizontal", "donut", "pie", "line", "area") and pts < 3:
                flags.append({
                    "severity": "HIGH",
                    "type": f"Chart {ctype} z {pts} punktami",
                    "detail": f"Chart_conditions_met wymaga ≥3 wartości. Tu: {pts}.",
                })

    # Red flag 3: Donut/pie struktury kosztów
    # (Struktura per oddział) — nie wykryjemy coverage tu, ale flagujemy jeśli donut bez alertu o coverage
    has_structure_chart = any(
        b.get("block_type") == "chart"
        and b.get("chart_type") in ("donut", "pie")
        and any(kw in (b.get("title") or "").lower() for kw in ["oddzia", "struktur", "depart", "kategor"])
        for b in layout
    )
    has_coverage_alert = any(
        b.get("block_type") == "alert_banner"
        and any(kw in (b.get("message") or "").lower() for kw in ["przypisz", "coverage", "oddzia"])
        for b in layout
    )
    if has_structure_chart and not has_coverage_alert:
        flags.append({"severity": "MEDIUM", "type": "Donut/pie struktury bez coverage check",
                      "detail": "Struktura kosztów donut/pie, ale brak alertu o coverage — możliwy fake donut."})

    # Red flag 4: Sprzeczne znaki delt w data_table
    for b in layout:
        if b.get("block_type") == "data_table":
            rows = b.get("rows", [])
            cols = b.get("columns", [])
            # Heuristic: szukamy wiersza gdzie delta/zmiana jest positive ale bazowa < porównywana (lub odwrotnie)
            for row in rows:
                if isinstance(row, dict):
                    values = [str(v) for v in row.values()]
                    # bardzo prosty heurystyk: jeśli jest '+' i 'spadek' w tym samym wierszu
                    if any("+" in v and "-" in v for v in values):
                        flags.append({"severity": "MEDIUM", "type": "Potencjalnie sprzeczne znaki delt",
                                      "detail": f"Wiersz tabeli z mieszanymi +/- znakami: {values[:3]}"})
                        break

    # Red flag 5: Brak "NIE HALUCYNUJ" efektu — tool_calls == 0 w strategic_debate
    if r.get("route") == "strategic_debate" and r.get("tool_calls", 0) == 0:
        flags.append({"severity": "HIGH", "type": "strategic_debate bez tool calls",
                      "detail": "Debata strategiczna bez zapytań do DB = halucynacja z pamięci."})

    # Red flag 6: SQL truncation ignored
    tools_used = r.get("tools_used", [])
    if "execute_sql_query" in tools_used and "truncated" in text_blob:
        # trudne do wykrycia statycznie, pomijamy chyba że jest wyraźnie
        pass

    return flags


def compare_to_baseline(new: dict, baseline: dict | None) -> dict:
    """Zwraca porównanie bloków, hero, charts."""
    if not baseline:
        return {"status": "no_baseline"}
    new_blocks = set(new.get("blocks", []))
    old_blocks = set(baseline.get("blocks_found", []))
    return {
        "status": "ok",
        "old_blocks": sorted(old_blocks),
        "new_blocks": sorted(new_blocks),
        "added": sorted(new_blocks - old_blocks),
        "removed": sorted(old_blocks - new_blocks),
        "old_hero": (baseline.get("hero_recommendation") or "")[:200],
        "new_hero": (new.get("hero_recommendation") or "")[:200],
    }


def load_baseline() -> dict:
    if not BASELINE_FILE.exists():
        return {}
    try:
        with open(BASELINE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {r.get("test_id"): r for r in data.get("full_results", [])}
    except Exception:
        return {}


def build_html(results: list[dict], started_at: str) -> str:
    total = len(results)
    total_flags = sum(len(r.get("red_flags", [])) for r in results)
    high = sum(1 for r in results for f in r.get("red_flags", []) if f["severity"] == "HIGH")
    status_color = "#10b981" if high == 0 else "#ef4444"
    status_label = "NO HIGH FLAGS" if high == 0 else f"{high} HIGH FLAGS"

    sections = ""
    for r in results:
        flags = r.get("red_flags", [])
        cmp = r.get("cmp", {})
        flags_html = "".join(
            f'<li style="color:{"#ef4444" if f["severity"]=="HIGH" else "#f59e0b"};margin:4px 0;">'
            f'<strong>[{f["severity"]}]</strong> {f["type"]}: <span style="color:#6b7280;">{f["detail"]}</span></li>'
            for f in flags
        )
        if not flags_html:
            flags_html = '<li style="color:#10b981;">Brak red-flags.</li>'

        cmp_html = ""
        if cmp.get("status") == "ok":
            added = ", ".join(cmp.get("added", [])) or "—"
            removed = ", ".join(cmp.get("removed", [])) or "—"
            cmp_html = f"""
            <div style="background:#f9fafb;padding:10px 14px;border-radius:6px;margin-top:12px;font-size:12px;">
                <strong>Porównanie vs baseline (2026-04-11):</strong><br>
                Bloki: {cmp['old_blocks']} → {cmp['new_blocks']}<br>
                Dodane: <span style="color:#10b981;">{added}</span> · Usunięte: <span style="color:#ef4444;">{removed}</span><br>
                <br><strong>Old hero:</strong> <em>{cmp['old_hero']}</em><br>
                <strong>New hero:</strong> <em>{cmp['new_hero']}</em>
            </div>"""

        sections += f"""
        <div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;padding:18px;margin-bottom:16px;">
            <div style="font-weight:700;font-size:14px;color:#111827;margin-bottom:6px;">{r['test_id']} — {r.get('route','?')} · {r.get('duration_sec','?')}s · tools={r.get('tool_calls',0)}</div>
            <div style="color:#6b7280;font-size:13px;margin-bottom:10px;font-style:italic;">"{r['prompt']}"</div>
            <div style="font-size:12px;color:#374151;margin-bottom:8px;">Bloki: <code style="background:#f3f4f6;padding:2px 6px;border-radius:4px;">{' + '.join(r.get('blocks', []) or ['—'])}</code></div>
            <div style="font-size:13px;color:#111827;margin-bottom:8px;"><strong>Hero:</strong> {(r.get('hero_recommendation') or 'NONE')[:350]}</div>
            <ul style="margin:8px 0 0;padding-left:20px;font-size:12px;">{flags_html}</ul>
            {cmp_html}
        </div>"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<div style="max-width:720px;margin:0 auto;padding:24px 16px;">
    <div style="background:#ffffff;border-radius:12px;padding:24px;border:1px solid #e5e7eb;margin-bottom:20px;">
        <div style="display:flex;align-items:center;justify-content:space-between;">
            <div style="font-size:20px;font-weight:700;color:#111827;">Auditra — Functional Smoke Test</div>
            <div style="background:{status_color};color:#fff;padding:4px 12px;border-radius:6px;font-size:12px;font-weight:600;">{status_label}</div>
        </div>
        <div style="color:#6b7280;font-size:13px;margin-top:8px;">
            Fixy anty-halucynacyjne Tier 1 — run na realnej firmie · {started_at}
        </div>
        <div style="background:#f9fafb;padding:14px;border-radius:8px;margin-top:14px;font-size:13px;color:#374151;">
            <strong>{total}</strong> testów · <strong>{total_flags}</strong> red flags (<strong style="color:#ef4444;">{high} HIGH</strong>).
            Porównanie vs baseline: <code>cfo_v2_full_output_20260411_175834.json</code>.
        </div>
    </div>
    {sections}
    <div style="font-size:11px;color:#9ca3af;text-align:center;margin-top:16px;">
        Company: {COMPANY_ID} · Model: Gemini 2.5-Flash/Pro · Trasa: agentic_cfo_stream
    </div>
</div></body></html>"""


def send_email(html: str, subject: str) -> tuple[bool, str]:
    import resend
    resend.api_key = os.environ.get("RESEND_API_KEY", "")
    if not resend.api_key:
        return False, "RESEND_API_KEY not set"
    try:
        res = resend.Emails.send({
            "from": f"Auditra CFO <{os.environ.get('RESEND_FROM_EMAIL', 'cfo@auditra.pl')}>",
            "to": [RECIPIENT],
            "subject": subject,
            "html": html,
        })
        return True, f"id={res.get('id', '?')}"
    except Exception as e:
        return False, str(e)


async def main():
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    baseline = load_baseline()
    results = []
    for tid, prompt in TEST_PROMPTS:
        print(f"[RUN] {tid}: {prompt[:60]}...")
        r = await run(tid, prompt)
        r["red_flags"] = detect_red_flags(r)
        r["cmp"] = compare_to_baseline(r, baseline.get(tid))
        results.append(r)
        print(f"  route={r['route']} blocks={r['blocks']} tools={r['tool_calls']} flags={len(r['red_flags'])}")

    # Save full output
    out_path = ROOT / "test_outputs" / f"smoke_functional_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"started_at": started, "results": results}, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[SAVED] {out_path}")

    # Build + send email
    high = sum(1 for r in results for f in r["red_flags"] if f["severity"] == "HIGH")
    html = build_html(results, started)
    subject = f"Auditra Functional Smoke — {len(results)} tests, {high} HIGH flags · {started}"
    ok, info = send_email(html, subject)
    print(f"[EMAIL] {'OK' if ok else 'FAIL'} {info}")
    return 0


if __name__ == "__main__":
    asyncio.run(main())
