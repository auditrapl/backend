"""
Generator danych testowych dla Auditra — TechFlow Sp. z o.o.
Zakres: luty-kwiecień 2026 (3 miesiące)

Produkuje:
- invoices.json — wszystkie faktury (opłacone + nieopłacone)
- bank_transfers.csv — historia przelewów (TYLKO opłacone faktury)

Spójność: każda faktura is_paid=True w JSON ma odpowiadający przelew w CSV
"""

import json
import csv
import uuid
from datetime import date, timedelta
from decimal import Decimal
from dataclasses import dataclass, asdict, field
from typing import Optional

# ============================================================
# KONFIGURACJA FIRMY
# ============================================================

COMPANY_NAME = "TechFlow Sp. z o.o."
COMPANY_NIP = "5272958441"
COMPANY_ACCOUNT = "72 1140 2004 0000 3502 8765 4321"

# Saldo początkowe 01.02.2026 — punkt startowy dla CSV
STARTING_BALANCE = Decimal("185000.00")

# ============================================================
# KONTRAHENCI — KLIENCI (income)
# ============================================================

CLIENTS = [
    # Klient Alpha — największy (40% MRR — koncentracja ryzyka)
    {
        "name": "Alpha Digital Group Sp. z o.o.",
        "nip": "1234567890",
        "account": "56 1020 1234 0000 1111 2222 3333",
        "net_per_month": Decimal("85000.00"),
        "category": "Subskrypcja SaaS",
    },
    # Klient Beta
    {
        "name": "Beta Commerce S.A.",
        "nip": "2345678901",
        "account": "89 1050 1234 0000 2222 3333 4444",
        "net_per_month": Decimal("45000.00"),
        "category": "Subskrypcja SaaS",
    },
    # Klient Gamma
    {
        "name": "Gamma Retail Sp. z o.o.",
        "nip": "3456789012",
        "account": "23 1030 1234 0000 3333 4444 5555",
        "net_per_month": Decimal("28000.00"),
        "category": "Subskrypcja SaaS",
    },
    # Klient Delta
    {
        "name": "Delta Logistics Sp. z o.o.",
        "nip": "4567890123",
        "account": "67 1090 1234 0000 4444 5555 6666",
        "net_per_month": Decimal("22000.00"),
        "category": "Subskrypcja SaaS",
    },
    # Klient Epsilon
    {
        "name": "Epsilon Finance Sp. z o.o.",
        "nip": "5678901234",
        "account": "12 1160 1234 0000 5555 6666 7777",
        "net_per_month": Decimal("18000.00"),
        "category": "Subskrypcja SaaS",
    },
    # Klient Zeta — najmniejszy, nowy klient
    {
        "name": "Zeta Ventures Sp. z o.o.",
        "nip": "6789012345",
        "account": "45 1240 1234 0000 6666 7777 8888",
        "net_per_month": Decimal("12000.00"),
        "category": "Subskrypcja SaaS",
    },
]

# ============================================================
# KONTRAHENCI — KOSZTY RECURRING (B2B + usługi)
# ============================================================

# Developerzy B2B (koszt kwalifikowany IP Box!)
DEV_CONTRACTORS = [
    {
        "name": "SoftDev Solutions Sp. z o.o.",
        "nip": "9876543210",
        "account": "34 1050 9999 0000 1234 5678 9012",
        "net_per_month": Decimal("18500.00"),
        "category": "Usługi programistyczne B2B",
    },
    {
        "name": "CodeBase s.r.o.",  # czeski kontraktor
        "nip": "CZ67890123",
        "account": "PL87 1020 9999 0000 2345 6789 0123",
        "net_per_month": Decimal("15800.00"),
        "category": "Usługi programistyczne B2B",
    },
    {
        "name": "DevTeam Polska Sp. z o.o.",
        "nip": "8765432109",
        "account": "65 1240 9999 0000 3456 7890 1234",
        "net_per_month": Decimal("16200.00"),
        "category": "Usługi programistyczne B2B",
    },
    # UWAGA: Prezes fakturujący firmę przez własne JDG — ryzyko rekwalifikacji
    {
        "name": "Jan Kowalski - Usługi Programistyczne",  # JDG prezesa
        "nip": "7788990011",
        "account": "98 1090 9999 0000 4567 8901 2345",
        "net_per_month": Decimal("12000.00"),
        "category": "Usługi programistyczne B2B",
    },
    {
        "name": "Tech Masters Sp. z o.o.",
        "nip": "3344556677",
        "account": "21 1160 9999 0000 5678 9012 3456",
        "net_per_month": Decimal("14500.00"),
        "category": "Usługi programistyczne B2B",
    },
]

