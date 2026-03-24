"""
Rejestr Polskich Obowiązków Podatkowych.
Deklaratywna definicja ~25 obowiązków z terminami, częstotliwością i logiką applies_to.
Używany przez cashflow_engine.generate_tax_calendar().
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.context_canonical import CanonicalContext


# ==============================================================================
# MODEL OBOWIĄZKU
# ==============================================================================

@dataclass
class TaxObligation:
    id: str
    label_template: str         # "VAT-7 za {prev_month} {year}" — placeholders: {prev_month}, {month}, {year}, {quarter}
    day_of_month: int           # termin: 15, 20, 25, 31
    frequency: str              # "monthly", "quarterly", "annual", "semi_annual"
    category: str               # "VAT", "CIT", "PIT", "ZUS", "Zdrowotna", "JPK", "Special"
    quarter_months: list[int] = field(default_factory=list)   # miesiące dla quarterly [1,4,7,10] lub semi [2,9]
    annual_month: int = 0       # miesiąc dla annual (3=marzec, 4=kwiecień)
    annual_day: int = 0         # dzień dla annual (31, 30)
    default_amount: Optional[float] = None
    amount_key: str = "none"    # klucz do lookup kwoty: "vat", "cit_monthly", "zus", "zdrowotna", "fixed", "none"


# ==============================================================================
# KWOTY ZUS 2026
# ==============================================================================

ZUS_AMOUNTS_2026: dict[str, Optional[float]] = {
    "ulga_na_start": 0.0,
    "preferencyjny": 420.0,
    "maly_zus_plus": None,      # zmienny — zależy od przychodu
    "pelny": 1600.0,
}

# Składka zdrowotna — szacunki domyślne (monthly)
ZDROWOTNA_DEFAULTS: dict[str, float] = {
    "Liniowy 19%": 350.0,       # 4.9% od dochodu — default placeholder
    "Skala podatkowa": 450.0,   # 9% od dochodu — default placeholder
    "Ryczałt": 400.0,           # ryczałtowa — zależna od przychodu
}


# ==============================================================================
# LOGIKA APPLIES_TO
# ==============================================================================

def _is_jdg(c: CanonicalContext) -> bool:
    lf = c.company_identity.legal_form
    return bool(lf and "JDG" in lf.upper())


def _is_corp(c: CanonicalContext) -> bool:
    return not _is_jdg(c)


def _is_vat_payer(c: CanonicalContext) -> bool:
    vr = c.tax_regime.vat_rate
    return vr is not None and vr != "ZW"


def _vat_monthly(c: CanonicalContext) -> bool:
    return _is_vat_payer(c) and c.tax_regime.vat_frequency != "quarterly"


def _vat_quarterly(c: CanonicalContext) -> bool:
    return _is_vat_payer(c) and c.tax_regime.vat_frequency == "quarterly"


def _has_cit(c: CanonicalContext) -> bool:
    return c.tax_regime.cit_rate is not None


def _has_cit_not_estonski(c: CanonicalContext) -> bool:
    tf = c.tax_regime.taxation_form or ""
    return _has_cit(c) and "estoński" not in tf.lower()


def _is_cit_estonski(c: CanonicalContext) -> bool:
    tf = c.tax_regime.taxation_form or ""
    return "estoński" in tf.lower()


def _has_employees(c: CanonicalContext) -> bool:
    ec = c.tax_regime.employee_count
    return ec is not None and ec > 0


def _has_special(obligation_id: str):
    """Factory: zwraca predykat sprawdzający czy obligation_id jest w special_obligations."""
    def check(c: CanonicalContext) -> bool:
        return obligation_id in c.tax_regime.special_obligations
    return check


def _always(c: CanonicalContext) -> bool:
    return True


def _jpk(c: CanonicalContext) -> bool:
    return c.tax_regime.jpk_obligation is True


# ==============================================================================
# REJESTR — OBOWIĄZKI PODSTAWOWE
# ==============================================================================

BASIC_OBLIGATIONS: list[tuple[TaxObligation, callable]] = [
    # --- VAT ---
    (TaxObligation(
        id="vat-7", label_template="VAT-7 za {prev_month} {year}",
        day_of_month=25, frequency="monthly", category="VAT", amount_key="vat",
    ), _vat_monthly),

    (TaxObligation(
        id="vat-7k", label_template="VAT-7K za {quarter} {year}",
        day_of_month=25, frequency="quarterly", category="VAT",
        quarter_months=[1, 4, 7, 10], amount_key="vat_quarterly",
    ), _vat_quarterly),

    # --- JPK ---
    (TaxObligation(
        id="jpk-vat", label_template="JPK_VAT za {prev_month} {year}",
        day_of_month=25, frequency="monthly", category="JPK", amount_key="none",
    ), _jpk),

    # --- CIT ---
    (TaxObligation(
        id="cit-advance", label_template="CIT zaliczka za {prev_month} {year}",
        day_of_month=20, frequency="monthly", category="CIT", amount_key="cit_monthly",
    ), _has_cit_not_estonski),

    (TaxObligation(
        id="cit-annual", label_template="CIT roczny za {prev_year}",
        day_of_month=0, frequency="annual", category="CIT",
        annual_month=3, annual_day=31, amount_key="cit_annual",
    ), _has_cit),

    (TaxObligation(
        id="cit-estonski-info", label_template="CIT estoński — podatek przy dystrybucji zysku",
        day_of_month=0, frequency="annual", category="CIT",
        annual_month=3, annual_day=31, amount_key="none",
    ), _is_cit_estonski),

    # --- PIT (JDG) ---
    (TaxObligation(
        id="pit-advance", label_template="PIT zaliczka za {prev_month} {year}",
        day_of_month=20, frequency="monthly", category="PIT", amount_key="none",
    ), _is_jdg),

    (TaxObligation(
        id="pit-annual", label_template="PIT roczny za {prev_year}",
        day_of_month=0, frequency="annual", category="PIT",
        annual_month=4, annual_day=30, amount_key="none",
    ), _is_jdg),

    # --- PIT-4R (pracodawcy) ---
    (TaxObligation(
        id="pit-4r", label_template="PIT-4R za {prev_month} {year}",
        day_of_month=20, frequency="monthly", category="PIT", amount_key="none",
    ), _has_employees),

    # --- ZUS ---
    (TaxObligation(
        id="zus", label_template="ZUS za {prev_month} {year}",
        day_of_month=15, frequency="monthly", category="ZUS", amount_key="zus",
    ), _always),
    # day_of_month=15 for JDG solo, 20 for employers — resolved dynamically in generator

    # --- Składka zdrowotna ---
    (TaxObligation(
        id="skladka-zdrowotna", label_template="Składka zdrowotna za {prev_month} {year}",
        day_of_month=20, frequency="monthly", category="Zdrowotna", amount_key="zdrowotna",
    ), _always),
]


# ==============================================================================
# REJESTR — OBOWIĄZKI SPECJALNE (branżowe / rzadkie)
# ==============================================================================

SPECIAL_OBLIGATIONS: list[tuple[TaxObligation, callable]] = [
    # --- PFRON ---
    (TaxObligation(
        id="pfron", label_template="PFRON za {prev_month} {year}",
        day_of_month=20, frequency="monthly", category="Special", amount_key="none",
    ), _has_special("pfron")),

    # --- Akcyza ---
    (TaxObligation(
        id="akcyza", label_template="Akcyza za {prev_month} {year}",
        day_of_month=25, frequency="monthly", category="Special", amount_key="none",
    ), _has_special("akcyza")),

    # --- Opłata cukrowa ---
    (TaxObligation(
        id="cukrowa", label_template="Opłata cukrowa za {prev_month} {year}",
        day_of_month=25, frequency="monthly", category="Special", amount_key="none",
    ), _has_special("cukrowa")),

    # --- Opłata od małpek ---
    (TaxObligation(
        id="malpki", label_template="Opłata od małpek za {prev_month} {year}",
        day_of_month=25, frequency="monthly", category="Special", amount_key="none",
    ), _has_special("malpki")),

    # --- Podatek od nieruchomości (kwartalne: 15.03, 15.05, 15.09, 15.11) ---
    (TaxObligation(
        id="nieruchomosci", label_template="Podatek od nieruchomości — rata {quarter} {year}",
        day_of_month=15, frequency="quarterly", category="Special",
        quarter_months=[3, 5, 9, 11], amount_key="none",
    ), _has_special("nieruchomosci")),

    # --- Podatek od środków transportu (15.02, 15.09) ---
    (TaxObligation(
        id="srodki_transportu", label_template="Podatek od śr. transportu — rata {year}",
        day_of_month=15, frequency="semi_annual", category="Special",
        quarter_months=[2, 9], amount_key="none",
    ), _has_special("srodki_transportu")),

    # --- Opłata środowiskowa (roczna, 31.03) ---
    (TaxObligation(
        id="srodowiskowa", label_template="Opłata środowiskowa za {prev_year}",
        day_of_month=0, frequency="annual", category="Special",
        annual_month=3, annual_day=31, amount_key="none",
    ), _has_special("srodowiskowa")),

    # --- Opłata produktowa (roczna, 15.03) ---
    (TaxObligation(
        id="produktowa", label_template="Opłata produktowa za {prev_year}",
        day_of_month=0, frequency="annual", category="Special",
        annual_month=3, annual_day=15, amount_key="none",
    ), _has_special("produktowa")),

    # --- BDO sprawozdanie (roczne, 15.03) ---
    (TaxObligation(
        id="bdo", label_template="BDO sprawozdanie za {prev_year}",
        day_of_month=0, frequency="annual", category="Special",
        annual_month=3, annual_day=15, amount_key="none",
    ), _has_special("bdo")),

    # --- Podatek bankowy ---
    (TaxObligation(
        id="bankowy", label_template="Podatek bankowy za {prev_month} {year}",
        day_of_month=25, frequency="monthly", category="Special", amount_key="none",
    ), _has_special("bankowy")),

    # --- Podatek od gier ---
    (TaxObligation(
        id="gry", label_template="Podatek od gier za {prev_month} {year}",
        day_of_month=10, frequency="monthly", category="Special", amount_key="none",
    ), _has_special("gry")),

    # --- WHT (podatek u źródła) ---
    (TaxObligation(
        id="wht", label_template="WHT za {prev_month} {year}",
        day_of_month=20, frequency="monthly", category="Special", amount_key="none",
    ), _has_special("wht")),

    # --- Danina solidarnościowa (roczna, 30.04) ---
    (TaxObligation(
        id="danina_solidarnosciowa", label_template="Danina solidarnościowa za {prev_year}",
        day_of_month=0, frequency="annual", category="Special",
        annual_month=4, annual_day=30, amount_key="none",
    ), _has_special("danina_solidarnosciowa")),

    # --- Minimalny podatek dochodowy (roczny, razem z CIT) ---
    (TaxObligation(
        id="min_podatek_dochodowy", label_template="Min. podatek dochodowy za {prev_year}",
        day_of_month=0, frequency="annual", category="Special",
        annual_month=3, annual_day=31, amount_key="none",
    ), _has_special("min_podatek_dochodowy")),

    # --- Podatek od budynków (miesięczny, 20.) ---
    (TaxObligation(
        id="budynki", label_template="Podatek od budynków za {prev_month} {year}",
        day_of_month=20, frequency="monthly", category="Special", amount_key="none",
    ), _has_special("budynki")),

    # --- Podatek tonażowy ---
    (TaxObligation(
        id="tonazowy", label_template="Podatek tonażowy za {prev_month} {year}",
        day_of_month=20, frequency="monthly", category="Special", amount_key="none",
    ), _has_special("tonazowy")),

    # --- Opłata paliwowa ---
    (TaxObligation(
        id="paliwowa", label_template="Opłata paliwowa za {prev_month} {year}",
        day_of_month=25, frequency="monthly", category="Special", amount_key="none",
    ), _has_special("paliwowa")),

    # --- Podatek rolny (kwartalne: 15.03, 15.05, 15.09, 15.11) ---
    (TaxObligation(
        id="rolny", label_template="Podatek rolny — rata {quarter} {year}",
        day_of_month=15, frequency="quarterly", category="Special",
        quarter_months=[3, 5, 9, 11], amount_key="none",
    ), _has_special("rolny")),

    # --- Opłata audiowizualna (kwartalna) ---
    (TaxObligation(
        id="audiowizualna", label_template="Opłata audiowizualna — {quarter} {year}",
        day_of_month=30, frequency="quarterly", category="Special",
        quarter_months=[3, 6, 9, 12], amount_key="none",
    ), _has_special("audiowizualna")),
]


# ==============================================================================
# PEŁNY REJESTR
# ==============================================================================

ALL_OBLIGATIONS: list[tuple[TaxObligation, callable]] = BASIC_OBLIGATIONS + SPECIAL_OBLIGATIONS

# Lookup by ID
OBLIGATION_BY_ID: dict[str, TaxObligation] = {
    ob.id: ob for ob, _ in ALL_OBLIGATIONS
}
