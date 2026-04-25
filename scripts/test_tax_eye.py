"""
Test Tax Eye: audytuj fakture i sprawdz czy generuje ProactiveInsight z legal_basis + ISAP URL.

cd auditra
python -m scripts.test_tax_eye
"""
import asyncio
import json
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

CID = "4a00cc9b-0aac-4d55-8873-06696b7188b3"


async def run():
    from app.database import AsyncSessionLocal
    from app.models import Invoice, ProactiveInsight
    from app.services.audit_service import AuditService
    from app.services.token_tracker import get_session_stats
    from sqlalchemy import select
    import uuid

    async with AsyncSessionLocal() as db:
        # Find an unaudited expense invoice
        result = await db.execute(
            select(Invoice)
            .where(Invoice.company_id == uuid.UUID(CID), Invoice.invoice_type == "expense")
            .order_by(Invoice.gross_amount.desc())
            .limit(1)
        )
        invoice = result.scalar_one_or_none()
        if not invoice:
            print("Brak faktur do audytu!")
            return

        print(f"=== AUDITING INVOICE ===")
        print(f"  ID: {invoice.id}")
        print(f"  Contractor: {invoice.contractor_name}")
        print(f"  Amount: {invoice.gross_amount} PLN ({invoice.category_name})")
        print(f"  Status: {invoice.status}, risk_score: {invoice.risk_score}")
        print()

        # Run audit
        print("[..] Running AuditService...")
        try:
            audited = await AuditService.audit_single_invoice(invoice, db)
            print(f"[OK] Audit complete!")
            print(f"  New risk_score: {audited.risk_score}")
            print(f"  Status: {audited.status}")

            if audited.ai_analysis_json:
                aj = audited.ai_analysis_json
                legal = aj.get("legal_basis", {})
                print(f"\n=== LEGAL BASIS ===")
                if isinstance(legal, dict):
                    print(f"  Act: {legal.get('act', 'N/A')}")
                    print(f"  Article: {legal.get('article', 'N/A')}")
                    for src in legal.get("sources", []):
                        print(f"  Source: {src.get('id', '?')} {src.get('title', 'N/A')}")
                        print(f"    URL: {src.get('url', 'BRAK')}")
                else:
                    print(f"  Raw: {str(legal)[:300]}")

                rec = aj.get("recommendation", "")
                if rec:
                    print(f"\n=== RECOMMENDATION ===")
                    print(f"  {rec[:300]}")

        except Exception as e:
            print(f"[ERROR] Audit failed: {e}")
            import traceback
            traceback.print_exc()
            return

        # Check if Tax Eye insight was created
        print(f"\n=== TAX EYE INSIGHTS ===")
        insights_result = await db.execute(
            select(ProactiveInsight)
            .where(
                ProactiveInsight.company_id == uuid.UUID(CID),
                ProactiveInsight.insight_type == "tax_eye",
            )
            .order_by(ProactiveInsight.created_at.desc())
            .limit(5)
        )
        insights = insights_result.scalars().all()
        if not insights:
            print("  Brak tax_eye insightow (risk za niski lub brak recommendation)")
        else:
            for ins in insights:
                print(f"\n  [{ins.urgency.upper()}] {ins.title}")
                print(f"    Savings: {ins.estimated_savings or 0:,.0f} PLN")
                print(f"    Legal: {(ins.legal_basis or 'N/A')[:200]}")
                print(f"    Invoice: {ins.source_invoice_id}")

        # Token stats
        stats = get_session_stats()
        print(f"\n=== TOKEN COSTS ===")
        print(f"  Input: {stats['total_input_tokens']:,} tokens")
        print(f"  Output: {stats['total_output_tokens']:,} tokens")
        print(f"  Cost: ${stats['total_cost_usd']:.4f} ({stats['total_cost_pln']:.2f} PLN)")


if __name__ == "__main__":
    asyncio.run(run())
