"""Upload & parsing API routes."""
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.config import get_settings
from app.core.security import encrypt_bytes
from app.models.models import Upload, Transaction, SourceType
from app.schemas.schemas import UploadOut
from app.parsers.base import BaseParser
from app.parsers.icici_parser import ICICIBankParser
from app.parsers.hdfc_parser import HDFCBankParser
from app.parsers.sbi_parser import SBIBankParser
from app.parsers.bob_parser import BankOfBarodaParser
from app.parsers.phonepe_parser import PhonePeParser
from app.parsers.gpay_parser import GooglePayParser
from app.services.reconciliation import reconcile_all, detect_self_transfers
from app.services.categorization import categorize_all_uncategorized
from app.services.budget_alerts import check_budgets_and_alert

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

PARSERS: dict[SourceType, BaseParser] = {
    SourceType.ICICI_BANK: ICICIBankParser(),
    SourceType.HDFC_BANK: HDFCBankParser(),
    SourceType.SBI_BANK: SBIBankParser(),
    SourceType.BOB_BANK: BankOfBarodaParser(),
    SourceType.PHONEPE: PhonePeParser(),
    SourceType.GOOGLEPAY: GooglePayParser(),
}


@router.post("/", response_model=UploadOut)
async def upload_statement(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    source_type: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Upload a bank statement or UPI app export. Parses, reconciles, and categorizes."""
    settings = get_settings()

    # Validate source type
    try:
        src = SourceType(source_type)
    except ValueError:
        raise HTTPException(400, f"Invalid source_type. Must be one of: {[s.value for s in SourceType]}")

    if src not in PARSERS:
        raise HTTPException(400, f"No parser available for {src.value}")

    # Read file
    file_bytes = await file.read()
    if len(file_bytes) > settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise HTTPException(413, f"File too large. Max {settings.MAX_UPLOAD_SIZE_MB}MB")

    # Duplicate check (skip in dev mode)
    file_hash = BaseParser.file_hash(file_bytes)
    if not settings.DEBUG:
        from sqlalchemy import select
        existing = await db.execute(select(Upload).where(Upload.file_hash == file_hash))
        if existing.scalar_one_or_none():
            raise HTTPException(409, "This file has already been uploaded (duplicate hash)")

    # Save encrypted file
    os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
    enc_filename = f"{uuid.uuid4().hex}.enc"
    enc_path = os.path.join(settings.UPLOAD_DIR, enc_filename)
    try:
        encrypted = encrypt_bytes(file_bytes)
        with open(enc_path, "wb") as f:
            f.write(encrypted)
    except RuntimeError:
        # Encryption key not set — save without encryption in dev
        enc_path = os.path.join(settings.UPLOAD_DIR, f"{uuid.uuid4().hex}_{file.filename}")
        with open(enc_path, "wb") as f:
            f.write(file_bytes)

    # Parse
    parser = PARSERS[src]
    result = parser.parse(file_bytes, file.filename)

    # Create upload record
    upload = Upload(
        filename=file.filename,
        source_type=src,
        encrypted_path=enc_path,
        file_hash=file_hash,
        row_count=result.row_count,
        uploaded_at=datetime.utcnow(),
        parsed_at=datetime.utcnow() if result.transactions else None,
        parse_errors={"errors": result.errors} if result.errors else None,
    )
    db.add(upload)
    await db.flush()

    # Insert transactions
    for ptxn in result.transactions:
        txn = Transaction(
            upload_id=upload.id,
            txn_date=ptxn.txn_date,
            txn_datetime=ptxn.txn_datetime,
            amount=ptxn.amount,
            is_debit=ptxn.is_debit,
            balance_after=ptxn.balance_after,
            source_type=ptxn.source_type,
            raw_narration=ptxn.raw_narration,
            bank_ref=ptxn.bank_ref,
            utr=ptxn.utr,
            cheque_no=ptxn.cheque_no,
            counterparty_name=ptxn.counterparty_name,
            counterparty_upi_id=ptxn.counterparty_upi_id,
            payment_app_note=ptxn.payment_app_note,
        )
        db.add(txn)

    await db.flush()

    # Background: reconcile, detect self-transfers, categorize, check budgets
    background_tasks.add_task(_post_upload_pipeline, upload.id)

    return upload


async def _post_upload_pipeline(upload_id: uuid.UUID):
    """Run after upload: reconcile → self-transfer detect → categorize → budget alerts."""
    from app.core.database import async_session
    async with async_session() as db:
        try:
            await reconcile_all(db)
            await detect_self_transfers(db)
            await categorize_all_uncategorized(db)
            await check_budgets_and_alert(db)
            await db.commit()
        except Exception as e:
            await db.rollback()
            print(f"Post-upload pipeline error: {e}")


@router.get("/", response_model=list[UploadOut])
async def list_uploads(db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select
    result = await db.execute(select(Upload).order_by(Upload.uploaded_at.desc()))
    return result.scalars().all()

