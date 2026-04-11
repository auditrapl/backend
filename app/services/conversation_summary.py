"""
Auto-summary wątku CFO.
Po przekroczeniu progu wiadomości (co SUMMARY_EVERY), Gemini Flash
streszcza starsze wiadomości i zapisuje summary w conversations.summary.
Następne zapytania używają summary zamiast pełnej historii.
"""
import os
import uuid
import json
import logging
from google import genai
from google.genai import types
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import Conversation, Message

logger = logging.getLogger(__name__)

# Co ile wiadomości generujemy summary
SUMMARY_EVERY = 10

# Ile najnowszych wiadomości zostawiamy "na wierzchu" (nie streszczamy)
KEEP_RECENT = 6

SUMMARY_PROMPT = """Jesteś asystentem streszczającym rozmowy finansowe CFO AI.

Otrzymasz historię rozmowy między CEO firmy a Wirtualnym CFO (AI). Twoim zadaniem jest:
1. Streszcz KLUCZOWE decyzje, rekomendacje i ustalenia z rozmowy
2. Zachowaj twarde liczby (kwoty, procenty, daty, terminy)
3. Zapisz kontekst potrzebny do kontynuacji rozmowy (np. "CEO rozważa inwestycję 200k PLN w marketing")
4. Pomiń powtórzenia i technikalia (np. "Agent uruchomił SQL query...")

Format: zwięzłe punkty, max 500 słów. Pisz po polsku."""


async def maybe_generate_summary(
    conversation_id: uuid.UUID,
) -> None:
    """
    Sprawdza czy wątek przekroczył próg wiadomości i generuje summary.
    Wywoływana jako background task po zapisaniu odpowiedzi asystenta.
    """
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        try:
            # Pobierz wątek
            conv_result = await db.execute(
                select(Conversation).where(Conversation.id == conversation_id)
            )
            conversation = conv_result.scalar_one_or_none()
            if not conversation:
                return

            # Sprawdź czy pora na summary (co SUMMARY_EVERY wiadomości)
            if conversation.message_count < SUMMARY_EVERY:
                return
            if conversation.message_count % SUMMARY_EVERY != 0:
                return

            logger.info(
                f"[SUMMARY] Generating summary for conversation {str(conversation_id)[:8]}.. "
                f"(message_count={conversation.message_count})"
            )

            # Pobierz WSZYSTKIE wiadomości z wątku
            msgs_result = await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at)
            )
            all_messages = msgs_result.scalars().all()

            if len(all_messages) <= KEEP_RECENT:
                return

            # Wiadomości do streszczenia = wszystkie OPRÓCZ ostatnich KEEP_RECENT
            messages_to_summarize = all_messages[:-KEEP_RECENT]

            # Zbuduj tekst do streszczenia
            existing_summary = conversation.summary or ""
            history_parts = []

            if existing_summary:
                history_parts.append(f"[Poprzednie podsumowanie]\n{existing_summary}\n")

            history_parts.append("[Nowe wiadomości do streszczenia]")
            for msg in messages_to_summarize:
                role_label = {"user": "CEO", "assistant": "CFO AI", "system": "System"}.get(msg.role, msg.role)
                # Skróć bardzo długie wiadomości asystenta (raporty)
                content = msg.content
                if msg.role == "assistant" and len(content) > 1000:
                    content = content[:1000] + "... [skrócono]"
                history_parts.append(f"{role_label}: {content}")

            history_text = "\n".join(history_parts)

            # Wywołaj Gemini Flash
            client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
            response = await client.aio.models.generate_content(
                model="gemini-2.5-flash",
                contents=history_text,
                config=types.GenerateContentConfig(
                    system_instruction=SUMMARY_PROMPT,
                    temperature=0.1,
                    max_output_tokens=1024,
                ),
            )

            new_summary = response.text.strip()
            if new_summary:
                conversation.summary = new_summary
                await db.commit()
                logger.info(
                    f"[SUMMARY] Saved summary ({len(new_summary)} chars) "
                    f"for conversation {str(conversation_id)[:8]}.."
                )

        except Exception as e:
            logger.error(f"[SUMMARY_ERROR] {e}", exc_info=True)
