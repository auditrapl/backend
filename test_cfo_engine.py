"""
CFO Engine Test Runner — testy logiki agentic_cfo bez UI.
Wywoluje agentic_cfo_stream() bezposrednio, zbiera chunki SSE,
waliduje route/response_type/wartosci, zapisuje outputy do .txt

Uzycie: python test_cfo_engine.py
Output: test_outputs/cfo_test_results.txt + cfo_test_results.json
"""
import asyncio
import json
import os
import sys
import re
import time
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv()

from app.schemas import CfoRequest
from app.services.agentic_cfo import agentic_cfo_stream

# ── KONFIGURACJA ──
COMPANY_ID = "4a00cc9b-0aac-4d55-8873-06696b7188b3"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "test_outputs")


@dataclass
class TestScenario:
    id: str
    name: str
    prompt: str
    group: str  # A, B, C, D, NEG
    expected_route: Optional[str] = None  # instant, analysis, strategic_debate
    expected_response_type: Optional[str] = None  # chat, dashboard
    must_contain_blocks: list = field(default_factory=list)  # block_type names
    must_not_contain_blocks: list = field(default_factory=list)
    must_contain_text: list = field(default_factory=list)  # substrings in any text
    must_not_contain_text: list = field(default_factory=list)
    value_checks: dict = field(default_factory=dict)  # {"SafeCash": (120000, 140000)}
    max_words: Optional[int] = None


@dataclass
class TestResult:
    id: str
    name: str
    group: str
    status: str = "PENDING"  # PASS, FAIL, ERROR
    route: Optional[str] = None
    response_type: Optional[str] = None
    duration_sec: float = 0.0
    tool_calls: int = 0
    blocks_found: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    raw_report: dict = field(default_factory=dict)
    all_text: str = ""


# ══════════════════════════════════════════════════════════════════════
# SCENARIUSZE TESTOWE
# ══════════════════════════════════════════════════════════════════════

