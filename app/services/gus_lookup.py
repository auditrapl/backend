"""
Serwis NIP Lookup — Biała Lista VAT MF.
Darmowe REST API bez klucza.
Dla firm bez VAT → user wpisuje nazwę ręcznie w onboardingu.
"""
import logging
from datetime import date
from typing import Optional

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

VAT_WHITELIST_URL = "https://wl-api.mf.gov.pl/api/search/nip"
TIMEOUT = 8.0


class CompanyLookupResult(BaseModel):
    name: Optional[str] = None
    legal_form: Optional[str] = None
    address: Optional[str] = None
    regon: Optional[str] = None
    krs: Optional[str] = None
    vat_status: Optional[str] = None
    bank_accounts: list[str] = []
    registration_date: Optional[str] = None


class NIPLookupResult(BaseModel):
    nip: str
    company: Optional[CompanyLookupResult] = None
    errors: list[str] = []


def _infer_legal_form(name: str | None, krs: str | None) -> str | None:
    if not name:
        return None
    upper = name.upper()
    if "SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ" in upper or "SP. Z O.O." in upper:
        return "Sp. z o.o."
    if "SPÓŁKA AKCYJNA" in upper or upper.endswith(" S.A.") or " S.A. " in upper:
        return "S.A."
    if "SPÓŁKA KOMANDYTOWA" in upper or " S.K." in upper or " SP.K." in upper:
        return "S.K."
    if "SPÓŁKA JAWNA" in upper or " SP.J." in upper or " S.J." in upper:
        return "Sp. j."
    if "SPÓŁKA PARTNERSKA" in upper or " SP.P." in upper:
        return "Sp. p."
    if "SPÓŁKA CYWILNA" in upper or " S.C." in upper:
        return "S.C."
    if not krs:
        return "JDG"
    return None


async def lookup_nip(nip: str) -> NIPLookupResult:
    nip_clean = nip.replace("-", "").replace(" ", "").strip()
    if len(nip_clean) != 10 or not nip_clean.isdigit():
        return NIPLookupResult(nip=nip_clean, errors=["NIP musi mieć 10 cyfr"])

    try:
        url = f"{VAT_WHITELIST_URL}/{nip_clean}?date={date.today().isoformat()}"
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        subject = data.get("result", {}).get("subject")
        if not subject:
            return NIPLookupResult(
                nip=nip_clean,
                company=CompanyLookupResult(vat_status="Niezarejestrowany"),
            )

        address = subject.get("workingAddress") or subject.get("residenceAddress")
        name = subject.get("name")
        krs = subject.get("krs")

        return NIPLookupResult(
            nip=nip_clean,
            company=CompanyLookupResult(
                name=name,
                legal_form=_infer_legal_form(name, krs),
                address=address,
                regon=subject.get("regon"),
                krs=krs,
                vat_status=subject.get("statusVat", "Niezarejestrowany"),
                bank_accounts=subject.get("accountNumbers", []) or [],
                registration_date=subject.get("registrationLegalDate"),
            ),
        )
    except Exception as e:
        logger.warning(f"VAT whitelist failed for NIP {nip_clean}: {e}")
        return NIPLookupResult(
            nip=nip_clean,
            errors=[f"Biała Lista VAT niedostępna: {str(e)[:100]}"],
        )
