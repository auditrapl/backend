# Stripe Setup (Test + Production)

Ten dokument zamyka operacyjnie Stripe dla Auditra: konto, klucze, CLI, webhooki i szybkie testy.

## 1. Konto Stripe i aktywacja

1. Zaloguj się do Stripe Dashboard i utwórz konto projektu Auditra.
2. Włącz tryb testowy (Test mode) i skonfiguruj dane firmy.
3. Dla produkcji przejdź aktywację konta (KYC, payouts, dane firmy).
4. Utwórz produkt/subskrypcję:
   - Product: `Auditra Pro`
   - Price (test): np. miesięczny `price_test_xxx`
   - Price (prod): miesięczny `price_live_xxx`

## 2. Klucze API i env vars

Wymagane zmienne backend:

- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `STRIPE_DEFAULT_PRICE_ID`
- `FRONTEND_APP_URL`

Wymagana zmienna frontend:

- `NEXT_PUBLIC_STRIPE_PRICE_ID`

Przykład lokalny (`backend/.env`):

```env
STRIPE_SECRET_KEY=sk_test_xxx
STRIPE_WEBHOOK_SECRET=whsec_xxx
STRIPE_DEFAULT_PRICE_ID=price_test_xxx
FRONTEND_APP_URL=http://localhost:3000
```

Przykład lokalny (`frontend/.env.local`):

```env
NEXT_PUBLIC_STRIPE_PRICE_ID=price_test_xxx
```

## 3. Stripe CLI (lokalne testy)

### Instalacja

PowerShell (Windows):

```powershell
winget install Stripe.StripeCLI
```

### Logowanie

```powershell
stripe login
```

### Forward webhooków do lokalnego backendu

Uruchom backend i wykonaj:

```powershell
stripe listen --forward-to http://localhost:8000/billing/webhook
```

CLI zwróci `webhook signing secret` (`whsec_...`).
Wpisz ten sekret do `STRIPE_WEBHOOK_SECRET` w `backend/.env`.

## 4. Webhook w Stripe Dashboard

W dashboardzie dodaj endpoint:

- URL: `https://<twoj-backend>/billing/webhook`

Zdarzenia do subskrypcji:

- `checkout.session.completed`
- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- `invoice.paid`
- `invoice.payment_failed`

Dla test i prod użyj osobnych endpointów i osobnych sekretów `whsec_*`.

## 5. Podpięte endpointy Auditra

Backend posiada gotowe endpointy:

- `POST /billing/checkout-session`
- `POST /billing/portal-session`
- `GET /billing/status`
- `POST /billing/webhook`

Frontend dashboard:

- `/dashboard/platnosci`

## 6. Szybki test e2e (test mode)

1. Zaloguj użytkownika i przejdź do `/dashboard/platnosci`.
2. Kliknij `Aktywuj checkout`.
3. Użyj testowej karty Stripe (np. 4242 4242 4242 4242).
4. Po powrocie na frontend sprawdź:
   - status subskrypcji
   - datę kolejnego pobrania
   - listę faktur PDF
5. Potwierdź w logach backendu, że webhook został obsłużony.

## 7. Produkcja (checklista deploy)

1. Ustaw live klucze (`sk_live_*`, `price_live_*`, `whsec_live_*`) w sekretach środowiska.
2. Ustaw `FRONTEND_APP_URL` na domenę produkcyjną.
3. Ustaw `NEXT_PUBLIC_STRIPE_PRICE_ID` na `price_live_*`.
4. Dodaj webhook produkcyjny do Stripe Dashboard.
5. Zweryfikuj checkout i pojawianie się faktur PDF na produkcji.
