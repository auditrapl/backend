#!/usr/bin/env python3
"""
CFO AI Test Suite v2 — Weryfikacja 8 problemów
Bezpośrednie wywołanie agentic_cfo_stream() (bez HTTP/serwera).
Zwraca CAŁY output — zero filtrowania.

Uruchomienie: python test_cfo_v2.py
Wyniki w: test_outputs/cfo_v2_full_output_[timestamp].json
"""
import asyncio
import json
import os
import sys
import time
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

from app.schemas import CfoRequest
from app.services.agentic_cfo import agentic_cfo_stream

# ─────────────────────────────────────────────
# KONFIGURACJA
# ─────────────────────────────────────────────
COMPANY_ID = "4a00cc9b-0aac-4d55-8873-06696b7188b3"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "test_outputs")


# ─────────────────────────────────────────────
# STRUKTURY DANYCH
# ─────────────────────────────────────────────
@dataclass
class RawResult:
    test_id: str
    problem_id: str
    prompt: str
    duration_sec: float = 0.0
    route: Optional[str] = None
    response_type: Optional[str] = None
    tool_calls: int = 0
    tools_used: list = field(default_factory=list)
    blocks_found: list = field(default_factory=list)
    hero_recommendation: Optional[str] = None
    executive_summary: Optional[str] = None
    message: Optional[str] = None
    blocks_full: list = field(default_factory=list)
    raw_report: dict = field(default_factory=dict)
    raw_meta: dict = field(default_factory=dict)
    sse_errors: list = field(default_factory=list)


# ─────────────────────────────────────────────
# EKSTRAKCJA PEŁNEJ TREŚCI BLOKÓW
# ─────────────────────────────────────────────
def extract_block_full(block: dict) -> dict:
    bt = block.get("block_type", "unknown")
    result = {"block_type": bt, "raw": block}

    if bt == "text_card":
        result["title"] = block.get("title", "")
        result["body"] = block.get("body", "")
        result["theme"] = block.get("theme", "")
    elif bt == "metric_grid":
        result["metrics"] = block.get("metrics", [])
    elif bt == "alert_banner":
        result["message"] = block.get("message", "")
        result["urgency"] = block.get("urgency", "")
    elif bt == "data_table":
        result["title"] = block.get("title", "")
        result["columns"] = block.get("columns", [])
        result["rows"] = block.get("rows", [])
    elif bt == "chart":
        result["title"] = block.get("title", "")
        result["chart_type"] = block.get("chart_type", "")
        result["series_labels"] = block.get("series_labels", [])
        result["data"] = block.get("data", [])
        result["data_points_count"] = len(block.get("data", []))
    elif bt == "timeline":
        result["steps"] = block.get("steps", [])
    elif bt == "scoring":
        result["score"] = block.get("score")
        result["label"] = block.get("label", "")
    elif bt == "parsed_data_list":
        result["items"] = block.get("items", [])

    return result


# ─────────────────────────────────────────────
# BEZPOŚREDNI RUNNER (bez HTTP)
# ─────────────────────────────────────────────
async def run_test(test_id: str, problem_id: str, prompt: str) -> RawResult:
    result = RawResult(test_id=test_id, problem_id=problem_id, prompt=prompt)

    t_start = time.time()
    report = {}
    meta = {}

    try:
        req = CfoRequest(prompt=prompt)
        async for chunk in agentic_cfo_stream(req, COMPANY_ID):
            try:
                data = json.loads(chunk)
                status = data.get("status", "")

                if status == "progress":
                    if data.get("type") == "phase_end" and data.get("phase") == "triage":
                        result.route = data.get("result")
                elif status == "success":
                    report = data.get("report", {})
                    meta = data.get("meta", {})
                elif status == "error":
                    result.sse_errors.append(data)
            except json.JSONDecodeError:
                pass
    except Exception as e:
        result.sse_errors.append({"error": str(e)})

    result.duration_sec = round(time.time() - t_start, 2)
    result.route = result.route or (meta or {}).get("route")
    result.response_type = report.get("response_type")
    result.tool_calls = (meta or {}).get("total_tool_calls", 0)
    result.tools_used = (meta or {}).get("tools_used", [])

    layout = report.get("layout", [])
    result.blocks_found = [b.get("block_type", "?") for b in layout]
    result.blocks_full = [extract_block_full(b) for b in layout]

    result.hero_recommendation = report.get("hero_recommendation")
    result.executive_summary = report.get("executive_summary")
    result.message = report.get("message")
    result.raw_report = report
    result.raw_meta = meta

    return result


