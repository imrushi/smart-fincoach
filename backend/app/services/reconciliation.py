"""
Reconciliation engine — matches bank transactions with UPI app transactions.

Priority:
1. UTR/RRN exact match (deterministic, ~95% of UPI txns)
2. Amount + date (±36h) fuzzy match with 1:1 constraint
3. Mark remaining as UNMATCHED for manual review
"""
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Transaction, ReconciliationMethod, SourceType

# Bank sources vs UPI app sources
BANK_SOURCES = {SourceType.ICICI_BANK, SourceType.HDFC_BANK, SourceType.SBI_BANK, SourceType.BOB_BANK}
UPI_SOURCES = {SourceType.PHONEPE, SourceType.GOOGLEPAY, SourceType.CRED, SourceType.IMOBILE}


async def reconcile_all(db: AsyncSession) -> dict:
    """Run full reconciliation pass. Returns stats."""
    stats = {"utr_matched": 0, "fuzzy_matched": 0, "unmatched": 0}

    # Step 1: UTR exact match
    bank_with_utr = await db.execute(
        select(Transaction).where(
            Transaction.source_type.in_(BANK_SOURCES),
            Transaction.utr.isnot(None),
            Transaction.reconciled_with_id.is_(None),
        )
    )
    bank_txns = bank_with_utr.scalars().all()

    for btxn in bank_txns:
        upi_match = await db.execute(
            select(Transaction).where(
                Transaction.source_type.in_(UPI_SOURCES),
                Transaction.utr == btxn.utr,
                Transaction.reconciled_with_id.is_(None),
            ).limit(1)
        )
        upi_txn = upi_match.scalar_one_or_none()
        if upi_txn:
            btxn.reconciled_with_id = upi_txn.id
            btxn.reconciliation_method = ReconciliationMethod.UTR_EXACT
            upi_txn.reconciled_with_id = btxn.id
            upi_txn.reconciliation_method = ReconciliationMethod.UTR_EXACT

            # Enrich bank txn with UPI app details
            if upi_txn.counterparty_name:
                btxn.counterparty_name = upi_txn.counterparty_name
            if upi_txn.counterparty_upi_id:
                btxn.counterparty_upi_id = upi_txn.counterparty_upi_id
            if upi_txn.payment_app_note:
                btxn.payment_app_note = upi_txn.payment_app_note

            stats["utr_matched"] += 1

    await db.flush()

    # Step 2: Fuzzy match — amount + date within ±36 hours, 1:1 constraint
    unmatched_bank = await db.execute(
        select(Transaction).where(
            Transaction.source_type.in_(BANK_SOURCES),
            Transaction.reconciled_with_id.is_(None),
        )
    )
    unmatched_upi = await db.execute(
        select(Transaction).where(
            Transaction.source_type.in_(UPI_SOURCES),
            Transaction.reconciled_with_id.is_(None),
        )
    )

    bank_list = unmatched_bank.scalars().all()
    upi_list = list(unmatched_upi.scalars().all())
    used_upi_ids: set[UUID] = set()

    for btxn in bank_list:
        best_match = None
        best_score = 0.0

        for utxn in upi_list:
            if utxn.id in used_upi_ids:
                continue

            # Amount must match exactly
            if btxn.amount != utxn.amount:
                continue
            # Debit/credit direction must match
            if btxn.is_debit != utxn.is_debit:
                continue

            # Date within ±36 hours
            day_diff = abs((btxn.txn_date - utxn.txn_date).days)
            if day_diff > 1:  # ±1 day covers ±36h for date-only comparison
                continue

            score = 1.0
            if day_diff == 0:
                score = 1.0
            else:
                score = 0.8

            if score > best_score:
                best_score = score
                best_match = utxn

        if best_match:
            btxn.reconciled_with_id = best_match.id
            btxn.reconciliation_method = ReconciliationMethod.AMOUNT_DATE_FUZZY
            best_match.reconciled_with_id = btxn.id
            best_match.reconciliation_method = ReconciliationMethod.AMOUNT_DATE_FUZZY
            used_upi_ids.add(best_match.id)

            if best_match.counterparty_name:
                btxn.counterparty_name = best_match.counterparty_name
            if best_match.counterparty_upi_id:
                btxn.counterparty_upi_id = best_match.counterparty_upi_id
            if best_match.payment_app_note:
                btxn.payment_app_note = best_match.payment_app_note

            stats["fuzzy_matched"] += 1

    # Count remaining unmatched
    unmatched_count = await db.execute(
        select(Transaction).where(Transaction.reconciled_with_id.is_(None))
    )
    stats["unmatched"] = len(unmatched_count.scalars().all())

    return stats


async def detect_self_transfers(db: AsyncSession) -> int:
    """
    Detect transfers between own accounts.
    A DEBIT in one bank ↔ CREDIT in another bank, same date, same amount.
    """
    count = 0
    debits = await db.execute(
        select(Transaction).where(
            Transaction.source_type.in_(BANK_SOURCES),
            Transaction.is_debit.is_(True),
            Transaction.is_self_transfer.is_(False),
        )
    )
    credits = await db.execute(
        select(Transaction).where(
            Transaction.source_type.in_(BANK_SOURCES),
            Transaction.is_debit.is_(False),
            Transaction.is_self_transfer.is_(False),
        )
    )

    debit_list = debits.scalars().all()
    credit_list = list(credits.scalars().all())
    used: set[UUID] = set()

    for d in debit_list:
        for c in credit_list:
            if c.id in used:
                continue
            if c.source_type == d.source_type:
                continue  # Must be different banks
            if d.amount == c.amount and abs((d.txn_date - c.txn_date).days) <= 1:
                d.is_self_transfer = True
                c.is_self_transfer = True
                from app.models.models import TransactionType
                d.transaction_type = TransactionType.TRANSFER
                c.transaction_type = TransactionType.TRANSFER
                used.add(c.id)
                count += 1
                break

    return count

