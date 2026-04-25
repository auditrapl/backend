# Dane testowe Auditra — TechFlow Sp. z o.o.

Zakres: **1 luty — 16 kwietnia 2026** (3 miesiące) | Saldo startowe: **185 000 PLN**

## Pliki

### `invoices.json`
**62 faktury** (56 opłaconych, 6 nieopłaconych)
- 18 przychodowych (subskrypcje SaaS od 6 klientów)
- 44 kosztowych (developerzy B2B, Google Ads, hosting, oprogramowanie, czynsz, leasing, jednorazowe)

Każda faktura ma: `id`, `internal_number`, `contractor_name`, `contractor_nip`, `contractor_account`, kwoty netto/VAT/brutto, daty wystawienia/płatności/terminu, `invoice_type`, `is_paid`, `category_name`, `department_id` (zawsze `null` — test halucynacji oddziałów).

### `bank_transfers.csv`
**69 przelewów** (format mBank, separator `;`)
- **56** to przelewy odpowiadające opłaconym fakturom (1:1 z JSON)
- **13** to transakcje **pozafakturowe** — pensje UoP, ZUS, VAT/CIT, prowizje, zaliczka od nowego leada

Kolumny: `Data operacji`, `Data ksiegowania`, `Typ`, `Kontrahent`, `NIP`, `Numer konta`, `Tytul`, `Opis`, `Kwota`, `Waluta`, `Saldo po operacji`.

---

## Sanity check — co powinien zobaczyć AI

| Metryka | Wartość |
|---------|---------|
| SafeCash (z opłaconych faktur) | **287 504 PLN** |
| Saldo bankowe (z CSV, na 16.04) | **396 606 PLN** |
| Różnica (pozafakturowe) | **+109 102 PLN** |
| MRR (recurring income) | ~210 000 PLN |
| Top 1 klient | Alpha — 104 550 PLN/mies (40% MRR) ⚠ koncentracja |

**To jest idealny test problemu Cashflow v2.** SafeCash ≠ Bank Balance. AI powinno to wykryć i zaproponować zadeklarowanie salda.

---

## Ukryte problemy dla AI do wykrycia

### 🔴 Tax Eye powinien wyłapać

1. **Google Ireland Limited (3 faktury × ~1380 PLN VAT)**
   - Zagraniczny dostawca naliczył polski VAT (23%)
   - Zgodnie z Art. 28b ustawy o VAT powinno być reverse charge (0%)
   - Nie da się odliczyć ~4 140 PLN VAT
   - Flag: `note_vat_issue: True` w generate.py

2. **Koszty developerskie B2B (faktury 5 kontraktorów × 3 miesiące = 15 faktur)**
   - Firma ma profil R&D (software house)
   - Potencjalnie kwalifikują się pod **IP Box (Art. 24d ust. 1 ustawy o CIT)**
   - Miesięcznie ~77k PLN kosztów dev → ~130k/rok oszczędności na CIT

3. **Premium Catering — "Reprezentacja"**
   - Faktura za catering na event firmowy (1626 PLN + VAT)
   - Kategoria: "Reprezentacja" → 0% KUP (Art. 16 ust. 1 pkt 28 CIT)
   - Jeśli to był event marketingowy → można zakwalifikować jako "Reklama" → 100% KUP

4. **MacBook Pro 13 008 PLN — amortyzacja jednorazowa**
   - Mały podatnik może skorzystać z Art. 16k ust. 7 CIT
   - Kwota < 50k EUR → jednorazowa amortyzacja zamiast rozłożenia

5. **Leasing Volkswagen Financial**
   - Faktura za leasing samochodu z 23% VAT
   - Sprawdzić: 50% czy 100% odliczenia VAT (Art. 86a)
   - Wymaga ewidencji przebiegu (VAT-26) dla 100%

### 🟡 CFO Radar powinien zaproponować strategiczne

6. **Jan Kowalski — JDG prezesa**
   - Prezes fakturuje firmę przez własne JDG (12k/mies)
   - Ryzyko rekwalifikacji wg Testu Przedsiębiorcy (Art. 5b ust. 1 PIT)
   - 5 kontrahentów B2B regularnie → długotrwały stosunek jak UoP

7. **Koncentracja na kliencie Alpha**
   - 40% MRR z jednego klienta (104 550 PLN / 258 300 PLN)
   - Top 3 razem = 75% MRR
   - Ryzyko biznesowe

