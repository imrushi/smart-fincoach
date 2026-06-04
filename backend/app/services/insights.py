"""
AI-powered insights engine:
- Month-over-month savings comparison
- Category drain analysis
- Recurring subscription detection
- Weekly LLM-generated narrative summary
"""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Transaction, Category, TransactionType, SourceType
from app.schemas.schemas import MonthSummary, CategorySpend
from app.services.reconciliation import BANK_SOURCES
from app.services.llm import get_llm_client, get_llm_model
from app.core.config import get_settings


async def get_month_summary(db: AsyncSession, month_date: date) -> MonthSummary:
    """Compute income/expense/savings for a given month."""
    m_start = month_date.replace(day=1)
    m_end = (m_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    async def _sum(is_debit: bool, txn_type: TransactionType = None):
        q = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.is_debit == is_debit,
            Transaction.is_self_transfer.is_(False),
            Transaction.txn_date >= m_start,
            Transaction.txn_date <= m_end,
            Transaction.source_type.in_(BANK_SOURCES),
        )
        if txn_type:
            q = q.where(Transaction.transaction_type == txn_type)
        r = await db.execute(q)
        return Decimal(str(r.scalar()))

    income = await _sum(False, TransactionType.INCOME)
    expense = await _sum(True, TransactionType.EXPENSE)
    transfer = await _sum(True, TransactionType.TRANSFER)
    investment = await _sum(True, TransactionType.INVESTMENT)
    savings = income - expense - investment
    rate = float(savings / income * 100) if income > 0 else 0

    return MonthSummary(
        month=m_start.strftime("%Y-%m"),
        total_income=income,
        total_expense=expense,
        total_transfer=transfer,
        total_investment=investment,
        savings=savings,
        savings_rate=round(rate, 1),
    )


async def get_category_breakdown(db: AsyncSession, month_date: date) -> list[CategorySpend]:
    """Get spending by category for a given month."""
    m_start = month_date.replace(day=1)
    m_end = (m_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    results = await db.execute(
        select(
            Category.name, Category.icon,
            func.coalesce(func.sum(Transaction.amount), 0).label("total"),
            func.count(Transaction.id).label("txn_count"),
        )
        .join(Transaction, Transaction.category_id == Category.id)
        .where(
            Transaction.is_debit.is_(True),
            Transaction.is_self_transfer.is_(False),
            Transaction.transaction_type == TransactionType.EXPENSE,
            Transaction.txn_date >= m_start,
            Transaction.txn_date <= m_end,
            Transaction.source_type.in_(BANK_SOURCES),
        )
        .group_by(Category.id)
        .order_by(func.sum(Transaction.amount).desc())
    )

    return [
        CategorySpend(
            category_name=row.name, category_icon=row.icon,
            total=Decimal(str(row.total)), txn_count=row.txn_count,
        )
        for row in results.all()
    ]


async def detect_recurring_subscriptions(db: AsyncSession) -> list[dict]:
    """
    Find recurring charges: same merchant, similar amount, appearing 2+ months.
    """
    # Get last 3 months of bank debits
    today = date.today()
    three_months_ago = (today.replace(day=1) - timedelta(days=90)).replace(day=1)

    txns = await db.execute(
        select(Transaction).where(
            Transaction.is_debit.is_(True),
            Transaction.is_self_transfer.is_(False),
            Transaction.txn_date >= three_months_ago,
            Transaction.source_type.in_(BANK_SOURCES),
        ).order_by(Transaction.txn_date)
    )

    # Group by normalized counterparty/narration
    from collections import defaultdict
    merchant_history = defaultdict(list)

    for txn in txns.scalars().all():
        key = (txn.counterparty_name or txn.raw_narration[:50]).lower().strip()
        merchant_history[key].append({
            "date": txn.txn_date,
            "amount": float(txn.amount),
            "month": txn.txn_date.strftime("%Y-%m"),
        })

    subscriptions = []
    for merchant, entries in merchant_history.items():
        months = set(e["month"] for e in entries)
        if len(months) >= 2:
            amounts = [e["amount"] for e in entries]
            avg_amt = sum(amounts) / len(amounts)
            # Check if amounts are similar (within 10%)
            if all(abs(a - avg_amt) / avg_amt < 0.1 for a in amounts):
                subscriptions.append({
                    "merchant": merchant,
                    "avg_amount": round(avg_amt, 2),
                    "months_active": len(months),
                    "monthly_cost": round(avg_amt, 2),
                    "total_cost": round(sum(amounts), 2),
                })

    return sorted(subscriptions, key=lambda x: x["monthly_cost"], reverse=True)


async def generate_ai_insight(db: AsyncSession) -> str | None:
    """Generate a weekly AI narrative summary using GPT."""
    settings = get_settings()
    if not (settings.OPENAI_API_KEY or settings.OPENROUTER_API_KEY):
        return "Configure OPENAI_API_KEY or OPENROUTER_API_KEY to enable AI insights."

    today = date.today()
    current = await get_month_summary(db, today)
    prev_month = (today.replace(day=1) - timedelta(days=1))
    previous = await get_month_summary(db, prev_month)
    breakdown = await get_category_breakdown(db, today)
    subs = await detect_recurring_subscriptions(db)

    summary_text = f"""Current month ({current.month}): Income ₹{current.total_income:,.0f}, Expenses ₹{current.total_expense:,.0f}, Savings ₹{current.savings:,.0f} ({current.savings_rate}%)
Previous month ({previous.month}): Income ₹{previous.total_income:,.0f}, Expenses ₹{previous.total_expense:,.0f}, Savings ₹{previous.savings:,.0f} ({previous.savings_rate}%)

Top categories this month:
{chr(10).join(f"- {c.category_icon} {c.category_name}: ₹{c.total:,.0f} ({c.txn_count} txns)" for c in breakdown[:8])}

Active subscriptions: {len(subs)} totaling ₹{sum(s['monthly_cost'] for s in subs):,.0f}/month
{chr(10).join(f"- {s['merchant']}: ₹{s['monthly_cost']:,.0f}/mo" for s in subs[:5])}"""

    client = get_llm_client()
    if not client:
        return "AI client not configured."
    model = get_llm_model()

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a smart personal finance coach for an Indian user. Be concise, actionable, and encouraging. Use ₹ for amounts. Max 200 words."},
                {"role": "user", "content": f"Analyze my finances and give me actionable advice:\n\n{summary_text}"},
            ],
            temperature=0.7,
            max_tokens=300,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Could not generate insight: {str(e)}"

