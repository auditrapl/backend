"""
Prompt assembler dla ciągłego czatu CFO.
Składa kontekst rozmowy z: summary starszych wiadomości + ostatnie N wiadomości.
Wstrzykiwany jako dodatkowy blok w system instruction agentów.
"""
import uuid
import logging
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import Conversation, Message

logger = logging.getLogger(__name__)

# Ile ostatnich wiadomości zawsze przekazujemy w pełni
RECENT_MESSAGES_LIMIT = 20


async def get_conversation_history_text(
    db: AsyncSession,
    conversation_id: uuid.UUID,
) -> Optional[str]:
    """
    Buduje blok tekstowy z historią rozmowy do wstrzyknięcia w prompt.
    Struktura:
      - Summary starszych wiadomości (jeśli istnieje)
      - Ostatnie N wiadomości w pełni

    Zwraca None jeśli wątek jest nowy (0 wiadomości).
    """
    # Pobierz wątek z summary
    conv_result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    conversation = conv_result.scalar_one_or_none()
    if not conversation:
        return None

    # Pobierz ostatnie wiadomości
    msgs_result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(RECENT_MESSAGES_LIMIT)
    )
    messages = list(reversed(msgs_result.scalars().all()))

    if not messages:
        return None

    lines = ["== HISTORIA ROZMOWY =="]

    # Wstaw summary starszych wiadomości
    if conversation.summary:
        lines.append("[Podsumowanie wcześniejszych wiadomości]")
        lines.append(conversation.summary)
        lines.append("")

    # Ostatnie wiadomości
    lines.append(f"[Ostatnie {len(messages)} wiadomości]")
    for msg in messages:
        role_label = {
            "user": "CEO",
            "assistant": "CFO AI",
            "system": "System",
        }.get(msg.role, msg.role)

        lines.append(f"{role_label}: {msg.content}")

    return "\n".join(lines)


def build_conversation_system_addendum(history_text: Optional[str]) -> str:
    """
    Tworzy blok do dołączenia do system instruction agentów CFO.
    Jeśli brak historii — zwraca pusty string (nowa rozmowa).
    """
    if not history_text:
        return ""

    return (
        "\n\n"
        "─── KONTEKST KONWERSACJI ───\n"
        "Poniżej znajduje się historia Twojej rozmowy z CEO tej firmy. "
        "Odwołuj się do wcześniejszych wątków, utrzymuj spójność z poprzednimi rekomendacjami "
        "i buduj na tym, co już zostało omówione. Nie powtarzaj analiz, które już przeprowadziłeś, "
        "chyba że CEO wprost o to prosi.\n\n"
        f"{history_text}\n"
        "─── KONIEC KONTEKSTU KONWERSACJI ───"
    )
