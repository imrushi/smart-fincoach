"""
Nightly LLM Analysis & Email Notification System.

Runs daily (triggered via cron/scheduler) and:
1. Checks budget breaches & run-rate projections
2. Detects anomalies (unusual large transactions)
3. Identifies missed recurring payments
4. Detects duplicate charges
5. Generates AI-powered insights
6. Sends email notifications based on priority
"""
from datetime import date, timedelta, datetime
from decimal import Decimal
from collections import defaultdict

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.models import (
    Transaction, Category, Budget, Alert,
    TransactionType, SourceType, ReconciliationMethod
)
from app.services.reconciliation import BANK_SOURCES
from app.services.insights import get_month_summary, get_category_breakdown, detect_recurring_subscriptions
from app.services.llm import get_llm_client, get_llm_model
from app.core.config import get_settings


# ── Analysis Functions ────────────────────────────────────────────────────────

async def check_budget_breaches(db: AsyncSession) -> list[dict]:
    """Check which budgets are breached or projected to breach."""
    today = date.today()
    m_start = today.replace(day=1)
    m_end = (m_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    days_in_month = (m_end - m_start).days + 1
    days_elapsed = (today - m_start).days + 1
    days_remaining = days_in_month - days_elapsed

    budgets = await db.execute(
        select(Budget, Category).join(Category).where(Budget.month == m_start)
    )

    alerts = []
    for budget, cat in budgets.all():
        spent_r = await db.execute(
            select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                Transaction.category_id == cat.id,
                Transaction.is_debit.is_(True),
                Transaction.is_self_transfer.is_(False),
                Transaction.transaction_type == TransactionType.EXPENSE,
                Transaction.txn_date >= m_start,
                Transaction.txn_date <= today,
                Transaction.source_type.in_(BANK_SOURCES),
            )
        )
        spent = Decimal(str(spent_r.scalar()))
        pct = float(spent / budget.limit_amount * 100) if budget.limit_amount > 0 else 0

        # Run-rate projection
        daily_rate = float(spent) / days_elapsed if days_elapsed > 0 else 0
        projected_total = daily_rate * days_in_month
        projected_overshoot = projected_total - float(budget.limit_amount)

        if pct >= 100:
            alerts.append({
                "priority": "CRITICAL",
                "category": cat.name,
                "icon": cat.icon,
                "type": "BUDGET_EXCEEDED",
                "message": f"{cat.icon} {cat.name} budget EXCEEDED: ₹{spent:,.0f} spent vs ₹{budget.limit_amount:,.0f} limit ({pct:.0f}%) with {days_remaining} days remaining.",
                "spent": float(spent),
                "limit": float(budget.limit_amount),
                "pct": pct,
            })
        elif projected_overshoot > 0 and pct >= 60:
            alerts.append({
                "priority": "WARNING",
                "category": cat.name,
                "icon": cat.icon,
                "type": "RUNRATE_BREACH",
                "message": f"{cat.icon} {cat.name}: At current pace (₹{daily_rate:,.0f}/day), you'll exceed budget by ₹{projected_overshoot:,.0f} by month-end.",
                "spent": float(spent),
                "limit": float(budget.limit_amount),
                "pct": pct,
                "projected_overshoot": projected_overshoot,
            })
        elif pct >= 80:
            alerts.append({
                "priority": "WARNING",
                "category": cat.name,
                "icon": cat.icon,
                "type": "BUDGET_WARNING",
                "message": f"{cat.icon} {cat.name} at {pct:.0f}%: ₹{spent:,.0f} of ₹{budget.limit_amount:,.0f} used. ₹{float(budget.limit_amount - spent):,.0f} remaining for {days_remaining} days.",
                "spent": float(spent),
                "limit": float(budget.limit_amount),
                "pct": pct,
            })

    return alerts


async def detect_anomalies(db: AsyncSession) -> list[dict]:
    """Find unusually large transactions (>2x category average)."""
    today = date.today()
    three_months_ago = today - timedelta(days=90)

    # Get category averages over last 3 months
    cat_avg = await db.execute(
        select(
            Transaction.category_id,
            Category.name,
            Category.icon,
            func.avg(Transaction.amount).label("avg_amount"),
            func.stddev(Transaction.amount).label("stddev_amount"),
        )
        .join(Category, Transaction.category_id == Category.id)
        .where(
            Transaction.is_debit.is_(True),
            Transaction.is_self_transfer.is_(False),
            Transaction.txn_date >= three_months_ago,
            Transaction.source_type.in_(BANK_SOURCES),
        )
        .group_by(Transaction.category_id, Category.name, Category.icon)
    )

    cat_stats = {row.category_id: {
        "name": row.name, "icon": row.icon,
        "avg": float(row.avg_amount or 0),
        "stddev": float(row.stddev_amount or 0),
    } for row in cat_avg.all()}

    # Check today's transactions
    today_txns = await db.execute(
        select(Transaction).where(
            Transaction.txn_date == today,
            Transaction.is_debit.is_(True),
            Transaction.is_self_transfer.is_(False),
            Transaction.source_type.in_(BANK_SOURCES),
        )
    )

    anomalies = []
    for txn in today_txns.scalars().all():
        if txn.category_id and txn.category_id in cat_stats:
            stats = cat_stats[txn.category_id]
            threshold = stats["avg"] + (2 * stats["stddev"]) if stats["stddev"] > 0 else stats["avg"] * 2
            if float(txn.amount) > threshold and float(txn.amount) > 1000:
                anomalies.append({
                    "priority": "WARNING" if float(txn.amount) > 10000 else "INFO",
                    "type": "ANOMALY",
                    "message": f"🚨 Unusual spend: ₹{txn.amount:,.0f} on {stats['icon']} {stats['name']} (avg: ₹{stats['avg']:,.0f}). Merchant: {txn.counterparty_name or txn.raw_narration[:50]}",
                    "amount": float(txn.amount),
                    "avg": stats["avg"],
                })

    return anomalies


