"""FastAPI application entry point."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import engine, Base, async_session
from app.core.seed import seed_categories_and_rules
from app.api import uploads, transactions, categories, dashboard
from app.api.auth import router as auth_router, verify_token
from fastapi import Depends


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables + seed
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as db:
        await seed_categories_and_rules(db)
    yield
    # Shutdown
    await engine.dispose()


settings = get_settings()

app = FastAPI(
    title=settings.APP_NAME,
    description="Smart personal finance ledger with AI-powered categorization & coaching",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(auth_router)  # Public (login endpoint)
app.include_router(uploads.router, dependencies=[Depends(verify_token)])
app.include_router(transactions.router, dependencies=[Depends(verify_token)])
app.include_router(categories.router, dependencies=[Depends(verify_token)])
app.include_router(dashboard.router, dependencies=[Depends(verify_token)])


@app.get("/api/health")
async def health():
    return {"status": "ok", "app": settings.APP_NAME}

