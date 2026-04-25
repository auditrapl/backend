"""
Seed: TechFlow Sp. z o.o. — scenariusz "Pulapka progu 2M EUR"
Firma SaaS rosnie szybko (MRR 210k), prognoza roczna ~2.5M PLN.
Prezes fakturuje przez JDG 12k/msc. R&D team 7 osob. CIT 9%.

cd auditra
python -m scripts.seed_test_company
"""
import asyncio
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

# Real user company (ziemowit.milosz56@outlook.com)
COMPANY_ID = uuid.UUID("4a00cc9b-0aac-4d55-8873-06696b7188b3")
USER_ID = uuid.UUID("33a23087-67b4-434a-b0a4-40009814f948")


async def main():
    from app.database import AsyncSessionLocal, engine
    from app.models import Invoice, CompanyContextModel, Department
    from sqlalchemy import text, delete

    async with AsyncSessionLocal() as db:
        cid = str(COMPANY_ID)
        uid = str(USER_ID)
        await db.execute(text("DELETE FROM invoices WHERE company_id = :c"), {"c": cid})
        await db.execute(text("DELETE FROM company_contexts WHERE company_id = :c"), {"c": cid})
        await db.execute(text("DELETE FROM departments WHERE company_id = :c"), {"c": cid})
        await db.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
        await db.execute(text("DELETE FROM companies WHERE id = :c"), {"c": cid})
        await db.commit()

        # ── 1. COMPANY ──
        await db.execute(text("""
            INSERT INTO companies (id, name, nip, industry, description, legal_form, taxation_form, vat_status, employee_count, zus_type, created_at)
            VALUES (:id, :name, :nip, :industry, :desc, :legal, :tax, :vat, :emp, :zus, NOW())
        """), {
            "id": cid, "name": "TechFlow Sp. z o.o.", "nip": "5272958441",
            "industry": "IT/Software", "desc": "B2B SaaS - platforma analityczna dla e-commerce. Prezes Jan Kowalski fakturuje przez JDG (B2B 12k/msc). Zespol R&D: 7 osob (5 B2B + 2 UoP).",
            "legal": "Sp. z o.o.", "tax": "CIT 9%", "vat": "Czynny podatnik VAT",
            "emp": 9, "zus": "pelny",
        })

        # ── 2. USER ──
        await db.execute(text("""
            INSERT INTO users (id, email, first_name, last_name, company_id, created_at)
            VALUES (:id, :email, :fn, :ln, :cid, NOW())
            ON CONFLICT (id) DO UPDATE SET company_id = :cid
        """), {
            "id": uid, "email": "jan.kowalski@techflow.pl",
            "fn": "Jan", "ln": "Kowalski", "cid": cid,
        })

        # ── 3. DEPARTMENTS ──
        dept_rd = Department(id=uuid.uuid4(), company_id=COMPANY_ID, name="R&D", monthly_limit=90000)
        dept_ops = Department(id=uuid.uuid4(), company_id=COMPANY_ID, name="Operations", monthly_limit=30000)
        dept_mkt = Department(id=uuid.uuid4(), company_id=COMPANY_ID, name="Marketing", monthly_limit=25000)
        db.add_all([dept_rd, dept_ops, dept_mkt])

        # ── 4. L1 CANONICAL ──
        canonical_data = {
            "company_identity": {
                "name": "TechFlow Sp. z o.o.",
                "nip": "5272958441",
                "legal_form": "Sp. z o.o.",
                "vat_status": "Czynny podatnik VAT",
                "accounting_method": "Pelna ksiegowosc",
            },
            "tax_regime": {
                "taxation_form": "CIT 9%",
                "vat_rate": "23%",
                "jpk_obligation": True,
                "cit_rate": 9.0,
                "zus_type": "pelny",
                "employee_count": 9,
                "vat_frequency": "monthly",
                "special_obligations": [],
            },
            "business_model": {
                "segment": "B2B",
                "model_type": "SaaS",
                "revenue_model": "Subskrypcja miesieczna",
                "industry": "IT/Software",
            },
            "strategic_compass": {
                "primary_goal": "Skalowanie ARR do 3M PLN w 2026",
                "strategy": "Rozwoj produktu + ekspansja DACH. Prezes fakturuje firme przez JDG (B2B 12k/msc netto).",
            },
            "unit_economics": {
                "ltv": 48000.0,
                "cac": 5200.0,
                "gross_margin_pct": 74.0,
                "churn_monthly_pct": 1.8,
            },
            "seasonality_and_patterns": [
                "Q4 = ~35% rocznych przychodow (budzety klientow)",
                "Zespol R&D: 5 kontraktorow B2B + 2 UoP = 7 osob dedykowanych rozwojowi produktu SaaS",
            ],
            "canonical_version": 1,
        }

        # ── 5. L2 OPERATIONAL ──
        operational_data = {
            "pulse": {
                "safecash": 485000.0,
                "safecash_updated_at": "2026-04-12T10:00:00",
                "simulation_cash": None,
                "cash_on_hand": None,
                "credit_line": 200000.0,
                "mrr": 210000.0,
                "net_burn_rate": -118000.0,
                "receivables_total": 65000.0,
                "payables_total": 28000.0,
                "receivables_overdue_30d": 0.0,
            },
            "invoice_metrics": {
                "monthly_revenue": 222000.0,
                "monthly_expenses": 118700.0,
                "monthly_net_cashflow": 103300.0,
                "prev_month_revenue": 198000.0,
                "revenue_mom_growth_pct": 12.1,
                "top_contractor_name": "DataBridge SA",
                "top_contractor_pct": 24.8,
                "top3_concentration_pct": 55.4,
                "auto_burn_rate": 115000.0,
                "auto_mrr": 206000.0,
                "invoice_count": 52,
                "metrics_updated_at": "2026-04-12T10:00:00",
            },
            "tax_position": {
                "vat_to_pay": 21500.0,
                "cit_estimated": 8300.0,
                "next_tax_deadline": "2026-04-25",
                "jpk_status": "Wyslany za marzec",
            },
            "risk_flags": [
                {"description": "Prognoza roczna ~2.5M PLN - zblizyc sie do progu malego podatnika?", "severity": "high"},
                {"description": "Prezes fakturuje firme przez JDG (B2B 12k/msc) - ryzyko ukrytego zysku przy CIT estonskim", "severity": "medium"},
            ],
            "period_goals": [
                {"description": "Pozyskac 3 nowych klientow Enterprise do konca Q2"},
                {"description": "Zoptymalizowac strukture podatkowa przed przekroczeniem progu 2M EUR"},
                {"description": "Rozwazyc IP Box dla dochodow z SaaS (R&D team 7 osob)"},
            ],
        }

        context = CompanyContextModel(
            id=uuid.uuid4(),
            company_id=COMPANY_ID,
            context_data={},
            canonical_data=canonical_data,
            canonical_version=1,
            canonical_updated_at=datetime(2026, 4, 1),
            operational_data=operational_data,
            operational_version=1,
            operational_updated_at=datetime(2026, 4, 12),
        )
        db.add(context)

        # ── 6. INVOICES (4 msc: sty-kwi 2026) — wyzszy revenue ──
        invoices = []

        def _inv(invoice_type, contractor, nip, net, issue_date, category,
                 is_paid=True, due_days=14):
            vat = round(net * Decimal("0.23"), 2)
            gross = net + vat
            iss = datetime.strptime(issue_date, "%Y-%m-%d")
            due = iss + timedelta(days=due_days)
            pay_date = due - timedelta(days=2) if is_paid else None
            invoices.append(Invoice(
                id=uuid.uuid4(),
                company_id=COMPANY_ID,
                user_id=USER_ID,
                filename=f"FV-{len(invoices)+1:03d}-2026.pdf",
                invoice_type=invoice_type,
                contractor_name=contractor,
                contractor_nip=nip,
                net_amount=float(net),
                vat_amount=float(vat),
                gross_amount=float(gross),
                issue_date=iss,
                due_date=due,
                is_paid=is_paid,
                payment_date=pay_date,
                payment_days=due_days - 2 if is_paid else None,
                status="verified",
                category_name=category,
                risk_score=0,
            ))

        # ── INCOME — 7 klientow, rosnacy MRR ──
        for month, rev_data in [
            ("01", [("DataBridge SA","5261234567",52000), ("FinVault Sp. z o.o.","7831234567",32000),
                    ("CloudOps GmbH","DE812345678",38000), ("MedTech Solutions","6181234567",20000),
                    ("Retail24 SA","5421234567",24000), ("LogiTrans Sp. z o.o.","1181234567",18000)]),
            ("02", [("DataBridge SA","5261234567",52000), ("FinVault Sp. z o.o.","7831234567",32000),
                    ("CloudOps GmbH","DE812345678",42000), ("MedTech Solutions","6181234567",20000),
                    ("Retail24 SA","5421234567",24000), ("LogiTrans Sp. z o.o.","1181234567",18000),
                    ("NovaPharma Sp. z o.o.","1121234567",16000)]),
            ("03", [("DataBridge SA","5261234567",55000), ("FinVault Sp. z o.o.","7831234567",35000),
                    ("CloudOps GmbH","DE812345678",42000), ("MedTech Solutions","6181234567",22000),
                    ("Retail24 SA","5421234567",24000), ("LogiTrans Sp. z o.o.","1181234567",18000),
                    ("NovaPharma Sp. z o.o.","1121234567",22000)]),
            ("04", [("DataBridge SA","5261234567",55000), ("FinVault Sp. z o.o.","7831234567",35000),
                    ("CloudOps GmbH","DE812345678",45000), ("MedTech Solutions","6181234567",22000),
                    ("Retail24 SA","5421234567",28000), ("LogiTrans Sp. z o.o.","1181234567",18000),
                    ("NovaPharma Sp. z o.o.","1121234567",25000)]),
        ]:
            for contractor, nip, amount in rev_data:
                _inv("income", contractor, nip, Decimal(str(amount)), f"2026-{month}-05", "SaaS License")

        # ── EXPENSES — monthly recurring ──
        for month in ["01", "02", "03", "04"]:
            _inv("expense", "Amazon Web Services",      "IE9692928F", Decimal("14000"), f"2026-{month}-01", "Infrastruktura")
            _inv("expense", "B2B R&D Team (5 osob)",    "self",       Decimal("60000"), f"2026-{month}-01", "R&D Wynagrodzenia B2B")
            _inv("expense", "R&D UoP (2 osoby)",        "self",       Decimal("18000"), f"2026-{month}-01", "R&D Wynagrodzenia UoP")
            _inv("expense", "Jan Kowalski JDG (Prezes)","1234567890", Decimal("12000"), f"2026-{month}-01", "Wynagrodzenie Prezesa B2B")
            _inv("expense", "WeWork Warszawa",          "5252678901", Decimal("9500"),  f"2026-{month}-05", "Czynsz biuro")
            _inv("expense", "Google Workspace + Tools",  "IE6388047V", Decimal("2200"),  f"2026-{month}-01", "Narzedzia")
            _inv("expense", "Google Gemini API",        "IE6388047V", Decimal("4500"),  f"2026-{month}-01", "AI/API")

        # Jednorazowe
        _inv("expense", "Kancelaria Podatkowa Nowak", "5271234567", Decimal("5500"), "2026-01-15", "Ksiegowosc")
        _inv("expense", "Google Ads",                 "IE6388047V", Decimal("18000"), "2026-02-20", "Marketing")
        _inv("expense", "Google Ads",                 "IE6388047V", Decimal("22000"), "2026-03-20", "Marketing")
        _inv("expense", "Kancelaria Podatkowa Nowak", "5271234567", Decimal("5500"), "2026-04-15", "Ksiegowosc")
        _inv("expense", "Dell Technologies",          "5262345678", Decimal("12000"), "2026-04-18", "Sprzet", is_paid=False, due_days=30)
        _inv("expense", "Konferencja SaaStr EU",      "DE998877665", Decimal("8500"), "2026-03-25", "Marketing", is_paid=True, due_days=7)

        db.add_all(invoices)
        await db.commit()

        income_count = sum(1 for i in invoices if i.invoice_type == "income")
        expense_count = sum(1 for i in invoices if i.invoice_type == "expense")
        print(f"[OK] Firma: TechFlow Sp. z o.o. (ID: {COMPANY_ID})")
        print(f"[OK] User: jan.kowalski@techflow.pl (ID: {USER_ID})")
        print(f"[OK] Faktury: {len(invoices)} total ({income_count} income + {expense_count} expense)")
        print(f"[OK] Kontekst: L1 + L2 (MRR 210k, SafeCash 485k, prezes B2B 12k)")
        print()

        print("[..] Przeliczam SafeCash i metryki faktur...")
        from app.services.context_operational import refresh_financial_metrics
        metrics = await refresh_financial_metrics(db, COMPANY_ID)
        print(f"[OK] SafeCash: {metrics.get('safecash', '?')} PLN")
        print(f"[OK] Monthly revenue: {metrics.get('monthly_revenue', '?')} PLN")
        print(f"[OK] Monthly expenses: {metrics.get('monthly_expenses', '?')} PLN")
        print()
        print(">> python -m scripts.test_cfo_query")


if __name__ == "__main__":
    asyncio.run(main())
