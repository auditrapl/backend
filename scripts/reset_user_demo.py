"""
Reset danych firmy dla usera ziemowit.milosz56@outlook.com
Zachowuje usera, czysci firme, wrzuca demo dane TechFlow.

cd auditra
python -m scripts.reset_user_demo
"""
import asyncio
import uuid
import sys
import io
from datetime import datetime, timedelta
from decimal import Decimal

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

CID = "4a00cc9b-0aac-4d55-8873-06696b7188b3"
UID = "33a23087-67b4-434a-b0a4-40009814f948"


async def main():
    from app.database import AsyncSessionLocal
    from app.models import Invoice, CompanyContextModel, Department
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        # ── 1. CLEANUP — single transaction, FK-safe order ──
        # Delete test company first (frees NIP 5272958441)
        test_cid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeee01"
        await db.execute(text(
            "DELETE FROM audit_logs WHERE invoice_id IN "
            "(SELECT id FROM invoices WHERE company_id IN (:c1, :c2))"
        ), {"c1": CID, "c2": test_cid})
        await db.execute(text(
            "DELETE FROM messages WHERE conversation_id IN "
            "(SELECT id FROM conversations WHERE company_id IN (:c1, :c2))"
        ), {"c1": CID, "c2": test_cid})

        for table in ["invoices", "conversations", "company_contexts", "departments",
                       "documents", "company_documents", "context_snapshots", "proactive_insights"]:
            await db.execute(text(
                f"DELETE FROM {table} WHERE company_id IN (:c1, :c2)"
            ), {"c1": CID, "c2": test_cid})

        # Delete test company record itself
        await db.execute(text("DELETE FROM users WHERE company_id = :c"), {"c": test_cid})
        await db.execute(text("DELETE FROM companies WHERE id = :c"), {"c": test_cid})

        await db.commit()
        print("[OK] Wyczyszczono dane firmy")

        # ── 2. UPDATE COMPANY ──
        await db.execute(text("""
            UPDATE companies SET
                name = :name, nip = :nip, industry = :ind, description = :desc,
                legal_form = :legal, taxation_form = :tax, vat_status = :vat,
                employee_count = :emp, zus_type = :zus
            WHERE id = :cid
        """), {
            "cid": CID,
            "name": "TechFlow Sp. z o.o.",
            "nip": "5272958441",
            "ind": "IT/Software",
            "desc": "B2B SaaS - platforma analityczna dla e-commerce. "
                    "Prezes fakturuje przez JDG (B2B 12k/msc). Zespol R&D: 7 osob.",
            "legal": "Sp. z o.o.",
            "tax": "CIT 9%",
            "vat": "Czynny podatnik VAT",
            "emp": 9,
            "zus": "pelny",
        })
        print("[OK] Firma -> TechFlow Sp. z o.o.")

        # ── 3. DEPARTMENTS ──
        cid_uuid = uuid.UUID(CID)
        db.add_all([
            Department(id=uuid.uuid4(), company_id=cid_uuid, name="R&D", monthly_limit=90000),
            Department(id=uuid.uuid4(), company_id=cid_uuid, name="Operations", monthly_limit=30000),
            Department(id=uuid.uuid4(), company_id=cid_uuid, name="Marketing", monthly_limit=25000),
        ])

        # ── 4. L1 + L2 CONTEXT ──
        canonical_data = {
            "company_identity": {
                "name": "TechFlow Sp. z o.o.", "nip": "5272958441",
                "legal_form": "Sp. z o.o.", "vat_status": "Czynny podatnik VAT",
                "accounting_method": "Pelna ksiegowosc",
            },
            "tax_regime": {
                "taxation_form": "CIT 9%", "vat_rate": "23%", "jpk_obligation": True,
                "cit_rate": 9.0, "zus_type": "pelny", "employee_count": 9,
                "vat_frequency": "monthly", "special_obligations": [],
            },
            "business_model": {
                "segment": "B2B", "model_type": "SaaS",
                "revenue_model": "Subskrypcja miesieczna", "industry": "IT/Software",
            },
            "strategic_compass": {
                "primary_goal": "Skalowanie ARR do 3M PLN w 2026",
                "strategy": "Rozwoj produktu + ekspansja DACH. Prezes fakturuje firme przez JDG (B2B 12k/msc netto).",
            },
            "unit_economics": {
                "ltv": 48000.0, "cac": 5200.0,
                "gross_margin_pct": 74.0, "churn_monthly_pct": 1.8,
            },
            "seasonality_and_patterns": [
                "Q4 = ~35% rocznych przychodow",
                "Zespol R&D: 5 B2B + 2 UoP = 7 osob dedykowanych rozwojowi SaaS",
            ],
            "canonical_version": 1,
        }

        operational_data = {
            "pulse": {
                "safecash": 485000.0, "safecash_updated_at": "2026-04-12T10:00:00",
                "simulation_cash": None, "cash_on_hand": None,
                "credit_line": 200000.0, "mrr": 210000.0, "net_burn_rate": -118000.0,
                "receivables_total": 65000.0, "payables_total": 28000.0,
                "receivables_overdue_30d": 0.0,
            },
            "invoice_metrics": {
                "monthly_revenue": 222000.0, "monthly_expenses": 118700.0,
                "monthly_net_cashflow": 103300.0, "prev_month_revenue": 198000.0,
                "revenue_mom_growth_pct": 12.1,
                "top_contractor_name": "DataBridge SA", "top_contractor_pct": 24.8,
                "top3_concentration_pct": 55.4,
                "auto_burn_rate": 115000.0, "auto_mrr": 206000.0,
                "invoice_count": 61, "metrics_updated_at": "2026-04-12T10:00:00",
            },
            "tax_position": {
                "vat_to_pay": 21500.0, "cit_estimated": 8300.0,
                "next_tax_deadline": "2026-04-25", "jpk_status": "Wyslany za marzec",
            },
            "risk_flags": [
                {"description": "Prognoza roczna ~2.5M PLN - zweryfikowac prog malego podatnika", "severity": "high"},
                {"description": "Prezes fakturuje firme przez JDG (B2B 12k/msc) - ryzyko ukrytego zysku przy CIT estonskim", "severity": "medium"},
            ],
            "period_goals": [
                {"description": "Pozyskac 3 nowych klientow Enterprise do konca Q2"},
                {"description": "Zoptymalizowac strukture podatkowa"},
                {"description": "Rozwazyc IP Box dla dochodow z SaaS (R&D team 7 osob)"},
            ],
        }

        db.add(CompanyContextModel(
            id=uuid.uuid4(), company_id=cid_uuid, context_data={},
            canonical_data=canonical_data, canonical_version=1,
            canonical_updated_at=datetime(2026, 4, 1),
            operational_data=operational_data, operational_version=1,
            operational_updated_at=datetime(2026, 4, 12),
        ))

        # ── 5. INVOICES (sty-kwi 2026) ──
        uid_uuid = uuid.UUID(UID)
        invoices = []

        def _inv(itype, contractor, nip, net, issue_date, category, is_paid=True, due_days=14):
            vat = round(net * Decimal("0.23"), 2)
            gross = net + vat
            iss = datetime.strptime(issue_date, "%Y-%m-%d")
            due = iss + timedelta(days=due_days)
            pay_date = due - timedelta(days=2) if is_paid else None
            invoices.append(Invoice(
                id=uuid.uuid4(), company_id=cid_uuid, user_id=uid_uuid,
                filename=f"FV-{len(invoices)+1:03d}-2026.pdf",
                invoice_type=itype, contractor_name=contractor, contractor_nip=nip,
                net_amount=float(net), vat_amount=float(vat), gross_amount=float(gross),
                issue_date=iss, due_date=due, is_paid=is_paid,
                payment_date=pay_date, payment_days=due_days - 2 if is_paid else None,
                status="verified", category_name=category, risk_score=0,
            ))

        # INCOME — 7 klientow, rosnacy MRR
        income_data = [
            ("01", [("DataBridge SA", "5261234567", 52000), ("FinVault Sp. z o.o.", "7831234567", 32000),
                    ("CloudOps GmbH", "DE812345678", 38000), ("MedTech Solutions", "6181234567", 20000),
                    ("Retail24 SA", "5421234567", 24000), ("LogiTrans Sp. z o.o.", "1181234567", 18000)]),
            ("02", [("DataBridge SA", "5261234567", 52000), ("FinVault Sp. z o.o.", "7831234567", 32000),
                    ("CloudOps GmbH", "DE812345678", 42000), ("MedTech Solutions", "6181234567", 20000),
                    ("Retail24 SA", "5421234567", 24000), ("LogiTrans Sp. z o.o.", "1181234567", 18000),
                    ("NovaPharma Sp. z o.o.", "1121234567", 16000)]),
            ("03", [("DataBridge SA", "5261234567", 55000), ("FinVault Sp. z o.o.", "7831234567", 35000),
                    ("CloudOps GmbH", "DE812345678", 42000), ("MedTech Solutions", "6181234567", 22000),
                    ("Retail24 SA", "5421234567", 24000), ("LogiTrans Sp. z o.o.", "1181234567", 18000),
                    ("NovaPharma Sp. z o.o.", "1121234567", 22000)]),
            ("04", [("DataBridge SA", "5261234567", 55000), ("FinVault Sp. z o.o.", "7831234567", 35000),
                    ("CloudOps GmbH", "DE812345678", 45000), ("MedTech Solutions", "6181234567", 22000),
                    ("Retail24 SA", "5421234567", 28000), ("LogiTrans Sp. z o.o.", "1181234567", 18000),
                    ("NovaPharma Sp. z o.o.", "1121234567", 25000)]),
        ]
        for month, rev_list in income_data:
            for contractor, nip, amount in rev_list:
                _inv("income", contractor, nip, Decimal(str(amount)), f"2026-{month}-05", "SaaS License")

        # EXPENSES — monthly recurring
        for month in ["01", "02", "03", "04"]:
            _inv("expense", "Amazon Web Services", "IE9692928F", Decimal("14000"), f"2026-{month}-01", "Infrastruktura")
            _inv("expense", "B2B R&D Team (5 osob)", "5213456789", Decimal("60000"), f"2026-{month}-01", "R&D Wynagrodzenia B2B")
            _inv("expense", "R&D UoP (2 osoby)", "WEWNETRZNE", Decimal("18000"), f"2026-{month}-01", "R&D Wynagrodzenia UoP")
            _inv("expense", "Jan Kowalski JDG (Prezes)", "1234567890", Decimal("12000"), f"2026-{month}-01", "Wynagrodzenie Prezesa B2B")
            _inv("expense", "WeWork Warszawa", "5252678901", Decimal("9500"), f"2026-{month}-05", "Czynsz biuro")
            _inv("expense", "Google Workspace + Tools", "IE6388047V", Decimal("2200"), f"2026-{month}-01", "Narzedzia")
            _inv("expense", "Google Gemini API", "IE6388047V", Decimal("4500"), f"2026-{month}-01", "AI/API")

        # Jednorazowe
        _inv("expense", "Kancelaria Podatkowa Nowak", "5271234567", Decimal("5500"), "2026-01-15", "Ksiegowosc")
        _inv("expense", "Google Ads", "IE6388047V", Decimal("18000"), "2026-02-20", "Marketing")
        _inv("expense", "Google Ads", "IE6388047V", Decimal("22000"), "2026-03-20", "Marketing")
        _inv("expense", "Kancelaria Podatkowa Nowak", "5271234567", Decimal("5500"), "2026-04-15", "Ksiegowosc")
        _inv("expense", "Dell Technologies", "5262345678", Decimal("12000"), "2026-04-18", "Sprzet", is_paid=False, due_days=30)
        _inv("expense", "Konferencja SaaStr EU", "DE998877665", Decimal("8500"), "2026-03-25", "Marketing")

        db.add_all(invoices)
        await db.commit()

        income_cnt = sum(1 for i in invoices if i.invoice_type == "income")
        expense_cnt = sum(1 for i in invoices if i.invoice_type == "expense")
        print(f"[OK] Faktury: {len(invoices)} ({income_cnt} income + {expense_cnt} expense)")

        # ── 6. REFRESH METRICS ──
        print("[..] Przeliczam SafeCash i metryki...")
        from app.services.context_operational import refresh_financial_metrics
        metrics = await refresh_financial_metrics(db, cid_uuid)
        print(f"[OK] SafeCash: {metrics.get('safecash', '?')} PLN")
        print()
        print("Gotowe! ziemowit.milosz56@outlook.com -> TechFlow Sp. z o.o.")
        print("61 faktur, 7 klientow, MRR ~210k, CIT 9%, R&D team 7 osob")


if __name__ == "__main__":
    asyncio.run(main())
