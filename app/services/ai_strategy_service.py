import json
import re
import math
from typing import Dict, Any, Tuple
import os
import logging
from google import genai
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.schemas import StrategyRequest, StrategyResponse
from app.services.context_manager import get_or_create_context

logger = logging.getLogger("auditra.strategy_orchestrator")

STRATEGY_SYSTEM_PROMPT = """
Jesteś Wirtualnym CFO - głównym doradcą zarządu ds. strategii finansowej.
Prezes symuluje właśnie scenariusz: "{template_name}".

Twarde dane PRZED symulacją:
- Runway: {current_runway}
- Miesięczne Koszty: {current_costs} PLN

Twarde dane PO symulacji (wyliczone przez system):
- Runway: {projected_runway}
- Miesięczne Koszty: {projected_costs} PLN

Oceń tę strategię biznesowo. Zwróć wynik jako CZYSTY JSON (bez formatowania markdown i gadek) zaczynający się od [STRATEGY_JSON] i kończący na [/STRATEGY_JSON]:
[STRATEGY_JSON]
{
  "tl_dr": "Krótka ocena np. 'Zbyt duże ryzyko, runway spada do 2 miesięcy'.",
  "status_color": "green" | "yellow" | "red",
  "action_plan": [
    {"step": 1, "action": "Zabezpiecz gotówkę", "details": "Krótki opis akcji..."}
  ],
  "critical_risk": "Największe zagrożenie tej decyzji, jednym zdaniem."
}
[/STRATEGY_JSON]
"""

class StrategyOrchestrator:
    def __init__(self):
        # The client will automatically pick up GEMINI_API_KEY or GOOGLE_API_KEY from env
        self.client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", os.environ.get("GOOGLE_API_KEY")))

    def _calculate_runway(self, cash: float, monthly_costs: float, monthly_revenue: float) -> str:
        burn_rate = monthly_costs - monthly_revenue
        if burn_rate <= 0:
            return "Nieskończoność" # Firma zarabia więcej niż pali
        
        runway_months = cash / burn_rate
        return f"{round(runway_months, 1)} msc"

    async def simulate_strategy(self, request: StrategyRequest, user: User, db: AsyncSession) -> StrategyResponse:
        # KROK 1 (BASELINE): Pobierz dane finansowe z CompanyContext
        context_record = await get_or_create_context(db, user.company_id)
        data = context_record.context_data or {}
        pulse = data.get("live_financial_pulse") or {}

        current_cash = float(pulse.get("cash_on_hand") or 0.0)
        current_monthly_revenue = float(pulse.get("mrr") or 0.0)
        net_burn = float(pulse.get("net_burn_rate") or 0.0)
        current_monthly_costs = abs(net_burn) + current_monthly_revenue if net_burn else current_monthly_revenue

        # Jeśli brak danych finansowych — zwróć info zamiast fake'owych liczb
        if current_cash == 0 and current_monthly_revenue == 0 and current_monthly_costs == 0:
            return StrategyResponse(
                baseline_runway=0.0,
                projected_runway=0.0,
                current_costs=0.0,
                projected_costs=0.0,
                ai_strategy_json={
                    "tl_dr": "Brak wystarczających danych finansowych w kontekście firmy do precyzyjnej symulacji.",
                    "status_color": "yellow",
                    "action_plan": [{"step": 1, "action": "Uzupełnij dane finansowe", "details": "Przejdź do panelu CFO i zaktualizuj dane firmy (gotówka, przychody, koszty) aby odblokować symulacje strategiczne."}],
                    "critical_risk": "Symulacja niemożliwa bez danych bazowych."
                }
            )

        baseline_burn = current_monthly_costs - current_monthly_revenue
        baseline_runway_val = current_cash / baseline_burn if baseline_burn > 0 else float('inf')

        # KROK 2 (MATH): Kalkulacja wpływu parametrów z formularza
        costs_impact = float(request.parameters.get("cost_increase", 0)) - float(request.parameters.get("cost_decrease", 0))
        revenue_impact = float(request.parameters.get("revenue_increase", 0)) - float(request.parameters.get("revenue_decrease", 0))

        projected_costs = current_monthly_costs + costs_impact
        projected_revenue = current_monthly_revenue + revenue_impact

        projected_burn = projected_costs - projected_revenue
        projected_runway_val = current_cash / projected_burn if projected_burn > 0 else float('inf')

        # Formatowane stringi dla modelu AI
        current_runway_str = self._calculate_runway(current_cash, current_monthly_costs, current_monthly_revenue)
        projected_runway_str = self._calculate_runway(current_cash, projected_costs, projected_revenue)

        # KROK 3 (AI PROMPT)
        prompt = STRATEGY_SYSTEM_PROMPT.replace("{template_name}", request.template_name)
        prompt = prompt.replace("{current_runway}", current_runway_str)
        prompt = prompt.replace("{current_costs}", str(round(current_monthly_costs, 2)))
        prompt = prompt.replace("{projected_runway}", projected_runway_str)
        prompt = prompt.replace("{projected_costs}", str(round(projected_costs, 2)))

        import asyncio
        loop = asyncio.get_event_loop()

        try:
            response = await loop.run_in_executor(None, lambda: self.client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt
            ))
            raw_text = response.text

            # KROK 4 (Regex Parsing)
            match = re.search(r'\[STRATEGY_JSON\](.*?)\[/STRATEGY_JSON\]', raw_text, re.DOTALL | re.IGNORECASE)
            
            if not match:
                raise ValueError("Brak bloku [STRATEGY_JSON] w odpowiedzi AI.")
            
            json_str = match.group(1).strip()
            # Clean up markdown code blocks if accidentally included by Gemini inside the block
            json_str = re.sub(r'```(?:json)?', '', json_str).strip()
            
            ai_strategy_json = json.loads(json_str)

        except Exception as e:
            logger.error(f"Błąd generowania strategii przez AI: {e}", exc_info=True)
            # Zasilenie obozu awaryjnego (np. Model zjadł odpowiedź)
            ai_strategy_json = {
                "tl_dr": "Wystąpił nieoczekiwany problem z obliczeniem doradztwa przez AI. Twoja kalkulacja powiodła się na liczbach.",
                "status_color": "yellow",
                "action_plan": [{"step": 1, "action": "Wymagana weryfikacja manualna", "details": "Niestety agent nie zwrócił spójnego planu uwzględniającego nowe wytyczne."}],
                "critical_risk": "Brak planu ratunkowego wygenerowanego przez sztuczną inteligencję."
            }

        return StrategyResponse(
            baseline_runway=baseline_runway_val,
            projected_runway=projected_runway_val,
            current_costs=current_monthly_costs,
            projected_costs=projected_costs,
            ai_strategy_json=ai_strategy_json
        )

# Global singleton instance
strategy_orchestrator = StrategyOrchestrator()
