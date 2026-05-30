"""
Indian Financial Planning Service:
- 80C/80D tax deduction tracker
- FY (April-March) financial year view
- Debt-to-income ratio
- Emergency fund health
- Fixed vs Variable expense classification
"""
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import Transaction, Category, TransactionType
from app.services.reconciliation import BANK_SOURCES


# ── Category Classification ───────────────────────────────────────────────────

# Fixed expenses: can't easily reduce month-to-month
FIXED_EXPENSE_CATEGORIES = {
    "EMI & Loans", "Rent", "Insurance", "Subscriptions",
}

# Tax-saving categories under 80C (limit: ₹1,50,000/year)
TAX_80C_CATEGORIES = {
    "Investments",  # ELSS, PPF, NPS
    "Insurance",    # Life insurance premium
    "EMI & Loans",  # Home loan principal
    "Education",    # Tuition fees for children
}

# Tax-saving under 80D (health insurance, limit: ₹25,000 for self, ₹50,000 for parents)
TAX_80D_CATEGORIES = {
    "Health & Medical",  # Health insurance premium
}

# 80D keywords to distinguish insurance premium from medical expenses
TAX_80D_KEYWORDS = [
    "health insurance", "mediclaim", "star health", "niva bupa",
    "care health", "hdfc ergo health", "icici lombard health",
    "max bupa", "aditya birla health",
]

# 80C keywords for more specific matching
TAX_80C_KEYWORDS = [
    "elss", "ppf", "nps", "national pension", "lic", "life insurance",
    "tax saver", "tax saving", "80c", "sukanya", "nsc",
    "tuition", "school fee", "college fee",
]


def _get_fy_range(fy_year: int | None = None) -> tuple[date, date]:
    """Get FY start and end dates. FY 2025-26 = April 2025 to March 2026."""
    today = date.today()
    if fy_year is None:
        # Current FY
        if today.month >= 4:
            fy_start = date(today.year, 4, 1)
            fy_end = date(today.year + 1, 3, 31)
        else:
            fy_start = date(today.year - 1, 4, 1)
            fy_end = date(today.year, 3, 31)
    else:
        fy_start = date(fy_year, 4, 1)
        fy_end = date(fy_year + 1, 3, 31)
    return fy_start, fy_end


async def get_fy_summary(db: AsyncSession, fy_year: int | None = None) -> dict:
    """Get financial year (April-March) summary."""
    fy_start, fy_end = _get_fy_range(fy_year)
    today = date.today()
    effective_end = min(fy_end, today)

    async def _sum(is_debit: bool, txn_type: TransactionType | None = None):
        q = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.is_debit == is_debit,
            Transaction.is_self_transfer.is_(False),
            Transaction.txn_date >= fy_start,
            Transaction.txn_date <= effective_end,
            Transaction.source_type.in_(BANK_SOURCES),
        )
        if txn_type:
            q = q.where(Transaction.transaction_type == txn_type)
        r = await db.execute(q)
        return Decimal(str(r.scalar()))

    income = await _sum(False, TransactionType.INCOME)
    expense = await _sum(True, TransactionType.EXPENSE)
    investment = await _sum(True, TransactionType.INVESTMENT)
    transfer = await _sum(True, TransactionType.TRANSFER)

    months_elapsed = (effective_end.year - fy_start.year) * 12 + (effective_end.month - fy_start.month) + 1
    avg_monthly_income = float(income) / months_elapsed if months_elapsed > 0 else 0
    avg_monthly_expense = float(expense) / months_elapsed if months_elapsed > 0 else 0

    return {
        "fy_label": f"FY {fy_start.year}-{str(fy_end.year)[2:]}",
        "fy_start": fy_start.isoformat(),
        "fy_end": fy_end.isoformat(),
        "months_elapsed": months_elapsed,
        "total_income": float(income),
        "total_expense": float(expense),
        "total_investment": float(investment),
        "total_transfer": float(transfer),
        "total_savings": float(income - expense - investment),
        "savings_rate": round(float((income - expense - investment) / income * 100), 1) if income > 0 else 0,
        "avg_monthly_income": round(avg_monthly_income, 0),
        "avg_monthly_expense": round(avg_monthly_expense, 0),
        "avg_monthly_savings": round(avg_monthly_income - avg_monthly_expense, 0),
    }


