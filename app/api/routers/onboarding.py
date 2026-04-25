"""
Router onboardingu — 5-ekranowy wizard.
Endpoints dla: NIP lookup, confirm company, AI wizard chat, invoice upload,
hypothesis generation, hypothesis confirmation, finalize.
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.auth import verify_token
from app.api.deps import get_current_user
from app.database import get_db, AsyncSessionLocal
from app.models import Company, User, CompanyContextModel, Invoice
from app.schemas import InvoiceCreate
from app.services.gus_lookup import lookup_nip, NIPLookupResult
from app.services.context_canonical import (
    CanonicalContext, CompanyIdentity, TaxRegime, BusinessModelDetail,
    FieldSource, merge_canonical,
)
from app.services.context_operational import (
    OperationalContext, refresh_safecash_background,
)
from app.services.onboarding_wizard_ai import (
    wizard_conversation_stream, wizard_extract_structured, WizardExtractionResult,
)
from app.services.onboarding_hypotheses import (
    generate_onboarding_facts, generate_onboarding_hypotheses,
    OnboardingReview, OnboardingHypothesis,
)
from app.services.audit_service import AuditService
from app.services.fa3_parser import parse_fa3_xml

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(verify_token)])


# ==============================================================================
# REQUEST / RESPONSE SCHEMAS
# ==============================================================================

class NIPLookupRequest(BaseModel):
    nip: str


class ConfirmCompanyRequest(BaseModel):
    nip: str
    name: str
    first_name: str
    last_name: str
    legal_form: Optional[str] = None
    address: Optional[str] = None
    pkd_code: Optional[str] = None
    pkd_description: Optional[str] = None
    vat_status: Optional[str] = None


class ConfirmCompanyResponse(BaseModel):
    company_id: str
    onboarding_status: str


class WizardMessageRequest(BaseModel):
    message: str
    conversation_history: list[dict] = []


class WizardCompleteRequest(BaseModel):
    extracted_data: dict


class ConfirmHypothesesRequest(BaseModel):
    confirmed: list[str] = []  # hypothesis IDs
    rejected: list[str] = []


class OnboardingStatusResponse(BaseModel):
    onboarding_status: str
    company_id: Optional[str] = None


# ==============================================================================
# EKRAN 1 — NIP LOOKUP
# ==============================================================================

@router.post("/lookup-nip", response_model=NIPLookupResult)
async def onboarding_lookup_nip(
    body: NIPLookupRequest,
    current_user: User = Depends(get_current_user),
):
    """GUS REGON + VAT Whitelist lookup po NIP."""
    result = await lookup_nip(body.nip)
    return result


@router.post("/confirm-company", response_model=ConfirmCompanyResponse)
async def onboarding_confirm_company(
    body: ConfirmCompanyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Potwierdź dane firmy z GUS i utwórz/zaktualizuj Company.
    Zamienia placeholder NIP (PENDING-xxx) na prawdziwy.
    """
    # Sprawdź czy NIP nie jest już zajęty przez inną firmę
    existing = await db.execute(
        select(Company).where(Company.nip == body.nip)
    )
    existing_company = existing.scalar_one_or_none()
    if existing_company and existing_company.id != (current_user.company_id or None):
        raise HTTPException(status_code=400, detail="Firma o tym NIP już istnieje w systemie.")

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if current_user.company_id:
        # User ma już firmę (placeholder z provisioning) → aktualizuj
        result = await db.execute(
            select(Company).where(Company.id == current_user.company_id)
        )
        company = result.scalar_one_or_none()
        if not company:
            raise HTTPException(status_code=404, detail="Firma użytkownika nie znaleziona.")

        company.nip = body.nip
        company.name = body.name
        company.legal_form = body.legal_form
        company.address = body.address
        company.pkd_code = body.pkd_code
        company.pkd_description = body.pkd_description
        company.vat_status = body.vat_status
        company.onboarding_status = "pending_wizard"
    else:
        # Nowa firma
        company = Company(
            name=body.name,
            nip=body.nip,
            legal_form=body.legal_form,
            address=body.address,
            pkd_code=body.pkd_code,
            pkd_description=body.pkd_description,
            vat_status=body.vat_status,
            onboarding_status="pending_wizard",
        )
        db.add(company)
        await db.flush()
        current_user.company_id = company.id

    # Update user info
    current_user.first_name = body.first_name
    current_user.last_name = body.last_name

    await db.commit()
    await db.refresh(company)

    # Init L1 Canonical Context z GUS data
    try:
        field_sources = {}
        if body.address or body.pkd_code:
            field_sources["company_identity"] = FieldSource(
                origin="gus_api", updated_at=now.isoformat()
            )
        if body.vat_status:
            field_sources["tax_regime.vat_status"] = FieldSource(
                origin="vat_whitelist", updated_at=now.isoformat()
            )

        vat_label = None
        if body.vat_status:
            vat_lower = body.vat_status.lower()
            if "czynny" in vat_lower:
                vat_label = "Czynny podatnik VAT"
            elif "zwolniony" in vat_lower:
                vat_label = "Zwolniony z VAT"

        initial_canonical = CanonicalContext(
            company_identity=CompanyIdentity(
                name=body.name,
                nip=body.nip,
                legal_form=body.legal_form,
                vat_status=vat_label,
            ),
            tax_regime=TaxRegime(
                vat_rate="23%" if vat_label and "Czynny" in vat_label else "ZW",
                jpk_obligation=vat_label is not None and "Czynny" in (vat_label or ""),
                vat_frequency="monthly",
            ),
            business_model=BusinessModelDetail(
                industry=body.pkd_description,
            ),
            field_sources=field_sources,
        )

        # Upsert context
        ctx_result = await db.execute(
            select(CompanyContextModel).where(CompanyContextModel.company_id == company.id)
        )
        ctx = ctx_result.scalar_one_or_none()
        if ctx:
            ctx.canonical_data = initial_canonical.model_dump()
            ctx.canonical_updated_at = now
        else:
            ctx = CompanyContextModel(
                company_id=company.id,
                context_data={},
                canonical_data=initial_canonical.model_dump(),
                canonical_updated_at=now,
            )
            db.add(ctx)

        await db.commit()
        logger.info(f"L1 initialized from GUS for company {company.id}")
    except Exception as e:
        logger.error(f"Failed to init L1 from GUS: {e}")

    return ConfirmCompanyResponse(
        company_id=str(company.id),
        onboarding_status="pending_wizard",
    )