SCENARIOS = [
    # ── GRUPA A: Proste pytania o liczby (instant) ──
    TestScenario(
        id="T-001", name="Ile mam kasy (SafeCash test)",
        prompt="Ile mam gotowki na koncie?",
        group="A",
        expected_route="instant",
        value_checks={"safecash_range": (100000, 200000)},  # SafeCash ~130k, NIE 840k
    ),
    TestScenario(
        id="T-002", name="Ile faktur w systemie",
        prompt="Ile faktur mam w systemie?",
        group="A",
        expected_route="instant",
        must_contain_text=["43", "faktur"],
    ),
    TestScenario(
        id="T-003", name="Kto jest najwiekszym klientem",
        prompt="Kto jest moim najwiekszym klientem pod wzgledem przychodow?",
        group="A",
        expected_route="instant",
        must_contain_text=["FinBank"],
    ),
    TestScenario(
        id="T-004", name="Ile zaleglosci",
        prompt="Ile mam niezaplaconych faktur kosztowych?",
        group="A",
        expected_route="instant",
    ),
    TestScenario(
        id="T-005", name="Najwyzszy koszt kontrahent",
        prompt="Ktory kontrahent generuje mi najwyzsze koszty?",
        group="A",
        expected_route="instant",
    ),

    # ── GRUPA B: Listy i tabele (data_table) ──
    TestScenario(
        id="T-006", name="Lista kontrahentow z przychodami",
        prompt="Pokaz mi liste wszystkich kontrahentow przychodowych z kwotami.",
        group="B",
        must_contain_blocks=["data_table"],
        must_contain_text=["FinBank"],
    ),
    TestScenario(
        id="T-007", name="Niezaplacone faktury",
        prompt="Pokaz tabele niezaplaconych faktur kosztowych posortowana od najwyzszej kwoty.",
        group="B",
        must_contain_blocks=["data_table"],
    ),
    TestScenario(
        id="T-008", name="Struktura kosztow",
        prompt="Jaka jest struktura moich kosztow? Pokaz rozklad po kontrahentach.",
        group="B",
        must_contain_blocks=["data_table"],
    ),

    # ── GRUPA C: Analiza (analysis, chart) ──
    TestScenario(
        id="T-011", name="Analiza cashflow",
        prompt="Przeanalizuj moj cashflow za ostatnie 3 miesiace. Jak wyglada trend?",
        group="C",
        expected_route="analysis",
        expected_response_type="dashboard",
    ),
    TestScenario(
        id="T-012", name="Ryzyko koncentracji klientow",
        prompt="Czy mam ryzyko koncentracji klientow? Jaki procent przychodow pochodzi od jednego kontrahenta?",
        group="C",
        expected_route="analysis",
        must_contain_text=["FinBank"],
    ),
    TestScenario(
        id="T-013", name="Analiza naleznosci",
        prompt="Przeanalizuj moje naleznosci. Ile jest przeterminowanych i jaki to wplyw na plynnosc?",
        group="C",
        expected_route="analysis",
        expected_response_type="dashboard",
    ),

    # ── GRUPA D: Strategic debate ──
    TestScenario(
        id="T-016", name="Zatrudnienie handlowca",
        prompt="Czy stac mnie na zatrudnienie nowego handlowca za 12 000 PLN brutto miesiecznie? Przeanalizuj wplyw na cashflow.",
        group="D",
        expected_route="strategic_debate",
        expected_response_type="dashboard",
    ),
    TestScenario(
        id="T-017", name="Inwestycja w marketing",
        prompt="Chce zainwestowac 50 000 PLN w kampanie marketingowa. Czy to sie oplaci przy moich obecnych przychodach?",
        group="D",
        expected_route="strategic_debate",
        expected_response_type="dashboard",
    ),

    # ── TESTY NEGATYWNE ──
    # NEG-001: Usuniety z listy SCENARIOS — testowany osobno jako NEG-001b (implicit learning bramka).
    # AI ma prawo uzyc 5M w odpowiedzi na symulacje, ale NIE powinno zapisac do L2 kontekstu.
    # Walidacja przeniesiona do run_implicit_learning_test().
    TestScenario(
        id="NEG-002", name="Brak timeline bez pytania o plan",
        prompt="Jaka jest moja marza brutto?",
        group="NEG",
        must_not_contain_blocks=["timeline"],
    ),
    TestScenario(
        id="NEG-003", name="Brak dramatycznego tonu",
        prompt="Jak wyglada moja sytuacja finansowa?",
        group="NEG",
        must_not_contain_text=[
            "Iluzja bezpiecze", "Tykaj", "bomba", "katastrofa",
            "dramatyczn", "niestety", "potezn",
        ],
    ),
]


# ══════════════════════════════════════════════════════════════════════
# TEST RUNNER
# ══════════════════════════════════════════════════════════════════════

def extract_all_text(report: dict) -> str:
    """Wyciaga caly tekst z raportu (message, executive_summary, layout blocks)."""
    parts = []
    if report.get("message"):
        parts.append(report["message"])
    if report.get("executive_summary"):
        parts.append(report["executive_summary"])
    if report.get("hero_recommendation"):
        parts.append(report["hero_recommendation"])
    for block in report.get("layout", []):
        for text_field in ["content", "body", "title", "subtitle", "description"]:
            if block.get(text_field):
                parts.append(str(block[text_field]))
        # metric_grid items
        for item in (block.get("items") or []):
            if isinstance(item, dict):
                for k, v in item.items():
                    parts.append(str(v))
        # data_table rows
        for row in (block.get("rows") or []):
            if isinstance(row, dict):
                for v in row.values():
                    parts.append(str(v))
        # data_table columns/data
        for col in (block.get("columns") or []):
            parts.append(str(col))
        for data_row in (block.get("data") or []):
            if isinstance(data_row, dict):
                for v in data_row.values():
                    parts.append(str(v))
            elif isinstance(data_row, list):
                for v in data_row:
                    parts.append(str(v))
    return "\n".join(parts)