# ─────────────────────────────────────────────
# TESTY
# ─────────────────────────────────────────────
async def run_all_tests() -> list[RawResult]:
    results = []

    # ── P1: Zduplikowane bloki ──
    print("\n[P1] Zduplikowane bloki")
    r = await run_test("P1-A", "P1", "Jaka jest nasza sytuacja finansowa?")
    results.append(r)
    print(f"  P1-A: {r.blocks_found} ({r.duration_sec}s)")

    r = await run_test("P1-B", "P1", "Oceń kondycję finansową firmy i czy mamy jakieś ryzyko.")
    results.append(r)
    print(f"  P1-B: {r.blocks_found} ({r.duration_sec}s)")

    # ── P2: Chart na instant ──
    print("\n[P2] Chart zbędny na instant")
    r = await run_test("P2-A", "P2", "Który kontrahent kosztuje nas najwięcej?")
    results.append(r)
    print(f"  P2-A: {r.blocks_found} ({r.duration_sec}s) route={r.route}")

    r = await run_test("P2-B", "P2", "Ile zapłaciliśmy AWS w marcu?")
    results.append(r)
    print(f"  P2-B: {r.blocks_found} ({r.duration_sec}s) route={r.route}")

    # ── P3: Brak rekomendacji ──
    print("\n[P3] Brak rekomendacji po zaległościach")
    r = await run_test("P3-A", "P3", "Które faktury są nieopłacone?")
    results.append(r)
    print(f"  P3-A: {r.blocks_found} ({r.duration_sec}s)")

    r = await run_test("P3-B", "P3", "Kto mi zalega i od jak dawna?")
    results.append(r)
    print(f"  P3-B: {r.blocks_found} ({r.duration_sec}s)")

    # ── P4: Prędkość instant (3x benchmark) ──
    print("\n[P4] Prędkość instant (benchmark x3)")
    for i in range(1, 4):
        r = await run_test(f"P4-SPD{i}", "P4", "Ile mam teraz na koncie?")
        results.append(r)
        print(f"  P4-SPD{i}: {r.duration_sec}s route={r.route}")

    # ── P5: Tool calls w debate ──
    print("\n[P5] Tool calls w strategic_debate")
    r = await run_test("P5-A", "P5", "Czy możemy otworzyć nowe biuro za 25 000 PLN miesięcznie?")
    results.append(r)
    print(f"  P5-A: tool_calls={r.tool_calls} duration={r.duration_sec}s route={r.route}")

    r = await run_test("P5-B", "P5", "Porównaj scenariusz zatrudnienia 1 vs 3 nowych osób po 10 000 PLN każda.")
    results.append(r)
    print(f"  P5-B: tool_calls={r.tool_calls} duration={r.duration_sec}s route={r.route}")

    # ── P6: Chat zamiast dashboard ──
    print("\n[P6] Chat zamiast dashboard dla liczb")
    r = await run_test("P6-A", "P6", "Podaj mi burn rate.")
    results.append(r)
    print(f"  P6-A: type={r.response_type} blocks={r.blocks_found}")

    r = await run_test("P6-B", "P6", "Jaki jest MRR?")
    results.append(r)
    print(f"  P6-B: type={r.response_type} blocks={r.blocks_found}")

    # ── P7: Brak chart dla trendów ──
    print("\n[P7] Brak chart dla pytań o trend")
    r = await run_test("P7-A", "P7", "Jak zmieniało się DSO w ostatnich 3 miesiącach?")
    results.append(r)
    print(f"  P7-A: {r.blocks_found} route={r.route}")

    r = await run_test("P7-B", "P7", "Pokaż mi trend przeterminowanych należności.")
    results.append(r)
    print(f"  P7-B: {r.blocks_found} route={r.route}")

    # ── P8: Brak data_table w inwestycjach ──
    print("\n[P8] Brak data_table w scenariuszach inwestycyjnych")
    r = await run_test("P8-A", "P8", "Mamy ofertę agencji za 8 000 PLN miesięcznie przez 12 miesięcy. Pokaż mi scenariusze.")
    results.append(r)
    print(f"  P8-A: {r.blocks_found} route={r.route}")

    r = await run_test("P8-B", "P8", "Porównaj koszt zatrudnienia vs outsourcingu developera na 15 000 PLN miesięcznie.")
    results.append(r)
    print(f"  P8-B: {r.blocks_found} route={r.route}")

    return results