# ==============================================================================
# EKRAN 2 — AI WIZARD
# ==============================================================================

@router.post("/wizard-message")
async def onboarding_wizard_message(
    body: WizardMessageRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    SSE stream: dwa równoległe calle AI.
    1. Conversation stream (prowadzi dialog)
    2. Structured extraction (wyciąga dane)
    """
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="Brak firmy.")

    # Pobierz kontekst firmy dla conversation AI
    ctx_result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == current_user.company_id)
    )
    ctx = ctx_result.scalar_one_or_none()
    company_context = ctx.canonical_data if ctx and ctx.canonical_data else {}

    import asyncio

    async def event_generator():
        try:
            # Start extraction task in parallel
            extraction_task = asyncio.create_task(
                wizard_extract_structured(body.message, body.conversation_history)
            )

            # Stream conversation — detekcja markera [DONE] i filtrowanie go ze streamu
            full_text = ""
            complete_signalled = False
            async for delta in wizard_conversation_stream(
                body.message, body.conversation_history, company_context
            ):
                full_text += delta
                # Usuń marker [DONE] z tekstu widocznego dla usera
                clean_delta = delta.replace("[DONE]", "").replace("[done]", "")
                if clean_delta:
                    yield f"data: {json.dumps({'type': 'conversation', 'delta': clean_delta}, ensure_ascii=False)}\n\n"
                if not complete_signalled and ("[DONE]" in full_text or "[done]" in full_text):
                    complete_signalled = True

            # Wait for extraction to complete
            try:
                extraction = await extraction_task
                extracted = extraction.model_dump(exclude_none=True)
                yield f"data: {json.dumps({'type': 'extraction', 'data': extracted}, ensure_ascii=False)}\n\n"

                # Fallback: jeśli AI nie oznaczyło [DONE], ale mamy >= 5 pól — też sygnalizuj complete
                if not complete_signalled and len(extracted) >= 5:
                    complete_signalled = True
            except Exception as e:
                logger.error(f"Extraction failed: {e}")
                yield f"data: {json.dumps({'type': 'extraction', 'data': {}})}\n\n"

            if complete_signalled:
                yield f"data: {json.dumps({'type': 'complete'})}\n\n"

            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        except Exception as e:
            logger.error(f"Wizard stream error: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)[:200]})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/wizard-complete")
async def onboarding_wizard_complete(
    body: WizardCompleteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Merge extracted wizard data into L1 canonical context."""
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="Brak firmy.")

    company_id = current_user.company_id
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    data = body.extracted_data

    # Build canonical update from extracted data
    wizard_canonical = CanonicalContext(
        company_identity=CompanyIdentity(
            accounting_method=data.get("accounting_method"),
        ),
        tax_regime=TaxRegime(
            taxation_form=data.get("taxation_form"),
            employee_count=data.get("employee_count"),
            zus_type=data.get("zus_type"),
        ),
        business_model=BusinessModelDetail(
            segment=data.get("segment"),
            model_type=data.get("model_type"),
            revenue_model=data.get("revenue_model"),
            industry=data.get("industry"),
        ),
        field_sources={
            k: FieldSource(origin="ai_wizard_extracted", updated_at=now.isoformat())
            for k in data.keys()
            if data[k] is not None
        },
    )

    # Merge into existing L1
    ctx_result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == company_id)
    )
    ctx = ctx_result.scalar_one_or_none()

    if ctx and ctx.canonical_data:
        existing = CanonicalContext.model_validate(ctx.canonical_data)
        merged = merge_canonical(existing, wizard_canonical)
        ctx.canonical_data = merged.model_dump()
        ctx.canonical_updated_at = now
    elif ctx:
        ctx.canonical_data = wizard_canonical.model_dump()
        ctx.canonical_updated_at = now
    else:
        ctx = CompanyContextModel(
            company_id=company_id,
            context_data={},
            canonical_data=wizard_canonical.model_dump(),
            canonical_updated_at=now,
        )
        db.add(ctx)

    # Update company fields too
    company_result = await db.execute(select(Company).where(Company.id == company_id))
    company = company_result.scalar_one_or_none()
    if company:
        if data.get("taxation_form"):
            company.taxation_form = data["taxation_form"]
        if data.get("employee_count") is not None:
            company.employee_count = data["employee_count"]
        if data.get("zus_type"):
            company.zus_type = data["zus_type"]
        if data.get("industry"):
            company.industry = data["industry"]
        company.onboarding_status = "pending_invoices"

    await db.commit()

    return {"onboarding_status": "pending_invoices"}