def extract_numbers(text: str) -> list[float]:
    """Wyciaga liczby z tekstu (PLN, %, etc.)."""
    # Match numbers like 130 472, 130472, 130,472, 130472.00
    raw = re.findall(r'[\d\s,]+[\d](?:\.\d+)?', text)
    numbers = []
    for r in raw:
        clean = r.replace(" ", "").replace(",", "")
        try:
            numbers.append(float(clean))
        except ValueError:
            pass
    return numbers


async def run_single_test(scenario: TestScenario) -> TestResult:
    """Uruchamia pojedynczy scenariusz testowy."""
    result = TestResult(
        id=scenario.id,
        name=scenario.name,
        group=scenario.group,
    )

    start = time.time()
    chunks = []

    try:
        req = CfoRequest(prompt=scenario.prompt)
        async for chunk in agentic_cfo_stream(req, COMPANY_ID):
            chunks.append(chunk)
    except Exception as e:
        result.status = "ERROR"
        result.errors.append(f"Stream error: {e}")
        result.duration_sec = time.time() - start
        return result

    result.duration_sec = time.time() - start

    # Parse chunks
    route = None
    report = None
    meta = None

    for raw_chunk in chunks:
        try:
            data = json.loads(raw_chunk)
        except json.JSONDecodeError:
            continue

        # Extract route from triage phase_end
        if data.get("type") == "phase_end" and data.get("phase") == "triage":
            route = data.get("result")

        # Extract final report
        if data.get("status") == "success":
            report = data.get("report", {})
            meta = data.get("meta", {})

        if data.get("status") == "error":
            result.errors.append(data.get("message", "Unknown error"))

    if not report:
        result.status = "ERROR"
        result.errors.append("Brak chunka 'success' w odpowiedzi")
        return result

    result.route = route or (meta or {}).get("route")
    result.response_type = report.get("response_type")
    result.tool_calls = (meta or {}).get("total_tool_calls", 0)
    result.raw_report = report

    # Extract blocks
    for block in report.get("layout", []):
        bt = block.get("block_type")
        if bt:
            result.blocks_found.append(bt)

    all_text = extract_all_text(report)
    result.all_text = all_text

    # ── WALIDACJA ──
    errors = []

    # Route check
    if scenario.expected_route and result.route != scenario.expected_route:
        errors.append(f"Route: oczekiwano '{scenario.expected_route}', dostano '{result.route}'")

    # Response type check
    if scenario.expected_response_type and result.response_type != scenario.expected_response_type:
        errors.append(f"Response type: oczekiwano '{scenario.expected_response_type}', dostano '{result.response_type}'")

    # Must contain blocks
    for block_type in scenario.must_contain_blocks:
        if block_type not in result.blocks_found:
            errors.append(f"Brak wymaganego bloku: '{block_type}' (znaleziono: {result.blocks_found})")

    # Must NOT contain blocks
    for block_type in scenario.must_not_contain_blocks:
        if block_type in result.blocks_found:
            errors.append(f"Niedozwolony blok obecny: '{block_type}'")

    # Must contain text
    all_text_lower = all_text.lower()
    for text_fragment in scenario.must_contain_text:
        if text_fragment.lower() not in all_text_lower:
            errors.append(f"Brak wymaganego tekstu: '{text_fragment}'")

    # Must NOT contain text
    for text_fragment in scenario.must_not_contain_text:
        if text_fragment.lower() in all_text_lower:
            errors.append(f"Niedozwolony tekst obecny: '{text_fragment}'")

    # Value checks
    if "safecash_range" in scenario.value_checks:
        lo, hi = scenario.value_checks["safecash_range"]
        numbers = extract_numbers(all_text)
        # Szukamy liczby w zakresie SafeCash
        found_in_range = any(lo <= n <= hi for n in numbers)
        found_840k = any(800000 <= n <= 900000 for n in numbers)
        if found_840k and not found_in_range:
            errors.append(
                f"BUG KASY: AI odpowiedzial 840k (z kontekstu) zamiast SafeCash ({lo}-{hi} PLN). "
                f"Liczby w odpowiedzi: {[n for n in numbers if n > 10000]}"
            )
        elif not found_in_range and not found_840k:
            errors.append(
                f"SafeCash nie znaleziony w zakresie {lo}-{hi}. "
                f"Liczby > 10k: {[n for n in numbers if n > 10000]}"
            )

    # Max words check
    if scenario.max_words:
        word_count = len(all_text.split())
        if word_count > scenario.max_words:
            errors.append(f"Za duzo slow: {word_count} (max {scenario.max_words})")

    result.errors = errors
    result.status = "PASS" if not errors else "FAIL"
    return result