# Inne koszty stałe
RECURRING_EXPENSES = [
    # Google Ads Ireland — PROBLEM: polski VAT od zagranicznego dostawcy
    {
        "name": "Google Ireland Limited",
        "nip": "IE6388047V",
        "account": "PL23 1050 0099 0000 9876 5432 1098",
        "net_per_month": Decimal("6000.00"),
        "category": "Reklama online",
        "vat_rate": Decimal("0.23"),  # BŁĄD — zagraniczny dostawca nie powinien naliczać PL VAT
        "note_vat_issue": True,
    },
    # Facebook/Meta Ads
    {
        "name": "Meta Platforms Ireland Ltd",
        "nip": "IE9692928F",
        "account": "PL56 1020 0099 0000 8765 4321 0987",
        "net_per_month": Decimal("4200.00"),
        "category": "Reklama online",
        "vat_rate": Decimal("0.00"),  # reverse charge, poprawnie
    },
    # Hosting AWS
    {
        "name": "Amazon Web Services EMEA SARL",
        "nip": "LU26888617",
        "account": "PL89 1160 0099 0000 7654 3210 9876",
        "net_per_month": Decimal("3800.00"),
        "category": "Infrastruktura IT",
        "vat_rate": Decimal("0.00"),  # reverse charge
    },
    # GitHub Enterprise
    {
        "name": "GitHub Inc.",
        "nip": "US900112233",
        "account": "PL12 1240 0099 0000 6543 2109 8765",
        "net_per_month": Decimal("2100.00"),
        "category": "Oprogramowanie",
        "vat_rate": Decimal("0.00"),  # reverse charge
    },
    # Linear
    {
        "name": "Linear Orbit Inc.",
        "nip": "US877889900",
        "account": "PL45 1090 0099 0000 5432 1098 7654",
        "net_per_month": Decimal("450.00"),
        "category": "Oprogramowanie",
        "vat_rate": Decimal("0.00"),
    },
    # Biuro rachunkowe
    {
        "name": "Finanse i Księgowość Magdalena Nowak sp. k.",
        "nip": "1122334455",
        "account": "78 1030 0099 0000 4321 0987 6543",
        "net_per_month": Decimal("2500.00"),
        "category": "Usługi księgowe",
        "vat_rate": Decimal("0.23"),
    },
    # Czynsz biura
    {
        "name": "Warsaw Office Rentals Sp. z o.o.",
        "nip": "5566778899",
        "account": "34 1140 0099 0000 3210 9876 5432",
        "net_per_month": Decimal("6800.00"),
        "category": "Czynsz biura",
        "vat_rate": Decimal("0.23"),
    },
    # Leasing samochodu
    {
        "name": "Volkswagen Financial Services Polska Sp. z o.o.",
        "nip": "9900112233",
        "account": "67 1050 0099 0000 2109 8765 4321",
        "net_per_month": Decimal("2800.00"),
        "category": "Leasing",
        "vat_rate": Decimal("0.23"),
    },
]

# ============================================================
# JEDNORAZOWE KOSZTY (interesujące dla AI)
# ============================================================

