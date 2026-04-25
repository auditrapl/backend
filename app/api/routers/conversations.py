import logging
import uuid
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from app.api.deps import get_current_user
from app.auth import verify_token
from app.database import get_db, AsyncSessionLocal
from app.schemas import (
    ConversationCreate, ConversationResponse, ConversationListItem,
    ConversationWithMessages, MessageCreate, MessageResponse,
    CfoRequest, UserResponse,
)
from app.models import Conversation, Message, User
from app.services.agentic_cfo import agentic_cfo_stream
from app.services.context_operational import process_and_upsert_operational
from app.services.conversation_summary import maybe_generate_summary
from app.services.suggested_questions import generate_suggested_questions

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_token)])


# ─── LIST CONVERSATIONS ─────────────────────────────────────────────────────

@router.get("/", response_model=list[ConversationListItem])
async def list_conversations(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lista wątków użytkownika (aktywne, posortowane od najnowszych)."""
    company_id = getattr(current_user, "company_id", None)
    if not company_id:
        raise HTTPException(status_code=400, detail="Brak przypisanej firmy.")

    result = await db.execute(
        select(Conversation)
        .where(
            Conversation.company_id == company_id,
            Conversation.user_id == current_user.id,
            Conversation.is_archived == False,
        )
        .order_by(Conversation.updated_at.desc())
    )
    return result.scalars().all()


# ─── CREATE CONVERSATION ────────────────────────────────────────────────────

@router.post("/", response_model=ConversationResponse, status_code=201)
async def create_conversation(
    body: ConversationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Utwórz nowy wątek rozmowy CFO."""
    company_id = getattr(current_user, "company_id", None)
    if not company_id:
        raise HTTPException(status_code=400, detail="Brak przypisanej firmy.")

    conversation = Conversation(
        company_id=company_id,
        user_id=current_user.id,
        title=body.title,
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return conversation


# ─── GET CONVERSATION WITH MESSAGES ─────────────────────────────────────────

@router.get("/{conversation_id}", response_model=ConversationWithMessages)
async def get_conversation(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pobierz wątek z pełną historią wiadomości."""
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id,
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Nie znaleziono wątku.")

    msgs_result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    messages = msgs_result.scalars().all()

    return ConversationWithMessages(
        id=conversation.id,
        company_id=conversation.company_id,
        user_id=conversation.user_id,
        title=conversation.title,
        summary=conversation.summary,
        message_count=conversation.message_count,
        is_archived=conversation.is_archived,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=messages,
    )


# ─── SEND MESSAGE (SSE STREAM) ──────────────────────────────────────────────

@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    body: MessageCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Wyślij wiadomość do wątku CFO.
    Zapisuje wiadomość użytkownika, uruchamia agentic CFO, streamuje odpowiedź SSE,
    po zakończeniu zapisuje odpowiedź asystenta.
    """
    # Sprawdź czy wątek należy do użytkownika
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id,
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Nie znaleziono wątku.")

    company_id = str(conversation.company_id)

    # Zapisz wiadomość użytkownika
    user_message = Message(
        conversation_id=conversation_id,
        role="user",
        content=body.content,
        metadata_json={"document_ids": body.document_ids} if body.document_ids else None,
    )
    db.add(user_message)
    conversation.message_count += 1

    # Auto-title z pierwszej wiadomości
    if not conversation.title:
        conversation.title = body.content[:80] + ("..." if len(body.content) > 80 else "")

    await db.commit()
    await db.refresh(user_message)

    # Implicit learning w tle
    async def background_chat_upsert(c_id: str, prompt_text: str):
        async with AsyncSessionLocal() as bg_db:
            try:
                await process_and_upsert_operational(bg_db, uuid.UUID(c_id), prompt_text)
            except Exception as ex:
                logger.error(f"Implicit Learning error: {ex}")

    background_tasks.add_task(background_chat_upsert, company_id, body.content)

    # Streamuj odpowiedź CFO i zapisz po zakończeniu
    cfo_request = CfoRequest(prompt=body.content, document_ids=body.document_ids)

    async def event_generator():
        full_response_chunks = []
        last_chunk_data = None

        try:
            async for chunk in agentic_cfo_stream(cfo_request, company_id, conversation_id=str(conversation_id)):
                full_response_chunks.append(chunk)
                last_chunk_data = chunk
                yield f"data: {chunk}\n\n"

                # Save category from triage to conversation (first message only)
                try:
                    import json as _jcat
                    parsed_chunk = _jcat.loads(chunk)
                    if parsed_chunk.get("phase") == "triage" and parsed_chunk.get("category"):
                        async with AsyncSessionLocal() as cat_db:
                            cat_result = await cat_db.execute(
                                select(Conversation).where(Conversation.id == conversation_id)
                            )
                            cat_conv = cat_result.scalar_one_or_none()
                            if cat_conv and not cat_conv.category:
                                cat_conv.category = parsed_chunk["category"]
                                await cat_db.commit()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"CFO stream error: {e}", exc_info=True)
            import json as _jerr
            err_detail = str(e)[:300].replace('"', "'")
            yield f'data: {_jerr.dumps({"status": "error", "message": f"Błąd: {err_detail}"})}\n\n'

        # Zapisz odpowiedź asystenta po zakończeniu streamu
        try:
            import json
            # Zbierz pełną odpowiedź z ostatniego chunka (zawiera final result)
            response_content = ""
            response_metadata = None
            if last_chunk_data:
                try:
                    parsed = json.loads(last_chunk_data)
                    if parsed.get("status") == "done":
                        response_content = parsed.get("executive_summary", "")
                        response_metadata = parsed
                    else:
                        response_content = last_chunk_data
                except (json.JSONDecodeError, TypeError):
                    response_content = last_chunk_data

            async with AsyncSessionLocal() as save_db:
                assistant_message = Message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=response_content or "Odpowiedź CFO",
                    metadata_json=response_metadata,
                )
                save_db.add(assistant_message)

                # Aktualizuj message_count
                conv_result = await save_db.execute(
                    select(Conversation).where(Conversation.id == conversation_id)
                )
                conv = conv_result.scalar_one_or_none()
                if conv:
                    conv.message_count += 1

                await save_db.commit()

            # Auto-summary w tle — fire-and-forget (nie blokuje streama)
            import asyncio as _aio
            _aio.create_task(maybe_generate_summary(conversation_id))

        except Exception as e:
            logger.error(f"Error saving assistant message: {e}", exc_info=True)

        # Suggested questions — generuj i wyślij jako osobny SSE event
        try:
            import json as _json
            suggestions = await generate_suggested_questions(
                user_prompt=body.content,
                assistant_response=response_content or "",
            )
            if suggestions:
                yield f"data: {_json.dumps({'status': 'suggestions', 'questions': suggestions})}\n\n"
        except Exception as e:
            logger.error(f"Suggested questions error: {e}", exc_info=True)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ─── ARCHIVE CONVERSATION ───────────────────────────────────────────────────

@router.delete("/{conversation_id}", status_code=204)
async def archive_conversation(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Archiwizuj wątek (soft delete)."""
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == current_user.id,
        )
    )
    conversation = result.scalar_one_or_none()
    if not conversation:
        raise HTTPException(status_code=404, detail="Nie znaleziono wątku.")

    conversation.is_archived = True
    await db.commit()
