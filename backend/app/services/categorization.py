"""
3-tier categorization engine:
  1. Rules (regex patterns — highest priority, cheapest)
  2. Embeddings (pgvector similarity to cached merchants)
  3. LLM fallback (gpt-4o-mini — only for genuinely new merchants, cached after)

Manual overrides auto-promote to rules after 1 confirmation.
"""
import re
from uuid import UUID

from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    Transaction, Category, CategoryRule, MerchantCategoryCache,
    TransactionType, SourceType
)
from app.core.config import get_settings
from app.services.llm import get_llm_client, get_llm_model


# ── Tier 1: Rule-based matching ──────────────────────────────────────────────

async def categorize_by_rules(txn: Transaction, db: AsyncSession) -> UUID | None:
    """Match transaction narration against category rules (regex/substring)."""
    rules = await db.execute(
        select(CategoryRule)
        .where(CategoryRule.is_active.is_(True))
        .order_by(CategoryRule.priority.desc())
    )
    narration = (txn.raw_narration or "").lower()
    counterparty = (txn.counterparty_name or "").lower()
    search_text = f"{narration} {counterparty}"

    for rule in rules.scalars().all():
        try:
            if re.search(rule.pattern, search_text, re.IGNORECASE):
                return rule.category_id
        except re.error:
            # Fallback to substring match if regex is invalid
            if rule.pattern.lower() in search_text:
                return rule.category_id
    return None


# ── Tier 2: Merchant cache lookup (embedding similarity) ─────────────────────

async def categorize_by_merchant_cache(txn: Transaction, db: AsyncSession) -> UUID | None:
    """Check if we've already categorized this merchant."""
    merchant_name = _extract_merchant(txn)
    if not merchant_name:
        return None

    # Exact match first
    cached = await db.execute(
        select(MerchantCategoryCache)
        .where(MerchantCategoryCache.merchant_normalized == merchant_name.lower().strip())
    )
    result = cached.scalar_one_or_none()
    if result:
        return result.category_id

    # TODO: Phase 3+ — pgvector similarity search with embeddings
    return None


# ── Tier 3: LLM fallback ─────────────────────────────────────────────────────

async def categorize_by_llm(txn: Transaction, categories: list[Category], db: AsyncSession) -> tuple[UUID | None, float]:
    """Ask gpt-4o-mini to categorize. Cache result per merchant."""
    client = get_llm_client()
    if not client:
        return None, 0.0

    model = get_llm_model()
    cat_list = ", ".join([f"{c.name} ({c.icon})" for c in categories])
    merchant = _extract_merchant(txn) or txn.raw_narration[:100]

    prompt = f"""You are a personal finance categorizer for an Indian user.
Given a transaction, return ONLY the category name from this list: {cat_list}

Transaction:
- Narration: {txn.raw_narration}
- Counterparty: {txn.counterparty_name or 'Unknown'}
- UPI ID: {txn.counterparty_upi_id or 'N/A'}
- Amount: ₹{txn.amount}
- Note: {txn.payment_app_note or 'N/A'}

Reply with JUST the category name, nothing else."""

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=20,
        )
        predicted = resp.choices[0].message.content.strip()

        # Match to actual category
        for cat in categories:
            if cat.name.lower() == predicted.lower():
                # Cache this merchant → category mapping
                await _cache_merchant(merchant, cat.id, "LLM", db)
                return cat.id, 0.9

        # Fuzzy match
        for cat in categories:
            if predicted.lower() in cat.name.lower() or cat.name.lower() in predicted.lower():
                await _cache_merchant(merchant, cat.id, "LLM", db)
                return cat.id, 0.7

    except Exception as e:
        pass  # Log error, don't crash

    return None, 0.0


# ── Transaction type correction based on category ─────────────────────────────

# Categories that are ALWAYS expenses even if credit (e.g., loan disbursement is different)
EXPENSE_CATEGORIES = {"EMI & Loans", "Rent", "Bills & Utilities", "Subscriptions", "Insurance"}
# Categories that override to INCOME if credited
INCOME_CATEGORIES = {"Salary", "Freelance", "Cashback & Rewards"}
# Categories that override type
REFUND_CATEGORIES = {"Refund"}


async def _fix_transaction_type(txn: Transaction, cat_id, categories: list) -> None:
    """Override transaction_type based on category when appropriate."""
    cat = next((c for c in categories if c.id == cat_id), None)
    if not cat:
        return
    if txn.is_debit and cat.name in EXPENSE_CATEGORIES:
        txn.transaction_type = TransactionType.EXPENSE
    elif not txn.is_debit and cat.name in INCOME_CATEGORIES:
        txn.transaction_type = TransactionType.INCOME
    elif cat.name in REFUND_CATEGORIES:
        txn.transaction_type = TransactionType.REFUND
    elif txn.is_debit and txn.transaction_type != TransactionType.TRANSFER:
        txn.transaction_type = TransactionType.EXPENSE