ONE_TIME_EXPENSES = [
    # Laptop dla nowego dev — amortyzacja jednorazowa (mały podatnik)
    {
        "name": "MediaExpert S.A.",
        "nip": "6677889900",
        "account": "89 1020 9999 1111 9876 5432 1099",
        "net_amount": Decimal("13008.13"),  # MacBook Pro
        "vat_rate": Decimal("0.23"),
        "category": "Sprzęt komputerowy",
        "issue_date": date(2026, 3, 12),
        "payment_date": date(2026, 3, 20),
    },
    # Konferencja — szkolenie zespołu
    {
        "name": "Infoshare Academy Sp. z o.o.",
        "nip": "2233445566",
        "account": "12 1160 9999 1111 8765 4321 0988",
        "net_amount": Decimal("2845.53"),
        "vat_rate": Decimal("0.23"),
        "category": "Szkolenia i rozwój",
        "issue_date": date(2026, 3, 5),
        "payment_date": date(2026, 3, 15),
    },
    # Kawa/catering na event firmowy
    {
        "name": "Premium Catering Sp. z o.o.",
        "nip": "4455667788",
        "account": "45 1050 9999 1111 7654 3210 9877",
        "net_amount": Decimal("1626.02"),
        "vat_rate": Decimal("0.23"),
        "category": "Reprezentacja",  # UWAGA: reprezentacja = 0% KUP
        "issue_date": date(2026, 2, 20),
        "payment_date": date(2026, 2, 28),
    },
    # Oprogramowanie — Figma
    {
        "name": "Figma Inc.",
        "nip": "US987654321",
        "account": "78 1020 9999 1111 6543 2109 8766",
        "net_amount": Decimal("4800.00"),  # subskrypcja roczna
        "vat_rate": Decimal("0.00"),
        "category": "Oprogramowanie",
        "issue_date": date(2026, 2, 10),
        "payment_date": date(2026, 2, 12),
    },
    # Sprzęt biurowy — biurka i fotele (nowe stanowiska)
    {
        "name": "Office Pro Sp. z o.o.",
        "nip": "3344556688",
        "account": "98 1240 9999 1111 5432 1098 7655",
        "net_amount": Decimal("8943.09"),
        "vat_rate": Decimal("0.23"),
        "category": "Wyposażenie biura",
        "issue_date": date(2026, 4, 3),
        "payment_date": date(2026, 4, 10),
    },
]

# ============================================================
# STAŁE SYSTEMOWE
# ============================================================

VAT_RATE = Decimal("0.23")

# ============================================================
# HELPERY
# ============================================================

def calc_amounts(net: Decimal, vat_rate: Decimal = VAT_RATE) -> tuple[Decimal, Decimal, Decimal]:
    """Zwraca (net, vat, gross) zaokrąglone do 2 miejsc."""
    vat = (net * vat_rate).quantize(Decimal("0.01"))
    gross = (net + vat).quantize(Decimal("0.01"))
    return net.quantize(Decimal("0.01")), vat, gross


def month_invoice_number(year: int, month: int, seq: int, prefix: str = "FV") -> str:
    return f"{prefix}/{year}/{month:02d}/{seq:03d}"


# ============================================================
# GENERATOR FAKTUR
# ============================================================