# ─────────────────────────────────────────────
# ZAPIS
# ─────────────────────────────────────────────
def build_full_output(results: list[RawResult]) -> dict:
    by_problem = {}
    for r in results:
        by_problem.setdefault(r.problem_id, []).append(r)

    problems_summary = {}
    for pid, tests in by_problem.items():
        problems_summary[pid] = {
            "tests": [t.test_id for t in tests],
            "routes": [t.route for t in tests],
            "durations_sec": [t.duration_sec for t in tests],
            "tool_calls": [t.tool_calls for t in tests],
            "blocks_found": [t.blocks_found for t in tests],
            "response_types": [t.response_type for t in tests],
        }

    return {
        "generated_at": datetime.now().isoformat(),
        "company_id": COMPANY_ID,
        "total_tests": len(results),
        "problems_summary": problems_summary,
        "full_results": [
            {
                "test_id": r.test_id,
                "problem_id": r.problem_id,
                "prompt": r.prompt,
                "duration_sec": r.duration_sec,
                "route": r.route,
                "response_type": r.response_type,
                "tool_calls": r.tool_calls,
                "tools_used": r.tools_used,
                "blocks_found": r.blocks_found,
                "blocks_count": len(r.blocks_found),
                "duplicate_blocks": [
                    bt for bt in set(r.blocks_found)
                    if r.blocks_found.count(bt) > 1
                    and bt not in ["alert_banner"]
                ],
                "hero_recommendation": r.hero_recommendation,
                "executive_summary": r.executive_summary,
                "message": r.message,
                "blocks_full": r.blocks_full,
                "raw_report": r.raw_report,
                "raw_meta": r.raw_meta,
                "sse_errors": r.sse_errors,
                "flags": {
                    "has_duplicate_blocks": any(
                        r.blocks_found.count(bt) > 1
                        for bt in set(r.blocks_found)
                        if bt not in ["alert_banner"]
                    ),
                    "has_chart": "chart" in r.blocks_found,
                    "has_text_card": "text_card" in r.blocks_found,
                    "has_data_table": "data_table" in r.blocks_found,
                    "has_timeline": "timeline" in r.blocks_found,
                    "has_metric_grid": "metric_grid" in r.blocks_found,
                    "has_alert_banner": "alert_banner" in r.blocks_found,
                    "is_dashboard": r.response_type == "dashboard",
                    "is_chat": r.response_type == "chat",
                    "slow_instant": r.duration_sec > 12 and r.route == "instant",
                    "too_many_tools": r.tool_calls > 12,
                }
            }
            for r in results
        ]
    }


async def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("=" * 60)
    print("CFO AI Test Suite v2 — Weryfikacja 8 problemów")
    print(f"Company: {COMPANY_ID}")
    print("=" * 60)

    results = await run_all_tests()
    output = build_full_output(results)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = os.path.join(OUTPUT_DIR, f"cfo_v2_full_output_{timestamp}.json")

    with open(filename, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print("\n" + "=" * 60)
    print("PODSUMOWANIE:")
    print(f"  Testy: {output['total_tests']}")
    print(f"  Plik:  {filename}")
    print()
    for pid, summary in output["problems_summary"].items():
        durations = summary["durations_sec"]
        print(f"  [{pid}]  routes={summary['routes']}  "
              f"durations={[f'{d}s' for d in durations]}  "
              f"blocks={summary['blocks_found']}")

    print()
    print("Wklej plik z wynikami z powrotem do Claude do analizy.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
