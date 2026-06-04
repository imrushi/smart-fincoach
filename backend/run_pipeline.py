#!/usr/bin/env python3
"""Run the full pipeline: reconcile, self-transfers, categorize, budget alerts."""
import asyncio
from app.core.database import async_session
from app.services.reconciliation import reconcile_all, detect_self_transfers
from app.services.categorization import categorize_all_uncategorized
from app.services.budget_alerts import check_budgets_and_alert


async def run():
    async with async_session() as db:
        print("Step 1: Reconciling...")
        recon = await reconcile_all(db)
        print(f"  {recon}")

        print("Step 2: Detecting self-transfers...")
        transfers = await detect_self_transfers(db)
        print(f"  {transfers} detected")

        print("Step 3: Categorizing (this may call OpenAI/OpenRouter for new merchants)...")
        count = await categorize_all_uncategorized(db)
        print(f"  {count} transactions categorized")

        print("Step 4: Checking budgets...")
        alerts = await check_budgets_and_alert(db)
        print(f"  {alerts}")

        await db.commit()
        print("\nDone! All committed.")


asyncio.run(run())