async def run_implicit_learning_test() -> TestResult:
    """
    Test NEG-001: Sprawdza czy symulacja NIE trafia do kontekstu L2.
    1. Sprawdz aktualny simulation_cash
    2. Wyslij prompt z hipoteza "zalozmy ze mamy 5 000 000 PLN"
    3. Poczekaj az background task sie wykona
    4. Sprawdz czy simulation_cash NIE zmienil sie na 5 000 000
    """
    result = TestResult(
        id="NEG-001b",
        name="Implicit Learning bramka — symulacja nie zapisuje do L2",
        group="NEG",
    )

    start = time.time()

    try:
        import uuid
        from app.database import AsyncSessionLocal
        from app.services.context_operational import get_or_create_operational

        cid = uuid.UUID(COMPANY_ID)

        # 1. Stan przed
        async with AsyncSessionLocal() as db:
            before = await get_or_create_operational(db, cid)
            sim_cash_before = before.live_financial_pulse.simulation_cash

        # 2. Odpal prompt z hipoteza
        req = CfoRequest(prompt="Zalozmy ze mamy 5 000 000 PLN gotowki. Jaki bylby nasz runway?")
        async for _ in agentic_cfo_stream(req, COMPANY_ID):
            pass

        # 3. Daj background task czas
        await asyncio.sleep(3)

        # 4. Sprawdz po
        async with AsyncSessionLocal() as db:
            after = await get_or_create_operational(db, cid)
            sim_cash_after = after.live_financial_pulse.simulation_cash

        result.duration_sec = time.time() - start

        if sim_cash_after and sim_cash_after >= 4_000_000:
            result.status = "FAIL"
            result.errors.append(
                f"IMPLICIT LEARNING BUG: simulation_cash zmienil sie na {sim_cash_after} "
                f"(bylo: {sim_cash_before}). Hipoteza '5M PLN' zapisala sie do kontekstu!"
            )
        else:
            result.status = "PASS"
            result.all_text = f"simulation_cash before={sim_cash_before}, after={sim_cash_after}"

    except Exception as e:
        result.status = "ERROR"
        result.errors.append(f"Error: {e}")
        result.duration_sec = time.time() - start

    return result