async def detect_duplicate_charges(db: AsyncSession) -> list[dict]:
    """Same amount to same merchant within 24 hours."""
    today = date.today()
    yesterday = today - timedelta(days=1)

    recent = await db.execute(
        select(Transaction).where(
            Transaction.txn_date >= yesterday,
            Transaction.is_debit.is_(True),
            Transaction.source_type.in_(BANK_SOURCES),
        ).order_by(Transaction.txn_date)
    )

    # Group by (merchant, amount)
    groups = defaultdict(list)
    for txn in recent.scalars().all():
        key = (
            (txn.counterparty_name or txn.raw_narration[:30]).lower().strip(),
            str(txn.amount),
        )
        groups[key].append(txn)

    duplicates = []
    for (merchant, amount), txns in groups.items():
        if len(txns) >= 2:
            duplicates.append({
                "priority": "WARNING",
                "type": "DUPLICATE",
                "message": f"⚠️ Possible duplicate: {len(txns)} charges of ₹{amount} to '{merchant}' in last 24hrs. Please verify.",
                "merchant": merchant,
                "amount": float(amount),
                "count": len(txns),
            })

    return duplicates


async def check_missed_recurring(db: AsyncSession) -> list[dict]:
    """Check if expected recurring payments haven't hit this month."""
    today = date.today()
    m_start = today.replace(day=1)

    # Only check after 10th of month (give time for payments to process)
    if today.day < 10:
        return []

    subs = await detect_recurring_subscriptions(db)

    # Check which subscriptions haven't appeared this month
    this_month_merchants = await db.execute(
        select(Transaction.counterparty_name, Transaction.raw_narration).where(
            Transaction.txn_date >= m_start,
            Transaction.is_debit.is_(True),
            Transaction.source_type.in_(BANK_SOURCES),
        )
    )

    current_merchants = set()
    for row in this_month_merchants.all():
        key = (row.counterparty_name or row.raw_narration[:50]).lower().strip()
        current_merchants.add(key)

    missed = []
    for sub in subs:
        if sub["merchant"] not in current_merchants and sub["months_active"] >= 3:
            missed.append({
                "priority": "INFO",
                "type": "MISSED_RECURRING",
                "message": f"📋 Expected recurring payment not seen this month: '{sub['merchant']}' (usually ₹{sub['monthly_cost']:,.0f}). Cancelled or bounced?",
                "merchant": sub["merchant"],
                "expected_amount": sub["monthly_cost"],
            })

    return missed


async def compute_savings_score(db: AsyncSession) -> dict:
    """Compute a 1-10 savings score based on multiple factors."""
    today = date.today()
    current = await get_month_summary(db, today)

    days_elapsed = today.day
    days_in_month = ((today.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)).day

    # Factors
    savings_rate = current.savings_rate
    expense_pace = float(current.total_expense) / days_elapsed * days_in_month if days_elapsed > 0 else 0

    # Score calculation (simple weighted)
    score = 5  # base
    if savings_rate > 30:
        score += 2
    elif savings_rate > 20:
        score += 1
    elif savings_rate < 10:
        score -= 1
    elif savings_rate < 0:
        score -= 2

    return {
        "score": max(1, min(10, score)),
        "savings_rate": savings_rate,
        "income": float(current.total_income),
        "expense": float(current.total_expense),
        "savings": float(current.savings),
    }


# ── Main Nightly Analysis ────────────────────────────────────────────────────

