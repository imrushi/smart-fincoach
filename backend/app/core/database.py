from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from app.core.config import get_settings

settings = get_settings()

# Ensure the URL uses asyncpg driver
_db_url = settings.DATABASE_URL
if _db_url.startswith("postgresql://"):
    _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
elif _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql+asyncpg://", 1)

# asyncpg needs ssl as a connect_arg, not a query param
# Always disable statement_cache_size for pgbouncer compatibility
_connect_args = {"statement_cache_size": 0}
if "supabase" in _db_url or "pooler" in _db_url or settings.APP_ENV == "production":
    import ssl
    _ssl_ctx = ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = ssl.CERT_NONE
    _connect_args["ssl"] = _ssl_ctx
    # Remove sslmode param if present (asyncpg doesn't support it in URL)
    if "?sslmode" in _db_url:
        _db_url = _db_url.split("?sslmode")[0]
    elif "&sslmode" in _db_url:
        _db_url = _db_url.split("&sslmode")[0]

# Use NullPool when behind pgbouncer (it handles pooling)
_use_nullpool = "pooler" in _db_url or "supabase" in _db_url or settings.APP_ENV == "production"
_pool_kwargs = {"poolclass": NullPool} if _use_nullpool else {"pool_size": 5, "max_overflow": 10}

engine = create_async_engine(
    _db_url,
    echo=False,
    connect_args=_connect_args,
    pool_pre_ping=True,
    **_pool_kwargs,
)

# Disable prepared statements at the engine level for pgbouncer compatibility
from sqlalchemy import event

@event.listens_for(engine.sync_engine, "connect")
def _on_connect(dbapi_conn, connection_record):
    dbapi_conn._statement_cache_size = 0
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