async def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    txt_path = os.path.join(OUTPUT_DIR, f"cfo_test_results_{timestamp}.txt")
    json_path = os.path.join(OUTPUT_DIR, f"cfo_test_results_{timestamp}.json")

    print("=" * 70)
    print(f"  CFO ENGINE TEST RUNNER")
    print(f"  Company: Automic ({COMPANY_ID})")
    print(f"  Scenarios: {len(SCENARIOS)} + 1 implicit learning test")
    print(f"  Started: {datetime.now().isoformat()}")
    print("=" * 70)

    results: list[TestResult] = []
    txt_lines = []

    txt_lines.append("=" * 70)
    txt_lines.append(f"CFO ENGINE TEST RESULTS — {datetime.now().isoformat()}")
    txt_lines.append(f"Company: Automic ({COMPANY_ID})")
    txt_lines.append("=" * 70)

    # ── Uruchom scenariusze ──
    for i, scenario in enumerate(SCENARIOS, 1):
        print(f"\n[{i}/{len(SCENARIOS)}] {scenario.id}: {scenario.name}")
        print(f"  Prompt: {scenario.prompt[:80]}...")

        result = await run_single_test(scenario)
        results.append(result)

        status_icon = "PASS" if result.status == "PASS" else "FAIL" if result.status == "FAIL" else "ERR!"
        print(f"  => [{status_icon}] route={result.route} type={result.response_type} "
              f"tools={result.tool_calls} time={result.duration_sec:.1f}s")
        if result.errors:
            for err in result.errors:
                print(f"     ! {err}")

        # Zapisz do txt
        txt_lines.append("")
        txt_lines.append("-" * 70)
        txt_lines.append(f"[{scenario.id}] {scenario.name} — {result.status}")
        txt_lines.append(f"Prompt: {scenario.prompt}")
        txt_lines.append(f"Route: {result.route} | Response type: {result.response_type} | "
                        f"Tool calls: {result.tool_calls} | Duration: {result.duration_sec:.1f}s")
        txt_lines.append(f"Blocks: {result.blocks_found}")
        if result.errors:
            txt_lines.append(f"ERRORS:")
            for err in result.errors:
                txt_lines.append(f"  - {err}")
        txt_lines.append(f"--- FULL TEXT OUTPUT ---")
        txt_lines.append(result.all_text[:3000] if result.all_text else "(no text)")
        txt_lines.append(f"--- RAW REPORT JSON ---")
        txt_lines.append(json.dumps(result.raw_report, ensure_ascii=False, indent=2, default=str)[:5000])

    # ── Implicit Learning test ──
    print(f"\n[SPECIAL] NEG-001b: Implicit Learning bramka")
    il_result = await run_implicit_learning_test()
    results.append(il_result)

    status_icon = "PASS" if il_result.status == "PASS" else "FAIL"
    print(f"  => [{status_icon}] {il_result.all_text}")
    if il_result.errors:
        for err in il_result.errors:
            print(f"     ! {err}")

    txt_lines.append("")
    txt_lines.append("-" * 70)
    txt_lines.append(f"[NEG-001b] Implicit Learning bramka — {il_result.status}")
    txt_lines.append(il_result.all_text)
    if il_result.errors:
        for err in il_result.errors:
            txt_lines.append(f"  - {err}")

    # ── PODSUMOWANIE ──
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    errored = sum(1 for r in results if r.status == "ERROR")
    total = len(results)
    total_time = sum(r.duration_sec for r in results)

    summary = [
        "",
        "=" * 70,
        "PODSUMOWANIE",
        "=" * 70,
        f"  PASS:  {passed}/{total}",
        f"  FAIL:  {failed}/{total}",
        f"  ERROR: {errored}/{total}",
        f"  Total time: {total_time:.1f}s",
        "",
    ]

    if failed > 0 or errored > 0:
        summary.append("FAILED/ERROR TESTS:")
        for r in results:
            if r.status != "PASS":
                summary.append(f"  [{r.id}] {r.name}: {r.status}")
                for err in r.errors:
                    summary.append(f"    - {err}")

    for line in summary:
        print(line)
        txt_lines.append(line)

    # ── Zapisz pliki ──
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(txt_lines))

    json_results = []
    for r in results:
        d = {
            "id": r.id, "name": r.name, "group": r.group,
            "status": r.status, "route": r.route,
            "response_type": r.response_type,
            "duration_sec": round(r.duration_sec, 2),
            "tool_calls": r.tool_calls,
            "blocks_found": r.blocks_found,
            "errors": r.errors,
        }
        json_results.append(d)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "company_id": COMPANY_ID,
            "summary": {"passed": passed, "failed": failed, "errored": errored, "total": total},
            "results": json_results,
        }, f, ensure_ascii=False, indent=2)

    print(f"\nOutputs saved:")
    print(f"  TXT: {txt_path}")
    print(f"  JSON: {json_path}")

    sys.exit(0 if failed == 0 and errored == 0 else 1)


if __name__ == "__main__":
    # Suppress SQLAlchemy echo
    import logging
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    asyncio.run(main())