async def run_nightly_analysis(db: AsyncSession) -> dict:
    """Run complete nightly analysis and generate alerts + email content."""
    settings = get_settings()

    # Gather all checks
    budget_alerts = await check_budget_breaches(db)
    anomalies = await detect_anomalies(db)
    duplicates = await detect_duplicate_charges(db)
    missed = await check_missed_recurring(db)
    savings = await compute_savings_score(db)

    all_findings = budget_alerts + anomalies + duplicates + missed

    # Separate by priority
    critical = [f for f in all_findings if f.get("priority") == "CRITICAL"]
    warnings = [f for f in all_findings if f.get("priority") == "WARNING"]
    info = [f for f in all_findings if f.get("priority") == "INFO"]

    # Store alerts in DB
    for finding in all_findings:
        alert = Alert(
            alert_type=finding.get("type", "INSIGHT"),
            title=f"[{finding['priority']}] {finding.get('type', 'Alert')}",
            message=finding["message"],
        )
        db.add(alert)

    # Generate LLM summary if there are findings
    llm_summary = None
    if (settings.OPENAI_API_KEY or settings.OPENROUTER_API_KEY) and all_findings:
        llm_summary = await _generate_nightly_llm_summary(db, all_findings, savings)

    await db.flush()

    result = {
        "date": date.today().isoformat(),
        "savings_score": savings,
        "critical_count": len(critical),
        "warning_count": len(warnings),
        "info_count": len(info),
        "critical": critical,
        "warnings": warnings,
        "info": info,
        "llm_summary": llm_summary,
    }

    # Send email if there are critical or warning items
    if critical or warnings:
        await _send_notification_email(result, settings)

    return result


async def _generate_nightly_llm_summary(db: AsyncSession, findings: list, savings: dict) -> str:
    """Generate LLM-powered nightly analysis summary."""
    client = get_llm_client()
    if not client:
        return "LLM analysis unavailable. Configure OPENAI_API_KEY or OPENROUTER_API_KEY."
    model = get_llm_model()

    today = date.today()
    m_start = today.replace(day=1)
    m_end = ((m_start + timedelta(days=32)).replace(day=1)) - timedelta(days=1)
    days_remaining = (m_end - today).days

    findings_text = "\n".join(f"- {f['message']}" for f in findings[:10])

    prompt = f"""You are a personal finance advisor for an Indian professional.

Today: {today.strftime('%d %B %Y')} (Day {today.day} of {m_end.day}, {days_remaining} days remaining)

Financial Health:
- Savings Score: {savings['score']}/10
- Income this month: ₹{savings['income']:,.0f}
- Expenses this month: ₹{savings['expense']:,.0f}
- Net savings: ₹{savings['savings']:,.0f} ({savings['savings_rate']}% rate)

Today's Findings:
{findings_text}

Generate a brief nightly report with:
1. 🚨 CRITICAL ACTIONS (if any) - what to do immediately
2. ⚠️ WATCH ITEMS - things to be aware of
3. 💡 ONE specific, actionable money-saving tip for tomorrow
4. 📊 Overall financial health assessment (1 line)

Be specific with ₹ amounts. Keep under 200 words. Tone: professional but friendly."""

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=400,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"LLM analysis unavailable: {str(e)}"


async def _send_notification_email(analysis: dict, settings) -> bool:
    """Send email notification via Resend (free tier: 3000 emails/month)."""
    if not getattr(settings, "RESEND_API_KEY", None) or not getattr(settings, "NOTIFICATION_EMAIL", None):
        return False

    try:
        import httpx

        critical = analysis.get("critical", [])
        warnings = analysis.get("warnings", [])
        llm_summary = analysis.get("llm_summary", "")
        savings = analysis.get("savings_score", {})

        # Build email HTML
        critical_html = ""
        if critical:
            items = "".join(f"<li style='color:#ef4444;margin:4px 0'>{f['message']}</li>" for f in critical)
            critical_html = f"<h3 style='color:#ef4444'>🚨 Critical Alerts</h3><ul>{items}</ul>"

        warning_html = ""
        if warnings:
            items = "".join(f"<li style='color:#f59e0b;margin:4px 0'>{f['message']}</li>" for f in warnings)
            warning_html = f"<h3 style='color:#f59e0b'>⚠️ Warnings</h3><ul>{items}</ul>"

        html_body = f"""
        <div style="font-family:system-ui;max-width:600px;margin:0 auto;padding:20px">
            <h2 style="color:#6366f1">₹ FinCoach — Nightly Report</h2>
            <p style="color:#666">Date: {analysis['date']} | Savings Score: {'⭐' * min(savings.get('score', 5), 10)} ({savings.get('score', 0)}/10)</p>

            {critical_html}
            {warning_html}

            {f'<div style="background:#f8f9fa;padding:16px;border-radius:8px;margin:16px 0"><h3>🤖 AI Analysis</h3><p style="white-space:pre-line">{llm_summary}</p></div>' if llm_summary else ''}

            <hr style="border:none;border-top:1px solid #eee;margin:20px 0">
            <p style="color:#999;font-size:12px">Monthly: Income ₹{savings.get('income', 0):,.0f} | Expenses ₹{savings.get('expense', 0):,.0f} | Savings ₹{savings.get('savings', 0):,.0f} ({savings.get('savings_rate', 0)}%)</p>
        </div>
        """

        subject_prefix = "🚨" if critical else "⚠️"
        subject = f"{subject_prefix} FinCoach: {len(critical)} critical, {len(warnings)} warnings — {analysis['date']}"

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": "FinCoach <noreply@fincoach.app>",
                    "to": [settings.NOTIFICATION_EMAIL],
                    "subject": subject,
                    "html": html_body,
                },
            )
            return resp.status_code == 200

    except Exception as e:
        print(f"Email send failed: {e}")
        return False