def generate_invoices() -> list[dict]:
    """Generuje wszystkie faktury dla Feb/Mar/Apr 2026."""
    invoices = []
    seq_counter = {}  # per miesiąc

    def next_seq(d: date, prefix: str = "FV") -> int:
        key = (d.year, d.month, prefix)
        seq_counter[key] = seq_counter.get(key, 0) + 1
        return seq_counter[key]

    # ---------- FAKTURY PRZYCHODOWE (income) ----------
    # Wystawiane 1-go każdego miesiąca, płatność 14 dni
    for month in [2, 3, 4]:
        issue_date = date(2026, month, 1)
        due_date = issue_date + timedelta(days=14)

        for i, client in enumerate(CLIENTS):
            net, vat, gross = calc_amounts(client["net_per_month"])

            # Logika opłacenia — realistyczne wzorce
            if month == 2:
                # Wszystkie luty opłacone
                is_paid = True
                payment_date = issue_date + timedelta(days=9 + i)  # 10-15.02
            elif month == 3:
                # Wszystkie marzec opłacone
                is_paid = True
                payment_date = issue_date + timedelta(days=10 + i)  # 11-16.03
            else:  # month == 4
                # Kwiecień — mix: Alpha, Gamma, Epsilon opłacone; Beta i Delta zalegają; Zeta jeszcze nie wymagalny
                if client["name"].startswith("Alpha"):
                    is_paid = True
                    payment_date = date(2026, 4, 14)
                elif client["name"].startswith("Gamma"):
                    is_paid = True
                    payment_date = date(2026, 4, 10)
                elif client["name"].startswith("Epsilon"):
                    is_paid = True
                    payment_date = date(2026, 4, 12)
                elif client["name"].startswith("Zeta"):
                    # Zeta wystawiony później — 10.04, nie wymagalny jeszcze
                    issue_date = date(2026, 4, 10)
                    due_date = issue_date + timedelta(days=14)
                    is_paid = False
                    payment_date = None
                else:
                    # Beta i Delta — nieopłacone (overdue!)
                    is_paid = False
                    payment_date = None

            inv = {
                "id": str(uuid.uuid4()),
                "internal_number": month_invoice_number(issue_date.year, issue_date.month, next_seq(issue_date)),
                "contractor_name": client["name"],
                "contractor_nip": client["nip"],
                "contractor_account": client["account"],
                "net_amount": str(net),
                "vat_amount": str(vat),
                "gross_amount": str(gross),
                "currency": "PLN",
                "issue_date": issue_date.isoformat(),
                "due_date": due_date.isoformat(),
                "payment_date": payment_date.isoformat() if payment_date else None,
                "payment_days": 14,
                "status": "approved",
                "invoice_type": "income",
                "is_vat_exempt": False,
                "is_paid": is_paid,
                "category_name": client["category"],
                "department_id": None,  # nieprzypisane — ważne dla testu halucynacji oddziałów
                "description": f"Subskrypcja SaaS TechFlow - {issue_date.strftime('%B %Y')}",
            }
            invoices.append(inv)

    # ---------- FAKTURY KOSZTOWE: DEVELOPERZY B2B ----------
    # Wystawiane 1-5 każdego miesiąca, płatność 14-21 dni
    for month in [2, 3, 4]:
        for i, dev in enumerate(DEV_CONTRACTORS):
            issue_date = date(2026, month, 1 + (i % 3))  # 1-3
            due_date = issue_date + timedelta(days=14)
            net, vat, gross = calc_amounts(dev["net_per_month"])

            if month == 2 or month == 3:
                is_paid = True
                payment_date = issue_date + timedelta(days=10 + (i * 2))  # 11-19 dnia
            else:  # kwiecień
                # Większość opłacona, 1 jeszcze nie
                if i < 4:
                    is_paid = True
                    payment_date = issue_date + timedelta(days=12 + i)
                else:
                    is_paid = False
                    payment_date = None

            inv = {
                "id": str(uuid.uuid4()),
                "internal_number": month_invoice_number(issue_date.year, issue_date.month, next_seq(issue_date)),
                "contractor_name": dev["name"],
                "contractor_nip": dev["nip"],
                "contractor_account": dev["account"],
                "net_amount": str(net),
                "vat_amount": str(vat),
                "gross_amount": str(gross),
                "currency": "PLN",
                "issue_date": issue_date.isoformat(),
                "due_date": due_date.isoformat(),
                "payment_date": payment_date.isoformat() if payment_date else None,
                "payment_days": 14,
                "status": "approved",
                "invoice_type": "expense",
                "is_vat_exempt": False,
                "is_paid": is_paid,
                "category_name": dev["category"],
                "department_id": None,
                "description": f"Usługi programistyczne - {issue_date.strftime('%B %Y')}",
            }
            invoices.append(inv)

    # ---------- FAKTURY KOSZTOWE: RECURRING (Google Ads, hosting, etc.) ----------
    for month in [2, 3, 4]:
        for i, exp in enumerate(RECURRING_EXPENSES):
            issue_date = date(2026, month, 5 + (i % 5))  # 5-9
            due_date = issue_date + timedelta(days=14)
            vat_rate = exp.get("vat_rate", VAT_RATE)
            net, vat, gross = calc_amounts(exp["net_per_month"], vat_rate)

            if month == 2 or month == 3:
                is_paid = True
                payment_date = issue_date + timedelta(days=8 + (i * 2))
            else:
                # kwiecień — mix
                if i < 6:
                    is_paid = True
                    payment_date = issue_date + timedelta(days=7 + i)
                else:
                    is_paid = False
                    payment_date = None

            description = f"{exp['category']} - {issue_date.strftime('%B %Y')}"
            if exp.get("note_vat_issue"):
                description += " [UWAGA: zagraniczny dostawca z naliczonym PL VAT]"

            inv = {
                "id": str(uuid.uuid4()),
                "internal_number": month_invoice_number(issue_date.year, issue_date.month, next_seq(issue_date)),
                "contractor_name": exp["name"],
                "contractor_nip": exp["nip"],
                "contractor_account": exp["account"],
                "net_amount": str(net),
                "vat_amount": str(vat),
                "gross_amount": str(gross),
                "currency": "PLN",
                "issue_date": issue_date.isoformat(),
                "due_date": due_date.isoformat(),
                "payment_date": payment_date.isoformat() if payment_date else None,
                "payment_days": 14,
                "status": "approved",
                "invoice_type": "expense",
                "is_vat_exempt": False,
                "is_paid": is_paid,
                "category_name": exp["category"],
                "department_id": None,
                "description": description,
            }
            invoices.append(inv)

    # ---------- FAKTURY JEDNORAZOWE ----------
    for exp in ONE_TIME_EXPENSES:
        issue_date = exp["issue_date"]
        payment_date = exp["payment_date"]
        due_date = issue_date + timedelta(days=14)
        net, vat, gross = calc_amounts(exp["net_amount"], exp["vat_rate"])

        is_paid = payment_date <= date(2026, 4, 16)

        inv = {
            "id": str(uuid.uuid4()),
            "internal_number": month_invoice_number(issue_date.year, issue_date.month, next_seq(issue_date)),
            "contractor_name": exp["name"],
            "contractor_nip": exp["nip"],
            "contractor_account": exp["account"],
            "net_amount": str(net),
            "vat_amount": str(vat),
            "gross_amount": str(gross),
            "currency": "PLN",
            "issue_date": issue_date.isoformat(),
            "due_date": due_date.isoformat(),
            "payment_date": payment_date.isoformat() if is_paid else None,
            "payment_days": 14,
            "status": "approved",
            "invoice_type": "expense",
            "is_vat_exempt": False,
            "is_paid": is_paid,
            "category_name": exp["category"],
            "department_id": None,
            "description": f"{exp['category']} - faktura jednorazowa",
        }
        invoices.append(inv)

    # Sortuj po dacie wystawienia
    invoices.sort(key=lambda x: x["issue_date"])

    return invoices


