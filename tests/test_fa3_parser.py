from decimal import Decimal

import pytest

from app.services.fa3_parser import parse_fa3_xml


SAMPLE_FA3_XML = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Faktura xmlns=\"http://crd.gov.pl/wzor/2023/06/29/12648/\">
  <Podmiot1>
    <DaneIdentyfikacyjne>
      <NIP>1112223344</NIP>
      <Nazwa>Sprzedawca Sp. z o.o.</Nazwa>
    </DaneIdentyfikacyjne>
  </Podmiot1>
  <Podmiot2>
    <DaneIdentyfikacyjne>
      <NIP>5556667788</NIP>
      <Nazwa>Kupujacy SA</Nazwa>
    </DaneIdentyfikacyjne>
  </Podmiot2>
  <Fa>
    <P_1>2026-04-10</P_1>
    <P_2>FV/04/2026/1</P_2>
    <P_13_1>100.00</P_13_1>
    <P_14_1>23.00</P_14_1>
    <P_15>123.00</P_15>
  </Fa>
  <FaWiersz>
    <P_7>Usluga abonamentowa</P_7>
    <P_8A>1</P_8A>
    <P_8B>szt</P_8B>
    <P_9A>100.00</P_9A>
    <P_11>100.00</P_11>
    <P_12>23</P_12>
  </FaWiersz>
</Faktura>
"""


def test_parse_fa3_xml_returns_structured_payload():
    parsed = parse_fa3_xml(SAMPLE_FA3_XML)

    assert parsed["schema"] == "FA(3)"
    assert parsed["invoice"]["number"] == "FV/04/2026/1"
    assert parsed["invoice"]["issue_date"] == "2026-04-10"
    assert parsed["seller"]["name"] == "Sprzedawca Sp. z o.o."
    assert parsed["seller"]["nip"] == "1112223344"
    assert parsed["totals"]["net"] == Decimal("100.00")
    assert parsed["totals"]["vat"] == Decimal("23.00")
    assert parsed["totals"]["gross"] == Decimal("123.00")
    assert len(parsed["line_items"]) == 1


def test_parse_fa3_xml_rejects_invalid_payload():
    with pytest.raises(ValueError):
        parse_fa3_xml("<not_invoice></not_invoice>")
