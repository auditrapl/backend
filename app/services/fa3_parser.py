from __future__ import annotations

from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET


def _tag_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _normalize(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _to_decimal(value: str | None) -> Decimal | None:
    value = _normalize(value)
    if value is None:
        return None
    try:
        return Decimal(value.replace(",", "."))
    except InvalidOperation:
        return None


def _find_text_any(node: ET.Element, names: tuple[str, ...]) -> str | None:
    wanted = set(names)
    for child in node.iter():
        if _tag_name(child.tag) in wanted:
            return _normalize(child.text)
    return None


def _iter_by_localname(node: ET.Element, local_name: str):
    for child in node.iter():
        if _tag_name(child.tag) == local_name:
            yield child


def parse_fa3_xml(xml_content: str) -> dict:
    """Parse Polish KSeF FA(3)-like XML invoice into structured JSON.

    The parser is namespace-agnostic and extracts stable invoice fields used by Auditra.
    It raises ValueError for malformed XML or clearly non-invoice payloads.
    """

    if not xml_content or not xml_content.strip():
        raise ValueError("Empty XML content")

    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid XML: {exc}") from exc

    # Heuristic guard: FA structures usually include at least one Podmiot and a summary block.
    has_subject = any(_tag_name(e.tag) in {"Podmiot1", "Podmiot2"} for e in root.iter())
    has_summary = any(_tag_name(e.tag) in {"Fa", "Faktura", "FaWiersz", "FaCtrl"} for e in root.iter())
    if not (has_subject and has_summary):
        raise ValueError("XML does not look like FA(3) invoice payload")

    podmiot1 = next(_iter_by_localname(root, "Podmiot1"), None)
    podmiot2 = next(_iter_by_localname(root, "Podmiot2"), None)

    seller_name = _find_text_any(podmiot1, ("Nazwa", "PelnaNazwa")) if podmiot1 is not None else None
    seller_nip = _find_text_any(podmiot1, ("NIP",)) if podmiot1 is not None else None
    buyer_name = _find_text_any(podmiot2, ("Nazwa", "PelnaNazwa")) if podmiot2 is not None else None
    buyer_nip = _find_text_any(podmiot2, ("NIP",)) if podmiot2 is not None else None

    issue_date = _find_text_any(root, ("P_1", "DataWystawienia", "DataWytworzeniaFa"))
    due_date = _find_text_any(root, ("TerminPlatnosci", "P_6", "P_6A"))
    invoice_number = _find_text_any(root, ("P_2", "NumerFa", "NrFa"))
    currency = _find_text_any(root, ("KodWaluty", "Waluta", "P_13_1W")) or "PLN"
    ksef_number = _find_text_any(root, ("NrKSeF", "NumerKSeF"))

    net_total = _to_decimal(_find_text_any(root, ("P_13_1", "NettoRazem", "WartoscNetto")))
    vat_total = _to_decimal(_find_text_any(root, ("P_14_1", "VatRazem", "KwotaVat")))
    gross_total = _to_decimal(_find_text_any(root, ("P_15", "BruttoRazem", "KwotaNaleznosciOgolem")))

    line_items: list[dict] = []
    for row in _iter_by_localname(root, "FaWiersz"):
        line_items.append(
            {
                "name": _find_text_any(row, ("P_7", "NazwaTowaruUslugi")),
                "quantity": _to_decimal(_find_text_any(row, ("P_8A", "Ilosc"))),
                "unit": _find_text_any(row, ("P_8B", "JednostkaMiary")),
                "unit_price_net": _to_decimal(_find_text_any(row, ("P_9A", "CenaJednostkowa"))),
                "net_amount": _to_decimal(_find_text_any(row, ("P_11", "WartoscNetto"))),
                "vat_rate": _find_text_any(row, ("P_12", "StawkaPodatku")),
                "vat_amount": _to_decimal(_find_text_any(row, ("P_11Vat", "KwotaPodatku"))),
                "gross_amount": _to_decimal(_find_text_any(row, ("P_11Brutto", "WartoscBrutto"))),
            }
        )

    vat_breakdown: list[dict] = []
    for row in _iter_by_localname(root, "P_13_1"):
        parent = row
        vat_breakdown.append(
            {
                "net_amount": _to_decimal(_normalize(parent.text)),
                "vat_amount": _to_decimal(_find_text_any(parent, ("P_14_1",))),
            }
        )

    return {
        "schema": "FA(3)",
        "invoice": {
            "number": invoice_number,
            "ksef_number": ksef_number,
            "issue_date": issue_date,
            "due_date": due_date,
            "currency": currency,
        },
        "seller": {
            "name": seller_name,
            "nip": seller_nip,
        },
        "buyer": {
            "name": buyer_name,
            "nip": buyer_nip,
        },
        "totals": {
            "net": net_total,
            "vat": vat_total,
            "gross": gross_total,
            "vat_breakdown": [x for x in vat_breakdown if x["net_amount"] is not None or x["vat_amount"] is not None],
        },
        "line_items": [
            item
            for item in line_items
            if any(value is not None for value in item.values())
        ],
    }