# ============================================================
# GENERATOR PRZELEWÓW BANKOWYCH (TYLKO OPŁACONE FAKTURY)
# ============================================================

def generate_bank_transfers(invoices: list[dict]) -> list[dict]:
    """Każda faktura is_paid=True → przelew. Plus dodatkowe przelewy pozafakturowe."""
    transfers = []

    # 1. Przelewy z opłaconych faktur
    for inv in invoices:
        if not inv["is_paid"]:
            continue

        amount = Decimal(inv["gross_amount"])
        if inv["invoice_type"] == "expense":
            amount = -amount  # wychodzące

        transfers.append({
            "date": inv["payment_date"],
            "counterparty": inv["contractor_name"],
            "counterparty_nip": inv["contractor_nip"],
            "counterparty_account": inv["contractor_account"],
            "amount": amount,
            "title": inv["internal_number"],
            "description": f"Platnosc faktury {inv['internal_number']} - {inv['description']}",
            "transaction_type": "IN" if inv["invoice_type"] == "income" else "OUT",
            "category_hint": "invoice_payment",
        })

    # 2. POZAFAKTUROWE transakcje — KRYTYCZNE dla testu discrepancy!
    # To są rzeczy które user nie ma jako faktury ale wpływają na saldo
    extra_transactions = [
        # Pensje UoP (2 osoby × 3 miesiące)
        {"date": "2026-02-28", "counterparty": "Anna Wiśniewska",
         "counterparty_nip": "", "counterparty_account": "12 1050 0088 0000 1111 1111 1111",
         "amount": Decimal("-8500.00"), "title": "Wynagrodzenie 02/2026",
         "description": "Wynagrodzenie UoP - Anna Wisniewska",
         "transaction_type": "OUT", "category_hint": "salary"},
        {"date": "2026-02-28", "counterparty": "Piotr Kowalczyk",
         "counterparty_nip": "", "counterparty_account": "56 1020 0088 0000 2222 2222 2222",
         "amount": Decimal("-7200.00"), "title": "Wynagrodzenie 02/2026",
         "description": "Wynagrodzenie UoP - Piotr Kowalczyk",
         "transaction_type": "OUT", "category_hint": "salary"},
        {"date": "2026-03-31", "counterparty": "Anna Wiśniewska",
         "counterparty_nip": "", "counterparty_account": "12 1050 0088 0000 1111 1111 1111",
         "amount": Decimal("-8500.00"), "title": "Wynagrodzenie 03/2026",
         "description": "Wynagrodzenie UoP - Anna Wisniewska",
         "transaction_type": "OUT", "category_hint": "salary"},
        {"date": "2026-03-31", "counterparty": "Piotr Kowalczyk",
         "counterparty_nip": "", "counterparty_account": "56 1020 0088 0000 2222 2222 2222",
         "amount": Decimal("-7200.00"), "title": "Wynagrodzenie 03/2026",
         "description": "Wynagrodzenie UoP - Piotr Kowalczyk",
         "transaction_type": "OUT", "category_hint": "salary"},

        # ZUS (przedsiębiorca + pracownicy)
        {"date": "2026-02-15", "counterparty": "Zakład Ubezpieczeń Społecznych",
         "counterparty_nip": "5252013110", "counterparty_account": "83 1010 1023 0000 2613 9520 0000",
         "amount": Decimal("-6420.00"), "title": "ZUS 01/2026",
         "description": "Skladki ZUS za styczen 2026",
         "transaction_type": "OUT", "category_hint": "zus"},
        {"date": "2026-03-16", "counterparty": "Zakład Ubezpieczeń Społecznych",
         "counterparty_nip": "5252013110", "counterparty_account": "83 1010 1023 0000 2613 9520 0000",
         "amount": Decimal("-6420.00"), "title": "ZUS 02/2026",
         "description": "Skladki ZUS za luty 2026",
         "transaction_type": "OUT", "category_hint": "zus"},
        {"date": "2026-04-15", "counterparty": "Zakład Ubezpieczeń Społecznych",
         "counterparty_nip": "5252013110", "counterparty_account": "83 1010 1023 0000 2613 9520 0000",
         "amount": Decimal("-6580.00"), "title": "ZUS 03/2026",
         "description": "Skladki ZUS za marzec 2026",
         "transaction_type": "OUT", "category_hint": "zus"},

        # VAT i CIT
        {"date": "2026-02-25", "counterparty": "Urząd Skarbowy Warszawa Śródmieście",
         "counterparty_nip": "5261027145", "counterparty_account": "78 1010 1010 0165 7022 2400 0000",
         "amount": Decimal("-19800.00"), "title": "VAT 01/2026",
         "description": "VAT za styczen 2026",
         "transaction_type": "OUT", "category_hint": "tax_vat"},
        {"date": "2026-03-25", "counterparty": "Urząd Skarbowy Warszawa Śródmieście",
         "counterparty_nip": "5261027145", "counterparty_account": "78 1010 1010 0165 7022 2400 0000",
         "amount": Decimal("-22300.00"), "title": "VAT 02/2026",
         "description": "VAT za luty 2026",
         "transaction_type": "OUT", "category_hint": "tax_vat"},
        # Zaliczki CIT
        {"date": "2026-03-20", "counterparty": "Urząd Skarbowy Warszawa Śródmieście",
         "counterparty_nip": "5261027145", "counterparty_account": "11 1010 1010 0165 7022 2900 0000",
         "amount": Decimal("-7800.00"), "title": "CIT zaliczka Q1 2026",
         "description": "Zaliczka CIT za I kwartal 2026",
         "transaction_type": "OUT", "category_hint": "tax_cit"},

        # Prowizje bankowe
        {"date": "2026-02-28", "counterparty": "mBank S.A.",
         "counterparty_nip": "5260215088", "counterparty_account": "",
         "amount": Decimal("-89.00"), "title": "Prowizja miesięczna",
         "description": "Prowizja za prowadzenie konta firmowego",
         "transaction_type": "OUT", "category_hint": "bank_fee"},
        {"date": "2026-03-31", "counterparty": "mBank S.A.",
         "counterparty_nip": "5260215088", "counterparty_account": "",
         "amount": Decimal("-89.00"), "title": "Prowizja miesięczna",
         "description": "Prowizja za prowadzenie konta firmowego",
         "transaction_type": "OUT", "category_hint": "bank_fee"},

        # Dodatkowy przychód — wpłata od nowego leada (zaliczka na custom development)
        {"date": "2026-03-22", "counterparty": "Omega Industries Sp. z o.o.",
         "counterparty_nip": "7890123456", "counterparty_account": "45 1140 0099 1111 3333 4444 5555",
         "amount": Decimal("25000.00"), "title": "Zaliczka - custom dev",
         "description": "Zaliczka na prace custom development - pilot",
         "transaction_type": "IN", "category_hint": "prepayment"},
    ]

    transfers.extend(extra_transactions)

    # Sortuj po dacie
    transfers.sort(key=lambda x: x["date"])

    # Oblicz saldo po każdej operacji
    balance = STARTING_BALANCE
    for t in transfers:
        balance += Decimal(str(t["amount"]))
        t["balance_after"] = balance

    return transfers


