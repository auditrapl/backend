import os
import uuid
from typing import List
from datetime import datetime

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from app.database import get_db
from app.models import Document, CompanyDocument, User
from app.api.deps import get_current_user
from app.services.webdav_service import WebDAVService

router = APIRouter(prefix="/api/documents", tags=["Documents"])
webdav_service = WebDAVService()

MAX_FILE_SIZE = 50 * 1024 * 1024 # 50 MB
ALLOWED_EXTENSIONS = {'.pdf', '.xml', '.csv', '.png', '.jpg'}

class RenameDocumentRequest(BaseModel):
    new_name: str

@router.post("/upload")
async def upload_document(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        if not current_user.company_id:
            raise HTTPException(status_code=400, detail="User is not associated with any company.")

        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, 
                detail=f"Unsupported file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
            )
        
        file_content = await file.read()
        size_bytes = len(file_content)

        if size_bytes > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, 
                detail="File size exceeds the 10 MB limit."
            )
            
        await file.seek(0)
        
        company_id_str = str(current_user.company_id)
        
        try:
            relative_path = await webdav_service.upload_file(file, company_id=company_id_str)
        except Exception as e:
            print(f"[Upload Error] WebDAV upload_file threw: {str(e)}")
            import traceback
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=f"WebDAV Upload failed: {str(e)}")

        display_ext = "PDF" if ext == ".pdf" else "CSV" if ext == ".csv" else "XML KSeF" if ext == ".xml" else ext.upper().replace(".", "")

        new_doc = Document(
            company_id=current_user.company_id,
            filename=file.filename,
            file_type=display_ext,
            filepath=relative_path,
            size_bytes=size_bytes
        )
        
        db.add(new_doc)
        await db.commit()
        await db.refresh(new_doc)
        
        # ─── MAŁY AGENT INDEKSUJĄCY: BackgroundTask (bezpieczniejsze niż create_task) ──
        from fastapi import BackgroundTasks
        from app.services.document_indexer import process_and_save_document
        from app.database import AsyncSessionLocal
        from app.services.context_manager import process_and_upsert_context
        
        bg = BackgroundTasks()
        
        # Stary agent do RAG-a
        bg.add_task(
            process_and_save_document,
            company_id=company_id_str,
            file_bytes=file_content,
            file_name=file.filename,
            file_url=relative_path
        )
        
        # Nowy silnik: Global Corporate Context Memory (Wielki Wybuch / Zamknięcie Miesiąca)
        async def background_context_upsert(c_id: str, f_content: bytes):
            # Musimy powołać nową sesję DB, bo ta żądania wygaśnie po Response
            async with AsyncSessionLocal() as bg_db:
                # Ogranicznik tekstu by nie zadławić Context Buildera
                extracted_text = f_content[:30000].decode("utf-8", errors="ignore") 
                try:
                    import uuid
                    await process_and_upsert_context(bg_db, uuid.UUID(c_id), extracted_text)
                except Exception as ex:
                    print(f"Błąd podczas aktualizacji Global Contextu w tle: {ex}")
                    
        bg.add_task(background_context_upsert, company_id_str, file_content)
        # ────────────────────────────────────────────────────────────────────────
        
        from fastapi.responses import JSONResponse
        response_data = {
            "id": str(new_doc.id),
            "filename": new_doc.filename,
            "size_bytes": new_doc.size_bytes,
            "file_type": new_doc.file_type,
            "upload_date": str(new_doc.upload_date)
        }
        return JSONResponse(content=response_data, background=bg)
    except Exception as e:
        print(f"[Upload Error] Unhandled exception in upload_document: {str(e)}")
        import traceback
        traceback.print_exc()
        raise e