async def get_tax_planning(db: AsyncSession, fy_year: int | None = None) -> dict:
    """Calculate 80C and 80D utilization for the financial year."""
    fy_start, fy_end = _get_fy_range(fy_year)
    today = date.today()
    effective_end = min(fy_end, today)

    # Get all debit transactions in the FY that might be tax-saving
    txns = await db.execute(
        select(Transaction, Category.name.label("cat_name"))
        .join(Category, Transaction.category_id == Category.id, isouter=True)
        .where(
            Transaction.is_debit.is_(True),
            Transaction.is_self_transfer.is_(False),
            Transaction.txn_date >= fy_start,
            Transaction.txn_date <= effective_end,
            Transaction.source_type.in_(BANK_SOURCES),
        )
    )

    section_80c_total = Decimal("0")
    section_80c_items = []
    section_80d_total = Decimal("0")
    section_80d_items = []

    for txn, cat_name in txns.all():
        narration = (txn.raw_narration or "").lower()
        counterparty = (txn.counterparty_name or "").lower()
        search_text = f"{narration} {counterparty}"

        # Check 80C
        is_80c = False
        if cat_name in TAX_80C_CATEGORIES:
            for kw in TAX_80C_KEYWORDS:
                if kw in search_text:
                    is_80c = True
                    break
            # Investments category is always 80C eligible (ELSS, PPF, etc.)
            if cat_name == "Investments":
                is_80c = True

        if is_80c:
            section_80c_total += txn.amount
            section_80c_items.append({
                "date": txn.txn_date.isoformat(),
                "amount": float(txn.amount),
                "description": txn.counterparty_name or txn.raw_narration[:60],
                "category": cat_name,
            })

        # Check 80D
        is_80d = False
        if cat_name in TAX_80D_CATEGORIES:
            for kw in TAX_80D_KEYWORDS:
                if kw in search_text:
                    is_80d = True
                    break

        if is_80d:
            section_80d_total += txn.amount
            section_80d_items.append({
                "date": txn.txn_date.isoformat(),
                "amount": float(txn.amount),
                "description": txn.counterparty_name or txn.raw_narration[:60],
            })

    limit_80c = 150000
    limit_80d = 25000  # self; 50000 for senior citizen parents

    months_remaining = 0
    if effective_end < fy_end:
        months_remaining = (fy_end.year - effective_end.year) * 12 + (fy_end.month - effective_end.month)

    return {
        "fy_label": f"FY {fy_start.year}-{str(fy_end.year)[2:]}",
        "section_80c": {
            "limit": limit_80c,
            "utilized": float(section_80c_total),
            "remaining": max(0, limit_80c - float(section_80c_total)),
            "pct_used": round(float(section_80c_total) / limit_80c * 100, 1) if limit_80c > 0 else 0,
            "potential_tax_saved": round(min(float(section_80c_total), limit_80c) * 0.3, 0),  # Assuming 30% tax bracket
            "items": section_80c_items[-10:],  # Last 10 items
            "months_remaining": months_remaining,
            "monthly_investment_needed": round((limit_80c - float(section_80c_total)) / max(months_remaining, 1), 0) if float(section_80c_total) < limit_80c else 0,
        },
        "section_80d": {
            "limit": limit_80d,
            "utilized": float(section_80d_total),
            "remaining": max(0, limit_80d - float(section_80d_total)),
            "pct_used": round(float(section_80d_total) / limit_80d * 100, 1) if limit_80d > 0 else 0,
            "potential_tax_saved": round(min(float(section_80d_total), limit_80d) * 0.3, 0),
            "items": section_80d_items[-10:],
        },
        "total_tax_saved": round(
            (min(float(section_80c_total), limit_80c) + min(float(section_80d_total), limit_80d)) * 0.3, 0
        ),
    }


async def get_debt_to_income(db: AsyncSession) -> dict:
    """Calculate debt-to-income ratio (total EMI / monthly income)."""
    today = date.today()
    # Use last 3 months average for stability
    three_months_ago = (today.replace(day=1) - timedelta(days=90)).replace(day=1)

    # Monthly income (average of last 3 months)
    income_r = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.is_debit.is_(False),
            Transaction.is_self_transfer.is_(False),
            Transaction.transaction_type == TransactionType.INCOME,
            Transaction.txn_date >= three_months_ago,
            Transaction.source_type.in_(BANK_SOURCES),
        )
    )
    total_income_3m = Decimal(str(income_r.scalar()))
    avg_monthly_income = float(total_income_3m) / 3

    # Monthly EMI (from EMI & Loans category)
    emi_r = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0))
        .select_from(Transaction)
        .join(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.is_debit.is_(True),
            Transaction.is_self_transfer.is_(False),
            Category.name == "EMI & Loans",
            Transaction.txn_date >= three_months_ago,
            Transaction.source_type.in_(BANK_SOURCES),
        )
    )
    total_emi_3m = Decimal(str(emi_r.scalar()))
    avg_monthly_emi = float(total_emi_3m) / 3

    # Also get rent as a fixed obligation
    rent_r = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0))
        .select_from(Transaction)
        .join(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.is_debit.is_(True),
            Transaction.is_self_transfer.is_(False),
            Category.name == "Rent",
            Transaction.txn_date >= three_months_ago,
            Transaction.source_type.in_(BANK_SOURCES),
        )
    )
    total_rent_3m = Decimal(str(rent_r.scalar()))
    avg_monthly_rent = float(total_rent_3m) / 3

    dti_ratio = (avg_monthly_emi / avg_monthly_income * 100) if avg_monthly_income > 0 else 0
    fixed_obligation_ratio = ((avg_monthly_emi + avg_monthly_rent) / avg_monthly_income * 100) if avg_monthly_income > 0 else 0

    # Health assessment
    if dti_ratio > 50:
        health = "CRITICAL"
        message = "Your EMI burden exceeds 50% of income. Banks consider this high risk. Consider debt consolidation."
    elif dti_ratio > 40:
        health = "WARNING"
        message = "EMI at 40-50% of income. No room for new loans. Focus on paying off existing debt."
    elif dti_ratio > 30:
        health = "MODERATE"
        message = "EMI is manageable but be cautious about taking new loans."
    elif dti_ratio > 0:
        health = "HEALTHY"
        message = "Good debt-to-income ratio. You have capacity for additional credit if needed."
    else:
        health = "EXCELLENT"
        message = "No EMI obligations detected. You're debt-free!"

    return {
        "avg_monthly_income": round(avg_monthly_income, 0),
        "avg_monthly_emi": round(avg_monthly_emi, 0),
        "avg_monthly_rent": round(avg_monthly_rent, 0),
        "dti_ratio": round(dti_ratio, 1),
        "fixed_obligation_ratio": round(fixed_obligation_ratio, 1),
        "health": health,
        "message": message,
        "bank_limit_pct": 50,  # Banks typically reject above 50%
    }