# ==============================================================================
# EKRAN 3 — IMPORT FAKTUR
# ==============================================================================

@router.post("/upload-invoices")
async def onboarding_upload_invoices(
    files: list[UploadFile] = File(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload i parsowanie faktur. Obsługuje XML (KSeF), PDF, CSV, ZIP."""
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="Brak firmy.")

    company_id = current_user.company_id
    uploaded = []
    errors = []

    for file in files:
        try:
            content = await file.read()
            filename = file.filename or "unknown"
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

            if ext == "zip":
                # Rozpakuj ZIP i przetwórz zawartość
                import zipfile
                import io
                with zipfile.ZipFile(io.BytesIO(content)) as zf:
                    for name in zf.namelist():
                        inner_ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                        if inner_ext in ("xml", "pdf", "csv", "xlsx"):
                            inner_content = zf.read(name)
                            inv = await _process_single_file(
                                name, inner_ext, inner_content,
                                company_id, current_user.id, db, background_tasks
                            )
                            if inv:
                                uploaded.append(str(inv.id))
            else:
                inv = await _process_single_file(
                    filename, ext, content,
                    company_id, current_user.id, db, background_tasks
                )
                if inv:
                    uploaded.append(str(inv.id))

        except Exception as e:
            logger.error(f"Failed to process {file.filename}: {e}")
            errors.append({"file": file.filename, "error": str(e)[:200]})

    # Update onboarding status
    company_result = await db.execute(select(Company).where(Company.id == company_id))
    company = company_result.scalar_one_or_none()
    if company:
        company.onboarding_status = "pending_review"
        await db.commit()

    # SafeCash refresh (once after all uploads)
    if uploaded:
        background_tasks.add_task(refresh_safecash_background, str(company_id))

    return {
        "uploaded": len(uploaded),
        "invoice_ids": uploaded,
        "errors": errors,
        "onboarding_status": "pending_review",
    }


async def _process_single_file(
    filename: str,
    ext: str,
    content: bytes,
    company_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
    background_tasks: BackgroundTasks,
) -> Invoice | None:
    """Przetwórz pojedynczy plik na fakturę."""
    invoice_data = {
        "filename": filename,
        "company_id": company_id,
        "user_id": user_id,
    }

    if ext == "xml":
        # KSeF FA(3) XML
        xml_text = content.decode("utf-8", errors="replace")
        try:
            parsed = parse_fa3_xml(xml_text)
            invoice_data["ksef_xml"] = xml_text
            invoice_data["parsed_data"] = parsed
            invoice_data["ksef_number"] = parsed["invoice"].get("ksef_number")
            invoice_data["internal_number"] = parsed["invoice"].get("number")
            invoice_data["contractor_name"] = parsed["seller"].get("name")
            invoice_data["contractor_nip"] = parsed["seller"].get("nip")
            invoice_data["currency"] = parsed["invoice"].get("currency")
            invoice_data["net_amount"] = parsed["totals"].get("net")
            invoice_data["vat_amount"] = parsed["totals"].get("vat")
            invoice_data["gross_amount"] = parsed["totals"].get("gross")
        except Exception as e:
            logger.warning(f"XML parse failed for {filename}: {e}")
            invoice_data["status"] = "parse_error"

    elif ext == "csv":
        # CSV — zapisz raw, background audit przetworzy
        invoice_data["status"] = "pending_parse"
        invoice_data["parsed_data"] = {"raw_csv_preview": content.decode("utf-8", errors="replace")[:2000]}

    elif ext == "pdf":
        # PDF — zapisz, background audit z Gemini Vision przetworzy
        invoice_data["status"] = "pending_parse"

    else:
        return None  # Unsupported format

    inv = Invoice(**invoice_data)
    db.add(inv)
    await db.commit()
    await db.refresh(inv)

    # Background audit
    background_tasks.add_task(AuditService.run_background_audit, inv.id)

    return inv


# ==============================================================================
# EKRAN 4 — FAKTY + HIPOTEZY
# ==============================================================================

@router.post("/generate-hypotheses", response_model=OnboardingReview)
async def onboarding_generate_hypotheses(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generuj fakty z InvoiceMetrics + hipotezy z AI."""
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="Brak firmy.")

    company_id = current_user.company_id

    # Get canonical context
    ctx_result = await db.execute(
        select(CompanyContextModel).where(CompanyContextModel.company_id == company_id)
    )
    ctx = ctx_result.scalar_one_or_none()
    canonical = CanonicalContext()
    if ctx and ctx.canonical_data:
        canonical = CanonicalContext.model_validate(ctx.canonical_data)

    # Generate facts and hypotheses
    facts = await generate_onboarding_facts(db, company_id)
    hypotheses = await generate_onboarding_hypotheses(db, company_id, canonical)

    return OnboardingReview(facts=facts, hypotheses=hypotheses)


@router.post("/confirm-hypotheses")
async def onboarding_confirm_hypotheses(
    body: ConfirmHypothesesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Merge potwierdzone hipotezy do L1/L2 context."""
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="Brak firmy.")

    # Status update (nawet jeśli brak hipotez do potwierdzenia)
    company_result = await db.execute(
        select(Company).where(Company.id == current_user.company_id)
    )
    company = company_result.scalar_one_or_none()
    if company:
        company.onboarding_status = "pending_finalize"
        await db.commit()

    return {"onboarding_status": "pending_finalize", "confirmed": len(body.confirmed)}


# ==============================================================================
# EKRAN 5 — FINALIZE
# ==============================================================================

@router.post("/finalize")
async def onboarding_finalize(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Odpal Morning Briefing natychmiast + ustaw status complete."""
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="Brak firmy.")

    company_id = current_user.company_id

    # Set status complete
    company_result = await db.execute(
        select(Company).where(Company.id == company_id)
    )
    company = company_result.scalar_one_or_none()
    if company:
        company.onboarding_status = "complete"
        await db.commit()

    return {"status": "ready", "onboarding_status": "complete"}


# ==============================================================================
# STATUS
# ==============================================================================

@router.get("/status", response_model=OnboardingStatusResponse)
async def onboarding_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Pobierz aktualny status onboardingu."""
    if not current_user.company_id:
        return OnboardingStatusResponse(onboarding_status="pending_nip")

    result = await db.execute(
        select(Company).where(Company.id == current_user.company_id)
    )
    company = result.scalar_one_or_none()
    if not company:
        return OnboardingStatusResponse(onboarding_status="pending_nip")

    return OnboardingStatusResponse(
        onboarding_status=company.onboarding_status,
        company_id=str(company.id),
    )
