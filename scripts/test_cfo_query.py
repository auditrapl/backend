"""
Test script: wywoluje AI CFO z pytaniem o CIT estonski.
Zapisuje pelny przebieg do JSON.

Uzycie:
  cd auditra
  python -m scripts.test_cfo_query
  python -m scripts.test_cfo_query "Ile mam gotowki?"
"""
import asyncio
import json
import sys
import os
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from scripts.seed_test_company import COMPANY_ID

OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "..", "test_output.json")


async def run_query(prompt: str):
    from app.schemas import CfoRequest
    from app.services.agentic_cfo import agentic_cfo_stream

    request = CfoRequest(prompt=prompt, document_ids=[])
    start = datetime.utcnow()

    full_log = {
        "query": prompt,
        "company_id": str(COMPANY_ID),
        "timestamp": start.isoformat(),
        "events": [],
        "final_report": None,
        "meta": None,
        "error": None,
        "elapsed_seconds": None,
    }

    print(f"\n=== AUDITRA CFO AI — Sandbox Test ===")
    print(f"Pytanie: {prompt}")
    print(f"Company: {COMPANY_ID}")
    print()

    async for chunk_str in agentic_cfo_stream(request, str(COMPANY_ID)):
        try:
            chunk = json.loads(chunk_str)
        except json.JSONDecodeError:
            full_log["events"].append({"raw": chunk_str[:500]})
            continue

        full_log["events"].append(chunk)
        status = chunk.get("status")

        if status == "progress":
            ptype = chunk.get("type", "")
            phase = chunk.get("phase", "")
            if ptype == "phase_start":
                print(f"  > [{phase}] {chunk.get('label', '')}")
            elif ptype == "phase_end":
                route = chunk.get("result", "")
                detail = chunk.get("detail", "")
                if route:
                    print(f"  OK [{phase}] route={route} cat={chunk.get('category','')} -- {detail}")
                else:
                    print(f"  OK [{phase}] {detail}")
            elif chunk.get("type") == "tool_call":
                print(f"    tool: {chunk.get('tool','?')}  args: {chunk.get('args','')[:80]}")
            elif chunk.get("type") == "tool_result":
                print(f"    result: {chunk.get('tool','?')} -> {chunk.get('preview','')[:80]}")
            elif chunk.get("type") == "agent_start":
                print(f"    agent {chunk.get('agent','?')} start")
            elif chunk.get("type") == "agent_done":
                print(f"    agent {chunk.get('agent','?')} done -- {chunk.get('detail','')}")
            elif ptype == "phase_update":
                print(f"  ~ [{phase}] {chunk.get('detail','')}")

        elif status == "success":
            full_log["final_report"] = chunk.get("report")
            full_log["meta"] = chunk.get("meta")
            print(f"\n  === SUCCESS ===")

        elif status == "error":
            full_log["error"] = chunk.get("message")
            print(f"\n  !!! ERROR: {chunk.get('message','?')}")

        elif status == "suggestions":
            full_log["suggestions"] = chunk.get("questions", [])

    elapsed = (datetime.utcnow() - start).total_seconds()
    full_log["elapsed_seconds"] = round(elapsed, 1)

    # Save to JSON file
    out_path = os.path.abspath(OUTPUT_FILE)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(full_log, f, indent=2, ensure_ascii=False, default=str)

    print(f"\n  Czas: {elapsed:.1f}s")
    print(f"  Events: {len(full_log['events'])}")
    if full_log["meta"]:
        print(f"  Route: {full_log['meta'].get('route','?')}")
        print(f"  Tool calls: {full_log['meta'].get('total_tool_calls',0)}")
    print(f"\n  JSON saved: {out_path}")
    print()


if __name__ == "__main__":
    prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else (
        "Czy oplaca mi sie przejsc na CIT estonski? "
        "Nie wyplacamy dywidend, reinwestujemy wszystko w rozwoj produktu."
    )
    asyncio.run(run_query(prompt))