# ============================================================
# MAIN
# ============================================================

def main():
    invoices = generate_invoices()
    transfers = generate_bank_transfers(invoices)

    # === JSON ===
    output = {
        "company": {
            "name": COMPANY_NAME,
            "nip": COMPANY_NIP,
            "account": COMPANY_ACCOUNT,
        },
        "period": {
            "from": "2026-02-01",
            "to": "2026-04-16",
        },
        "summary": {
            "total_invoices": len(invoices),
            "paid_invoices": sum(1 for i in invoices if i["is_paid"]),
            "unpaid_invoices": sum(1 for i in invoices if not i["is_paid"]),
            "income_invoices": sum(1 for i in invoices if i["invoice_type"] == "income"),
            "expense_invoices": sum(1 for i in invoices if i["invoice_type"] == "expense"),
        },
        "invoices": invoices,
    }

    with open("/home/claude/testdata/invoices.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"✓ invoices.json — {len(invoices)} faktur")
    print(f"  - Opłacone: {output['summary']['paid_invoices']}")
    print(f"  - Nieopłacone: {output['summary']['unpaid_invoices']}")
    print(f"  - Przychody: {output['summary']['income_invoices']}")
    print(f"  - Koszty: {output['summary']['expense_invoices']}")

    # === CSV ===
    with open("/home/claude/testdata/bank_transfers.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow([
            "Data operacji",
            "Data ksiegowania",
            "Typ",
            "Kontrahent",
            "NIP kontrahenta",
            "Numer konta kontrahenta",
            "Tytul",
            "Opis operacji",
            "Kwota",
            "Waluta",
            "Saldo po operacji",
        ])
        for t in transfers:
            writer.writerow([
                t["date"],
                t["date"],
                t["transaction_type"],
                t["counterparty"],
                t["counterparty_nip"],
                t["counterparty_account"],
                t["title"],
                t["description"],
                f"{t['amount']:.2f}",
                "PLN",
                f"{t['balance_after']:.2f}",
            ])

    print(f"\n✓ bank_transfers.csv — {len(transfers)} przelewow")
    print(f"  Saldo poczatkowe: {STARTING_BALANCE} PLN")
    print(f"  Saldo koncowe:    {transfers[-1]['balance_after']} PLN")

    # Sanity check spójności
    print(f"\n=== SPOJNOSC ===")
    invoice_payments = sum(1 for i in invoices if i["is_paid"])
    invoice_based_transfers = sum(1 for t in transfers if t["category_hint"] == "invoice_payment")
    print(f"Faktury opłacone: {invoice_payments}")
    print(f"Przelewy z faktur w CSV: {invoice_based_transfers}")
    print(f"Pozafakturowe przelewy: {len(transfers) - invoice_based_transfers}")
    assert invoice_payments == invoice_based_transfers, "NIESPÓJNOŚĆ!"
    print("✓ 1:1 faktury opłacone vs przelewy z faktur")

    # Discrepancy test
    safecash = sum(
        Decimal(i["gross_amount"]) if i["invoice_type"] == "income" else -Decimal(i["gross_amount"])
        for i in invoices if i["is_paid"]
    )
    bank_balance = transfers[-1]["balance_after"]
    print(f"\n=== DISCREPANCY TEST ===")
    print(f"SafeCash (z paid invoices):     {safecash:>12.2f} PLN")
    print(f"Bank balance (saldo konta):     {bank_balance:>12.2f} PLN")
    print(f"Różnica (pozafakturowe):        {bank_balance - safecash:>12.2f} PLN")
    print(f"→ To jest właśnie problem który cashflow v2 ma rozwiązać!")


if __name__ == "__main__":
    main()