async def get_emergency_fund_health(db: AsyncSession) -> dict:
    """Calculate emergency fund adequacy (liquid savings / monthly expenses)."""
    today = date.today()
    three_months_ago = (today.replace(day=1) - timedelta(days=90)).replace(day=1)

    # Average monthly expenses (last 3 months)
    expense_r = await db.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.is_debit.is_(True),
            Transaction.is_self_transfer.is_(False),
            Transaction.transaction_type == TransactionType.EXPENSE,
            Transaction.txn_date >= three_months_ago,
            Transaction.source_type.in_(BANK_SOURCES),
        )
    )
    avg_monthly_expense = float(Decimal(str(expense_r.scalar()))) / 3

    # Net savings accumulated (total income - total expenses over full history)
    # Use balance_after from most recent transaction as proxy for liquid savings
    latest_balance_r = await db.execute(
        select(Transaction.balance_after).where(
            Transaction.balance_after.isnot(None),
            Transaction.source_type.in_(BANK_SOURCES),
        ).order_by(Transaction.txn_date.desc()).limit(1)
    )
    latest_balance = latest_balance_r.scalar()
    liquid_savings = float(latest_balance) if latest_balance else 0

    months_covered = liquid_savings / avg_monthly_expense if avg_monthly_expense > 0 else 0
    recommended_months = 6  # Standard recommendation

    if months_covered >= 6:
        health = "EXCELLENT"
        message = f"Your emergency fund covers {months_covered:.1f} months. Well done!"
    elif months_covered >= 3:
        health = "GOOD"
        message = f"Covers {months_covered:.1f} months. Aim for 6 months (₹{avg_monthly_expense * 6:,.0f})."
    elif months_covered >= 1:
        health = "WARNING"
        message = f"Only {months_covered:.1f} months covered. Build this to at least 3 months urgently."
    else:
        health = "CRITICAL"
        message = "Less than 1 month of expenses covered. This is your top priority."

    target_amount = avg_monthly_expense * recommended_months
    gap = max(0, target_amount - liquid_savings)

    return {
        "liquid_savings": round(liquid_savings, 0),
        "avg_monthly_expense": round(avg_monthly_expense, 0),
        "months_covered": round(months_covered, 1),
        "recommended_months": recommended_months,
        "target_amount": round(target_amount, 0),
        "gap": round(gap, 0),
        "health": health,
        "message": message,
    }


async def get_fixed_vs_variable(db: AsyncSession, month_date: date | None = None) -> dict:
    """Split expenses into fixed (can't reduce) vs variable (controllable)."""
    today = month_date or date.today()
    m_start = today.replace(day=1)
    m_end = (m_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)

    # Get all expenses with categories
    results = await db.execute(
        select(
            Category.name, Category.icon,
            func.coalesce(func.sum(Transaction.amount), 0).label("total"),
            func.count(Transaction.id).label("txn_count"),
        )
        .join(Category, Transaction.category_id == Category.id)
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

    fixed = []
    variable = []
    total_fixed = Decimal("0")
    total_variable = Decimal("0")

    for row in results.all():
        item = {
            "category": row.name,
            "icon": row.icon,
            "amount": float(row.total),
            "txn_count": row.txn_count,
        }
        if row.name in FIXED_EXPENSE_CATEGORIES:
            fixed.append(item)
            total_fixed += Decimal(str(row.total))
        else:
            variable.append(item)
            total_variable += Decimal(str(row.total))

    total = float(total_fixed + total_variable)

    return {
        "month": m_start.strftime("%Y-%m"),
        "fixed": {
            "total": float(total_fixed),
            "pct_of_total": round(float(total_fixed) / total * 100, 1) if total > 0 else 0,
            "items": fixed,
            "note": "These are commitments you can't easily change month-to-month.",
        },
        "variable": {
            "total": float(total_variable),
            "pct_of_total": round(float(total_variable) / total * 100, 1) if total > 0 else 0,
            "items": variable,
            "note": "These are controllable — focus budget cuts here.",
        },
        "total_expense": total,
        "controllable_savings_potential": round(float(total_variable) * 0.2, 0),  # 20% reduction target
    }