@router.get("/")
async def list_documents(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not current_user.company_id:
        raise HTTPException(status_code=400, detail="User is not associated with any company.")
        
    result = await db.execute(
        select(Document)
        .where(Document.company_id == current_user.company_id)
        .order_by(Document.upload_date.desc())
    )
    docs = result.scalars().all()

    cd_result = await db.execute(
        select(CompanyDocument.file_name, CompanyDocument.document_type)
        .where(CompanyDocument.company_id == current_user.company_id)
    )
    ocr_map = {row.file_name: row.document_type for row in cd_result.all()}

    return [
        {
            "id": str(d.id),
            "name": d.filename,
            "type": d.file_type,
            "ocr_type": ocr_map.get(d.filename),
            "date": d.upload_date.strftime("%d %b %Y"),
            "size": f"{d.size_bytes / 1024 / 1024:.2f} MB" if d.size_bytes > 1024 * 1024 else f"{d.size_bytes / 1024:.0f} KB"
        }
        for d in docs
    ]


@router.get("/{document_id}/download")
async def download_document(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Document).where(Document.id == document_id, Document.company_id == current_user.company_id)
    )
    doc = result.scalar_one_or_none()
    
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found or access denied")
        
    # Return StreamingResponse reading straight from WebDAV
    # Map content-types
    content_types = {
        "PDF": "application/pdf",
        "CSV": "text/csv",
        "XML KSeF": "application/xml"
    }
    
    ctype = content_types.get(doc.file_type, "application/octet-stream")
    
    headers = {
        "Content-Disposition": f'inline; filename="{doc.filename}"'
    }
    
    return StreamingResponse(
        webdav_service.stream_file(doc.filepath), 
        media_type=ctype,
        headers=headers
    )


@router.patch("/{document_id}/rename")
async def rename_document(
    document_id: uuid.UUID,
    payload: RenameDocumentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Document).where(Document.id == document_id, Document.company_id == current_user.company_id)
    )
    doc = result.scalar_one_or_none()
    
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found or access denied")
        
    doc.filename = payload.new_name
    await db.commit()
    
    return {"status": "success", "new_name": doc.filename}


@router.delete("/{document_id}")
async def delete_document(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Document).where(Document.id == document_id, Document.company_id == current_user.company_id)
    )
    doc = result.scalar_one_or_none()
    
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found or access denied")
        
    # Delete from WebDAV
    success = await webdav_service.delete_file(doc.filepath)
    if not success:
        # Depending on policy, maybe we still want to delete DB record, or just raise 500
        pass 
        
    await db.delete(doc)
    await db.commit()
    
    return {"status": "success", "message": "Document deleted permanently."}


@router.get("/{document_id}/ai-summary")
async def get_document_ai_summary(
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Zwraca AI summary z tabeli company_documents dla danego dokumentu.
    Matchowanie po nazwie pliku (files-table record → company_documents record).
    """
    from app.models import CompanyDocument
    
    # Pobierz oryginalny dokument żeby wiedzieć jaka jest nazwa pliku
    result = await db.execute(
        select(Document).where(Document.id == document_id, Document.company_id == current_user.company_id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found or access denied")
    
    # Znajdź rekord CompanyDocument po nazwie pliku (match po file_name)
    ai_result = await db.execute(
        select(CompanyDocument)
        .where(
            CompanyDocument.company_id == current_user.company_id,
            CompanyDocument.file_name == doc.filename
        )
        .order_by(CompanyDocument.created_at.desc())
        .limit(1)
    )
    ai_doc = ai_result.scalar_one_or_none()
    
    if not ai_doc:
        # Agent Indeksujący jeszcze nie przetworzy pliku (fire-and-forget jest w toku)
        return {"status": "processing", "ai_summary": None}
    
    if not ai_doc.ai_summary:
        return {"status": "processing", "ai_summary": None}
    
    return {
        "status": "ready",
        "ai_summary": ai_doc.ai_summary,
        "document_type": ai_doc.document_type,
        "indexed_at": ai_doc.created_at.isoformat() if ai_doc.created_at else None
    }