8. **Fundacja rodzinna / Holding**
   - Sp. z o.o., przychody ~2.5M/rok, rosnące
   - Jednoosobowa struktura (Jan Kowalski fakturujący przez JDG)
   - Przy CIT estońskim lub fundacji rodzinnej — strukturalne oszczędności

### 🔵 Cashflow / operacyjne

9. **6 faktur nieopłaconych na 16.04:**
   - Beta Commerce — FV/2026/04/002 (55 350 PLN, overdue 1 dzień)
   - Delta Logistics — FV/2026/04/004 (27 060 PLN, overdue 1 dzień)
   - Zeta Ventures — FV/2026/04/006 (14 760 PLN, issued 10.04, jeszcze nie wymagalna)
   - 1 faktura kosztowa Tech Masters (kwiecień, nieopłacona)
   - 2 inne drobne koszty kwietniowe

10. **Pilne obowiązki podatkowe (do 25.04)**
    - VAT za marzec: ~24 000 PLN (nieopłacony jeszcze w CSV)
    - Zaliczka CIT Q1: została zapłacona w marcu (w CSV)

11. **Brak przypisanych oddziałów do wszystkich faktur**
    - `department_id: null` dla każdej
    - AI NIE powinno halucynować donut chart "R&D 56% / Marketing 20%"
    - Powinno pokazać empty state: "Przypisz oddziały aby zobaczyć strukturę"

---

## Wzorce do testu Proactive Insights

### Tax Eye per faktura (trigger na pojedynczych)
- Google Ads → `vat_foreign_supplier_incorrect` 
- Faktura developerska (SoftDev, CodeBase, etc.) → `ip_box_qualified_cost`
- MacBook Pro → `one_time_depreciation`
- Leasing VW → `vat_vehicle_check_100_vs_50`
- Premium Catering → `representation_vs_advertising`

### CFO Radar (trigger na agregacji)
- 12+ faktur R&D/dev total 230k → `ip_box_pattern_strong`
- Jan Kowalski JDG wzorzec → `b2b_rekwalifikacja_risk`
- Alpha 40% MRR → `customer_concentration_risk`
- Rosnący MRR (+4.6% MoM) + mały podatnik → `estonian_cit_evaluation`

---

## Jak tego użyć w testach

### Test 1 — Onboarding import flow
1. Utwórz pustą firmę TechFlow z NIP 5272958441
2. Upload `invoices.json` (lub konwertuj na 62 pojedyncze PDF)
3. Upload `bank_transfers.csv`
4. Sprawdź Ekran 4 onboardingu — czy hipotezy są trafne?

### Test 2 — Cashflow Context v2 (discrepancy)
1. Po imporcie: SafeCash z snapshot = 287 504
2. Saldo bankowe z CSV = 396 606
3. Różnica 109k → AI powinno wygenerować discrepancy alert
4. User deklaruje saldo 396 606 → primary_source zmienia się z `safecash` na `declared`

### Test 3 — Tax Eye accuracy
Po analizie wszystkich faktur sprawdź czy AI znalazło:
- [ ] Google Ads VAT issue (3 faktury)
- [ ] IP Box na fakturach developerskich (5 kontraktorów × 3 msc)
- [ ] Reprezentacja vs reklama (Premium Catering)
- [ ] Amortyzacja jednorazowa (MacBook)
- [ ] VAT samochód 50% vs 100% (leasing)

Target: >80% trafności, max 1-2 false positives.

### Test 4 — CFO Radar strategic
Po odpaleniu nocnego scana sprawdź czy wygenerował:
- [ ] Alert koncentracji klientów (Alpha 40%)
- [ ] Hipotezę IP Box (agregacja 15 faktur)
- [ ] Ryzyko rekwalifikacji JDG prezesa
- [ ] Propozycję ewaluacji CIT estońskiego

---

## Notatki techniczne

- Wszystkie kwoty w **Decimal** (dokładność 2 miejsca)
- Daty w formacie **ISO 8601** (YYYY-MM-DD)
- Numery faktur: `FV/2026/MM/NNN` (sekwencja per miesiąc)
- NIP-y wymyślone (nie koliduje z rzeczywistymi firmami — `1234567890`, `2345678901`, etc.)
- Konta IBAN w formacie polskim (26 cyfr, spacje co 4)
- Pozafakturowe przelewy mają pusty `NIP` dla osób fizycznych (pensje)

---

Generator: `generate.py` (deterministyczny, można regenerować)