# ── Main categorization pipeline ─────────────────────────────────────────────

async def categorize_transaction(txn: Transaction, db: AsyncSession) -> None:
    """Run 3-tier categorization on a single transaction."""
    if txn.is_self_transfer:
        txn.transaction_type = TransactionType.TRANSFER
        return

    # Skip if already manually categorized
    if txn.categorization_source == "MANUAL":
        return

    categories = (await db.execute(select(Category))).scalars().all()

    # Detect income
    if not txn.is_debit:
        txn.transaction_type = TransactionType.INCOME
        # Still categorize (salary, refund, cashback, etc.)

    # Tier 1: Rules
    cat_id = await categorize_by_rules(txn, db)
    if cat_id:
        txn.category_id = cat_id
        txn.categorization_source = "RULE"
        txn.categorization_confidence = 1.0
        # Fix: EMI/Loans debits should always be EXPENSE even if rule matches
        await _fix_transaction_type(txn, cat_id, categories)
        return

    # Tier 2: Merchant cache
    cat_id = await categorize_by_merchant_cache(txn, db)
    if cat_id:
        txn.category_id = cat_id
        txn.categorization_source = "EMBEDDING"
        txn.categorization_confidence = 0.95
        await _fix_transaction_type(txn, cat_id, categories)
        return

    # Tier 3: LLM
    cat_id, confidence = await categorize_by_llm(txn, list(categories), db)
    if cat_id:
        txn.category_id = cat_id
        txn.categorization_source = "LLM"
        txn.categorization_confidence = confidence
        await _fix_transaction_type(txn, cat_id, categories)
        return


async def categorize_all_uncategorized(db: AsyncSession) -> int:
    """Categorize all transactions that don't have a category yet."""
    uncategorized = await db.execute(
        select(Transaction).where(
            Transaction.category_id.is_(None),
            or_(
                Transaction.categorization_source.is_(None),
                Transaction.categorization_source != "MANUAL",
            ),
        )
    )
    count = 0
    for txn in uncategorized.scalars().all():
        await categorize_transaction(txn, db)
        count += 1
    return count


# ── Manual override → auto-promote to rule ────────────────────────────────────

async def override_category(txn_id: UUID, category_id: UUID, db: AsyncSession) -> None:
    """User manually overrides category. Auto-promotes to merchant cache + rule."""
    txn = await db.get(Transaction, txn_id)
    if not txn:
        return

    txn.category_id = category_id
    txn.categorization_source = "MANUAL"
    txn.categorization_confidence = 1.0

    merchant = _extract_merchant(txn)
    if merchant:
        # Update or create merchant cache
        await _cache_merchant(merchant, category_id, "MANUAL", db)

        # Auto-promote to rule: create pattern from merchant name
        existing = await db.execute(
            select(CategoryRule).where(
                CategoryRule.pattern == re.escape(merchant.lower()),
                CategoryRule.category_id == category_id,
            )
        )
        if not existing.scalar_one_or_none():
            rule = CategoryRule(
                pattern=re.escape(merchant.lower()),
                category_id=category_id,
                priority=10,
                source="AUTO_PROMOTED",
            )
            db.add(rule)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_merchant(txn: Transaction) -> str | None:
    """Extract a normalized merchant name from transaction."""
    if txn.counterparty_name and txn.counterparty_name not in ("", "nan"):
        return txn.counterparty_name.strip()

    narration = txn.raw_narration or ""
    # Try to extract from UPI narration: UPI-MERCHANT-VPA-UTR
    parts = narration.split("-")
    if len(parts) >= 2:
        return parts[1].strip()

    return narration[:100].strip() if narration else None


async def _cache_merchant(name: str, category_id: UUID, source: str, db: AsyncSession):
    """Insert or update merchant→category cache."""
    normalized = name.lower().strip()
    existing = await db.execute(
        select(MerchantCategoryCache)
        .where(MerchantCategoryCache.merchant_normalized == normalized)
    )
    cached = existing.scalar_one_or_none()
    if cached:
        cached.category_id = category_id
        cached.source = source
        cached.override_count += 1
    else:
        db.add(MerchantCategoryCache(
            merchant_name=name,
            merchant_normalized=normalized,
            category_id=category_id,
            source=source,
        ))

