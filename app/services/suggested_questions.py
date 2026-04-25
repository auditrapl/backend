"""
Suggested questions — generuje 3 follow-up pytania po odpowiedzi CFO AI.
Wyświetlane jako pill buttons w UI.
"""
import os
import json
import logging
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

SUGGESTIONS_PROMPT = """Jesteś asystentem CFO AI. Na podstawie ostatniego pytania CEO i odpowiedzi AI,
zaproponuj dokładnie 3 krótkie, konkretne pytania follow-up, które CEO mógłby chcieć zadać.

Zasady:
- Każde pytanie max 60 znaków
- Pytania muszą być różnorodne (np. pogłębienie, alternatywa, ryzyko)
- Pisz po polsku, w tonie biznesowym

KRYTYCZNE — FORMAT ODPOWIEDZI:
- Zwróć WYŁĄCZNIE surowy JSON, bez żadnej preambuły, wstępu ani markdown fences (```).
- NIE pisz "Here is the JSON" ani "Oto JSON".
- Pierwszym znakiem odpowiedzi MUSI być `{`, ostatnim `}`.
- Schemat: {"questions": ["pytanie 1", "pytanie 2", "pytanie 3"]}"""


class SuggestedQuestionsOutput(BaseModel):
    questions: list[str] = Field(description="Dokładnie 3 pytania follow-up")


async def generate_suggested_questions(
    user_prompt: str,
    assistant_response: str,
) -> list[str]:
    """
    Generuje 3 suggested questions na podstawie ostatniej wymiany.
    Używa Gemini 2.5-Flash (szybki, tani).
    Zwraca pustą listę w razie błędu.
    """
    try:
        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

        # Skróć odpowiedź asystenta żeby nie przepalać tokenów
        short_response = assistant_response[:1500] if len(assistant_response) > 1500 else assistant_response

        content = (
            f"Pytanie CEO: {user_prompt}\n\n"
            f"Odpowiedź CFO AI: {short_response}"
        )

        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=content,
            config=types.GenerateContentConfig(
                system_instruction=SUGGESTIONS_PROMPT,
                response_mime_type="application/json",
                response_schema=SuggestedQuestionsOutput,
                temperature=0.7,
                # 512 zamiast 256 — Gemini czasem zmarnuje tokeny na preambule
                # i skonczy zanim wygeneruje JSON. 512 daje margines bezpieczenstwa.
                max_output_tokens=512,
            ),
        )

        from app.services._json_parse import safe_model_validate_json
        try:
            result = safe_model_validate_json(SuggestedQuestionsOutput, response.text)
        except ValueError as ve:
            # Pusta odpowiedz lub brak JSON — prawdopodobnie Gemini skonczyl tokeny na preambule
            logger.warning(f"[SUGGESTIONS] {ve}")
            return []

        questions = [q[:80] for q in result.questions[:3]]
        logger.info(f"[SUGGESTIONS] Generated {len(questions)} questions")
        return questions

    except Exception as e:
        logger.error(f"[SUGGESTIONS_ERROR] {e}", exc_info=True)
        return []
