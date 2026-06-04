from pydantic_settings import BaseSettings
from functools import lru_cache
import os


class Settings(BaseSettings):
    # App
    APP_NAME: str = "FinCoach"
    APP_ENV: str = "development"
    DEBUG: bool = True
    SECRET_KEY: str = "change-me-in-production-use-openssl-rand-hex-32"

    # Database (Supabase Postgres)
    DATABASE_URL: str = "postgresql+asyncpg://postgres:password@localhost:5432/fincoach"

    # Supabase
    SUPABASE_URL: str = ""
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""

    # OpenAI / OpenRouter
    OPENAI_API_KEY: str = ""
    OPENAI_API_BASE_URL: str = ""
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/v1"
    OPENROUTER_MODEL: str = "gpt-4o-mini"
    AI_MODEL: str = "gpt-4o-mini"

    # Encryption key for stored PDFs (Fernet)
    FILE_ENCRYPTION_KEY: str = ""

    # Admin auth
    ADMIN_EMAIL: str = "admin@fincoach.app"
    ADMIN_PASSWORD: str = "change-me"
    ADMIN_OTP: str = "123456"
    JWT_SECRET: str = "change-me-jwt-secret-use-openssl-rand-hex-32"
    JWT_EXPIRY_HOURS: int = 24

    # Notifications (Resend free tier: 3000 emails/month)
    RESEND_API_KEY: str = ""
    NOTIFICATION_EMAIL: str = ""  # Where to send nightly reports

    # Upload
    UPLOAD_DIR: str = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "uploads")
    MAX_UPLOAD_SIZE_MB: int = 20

    # CORS - stored as comma-separated string to avoid pydantic-settings JSON parsing issues
    CORS_ORIGINS: str = "http://localhost:3000"

    @property
    def cors_origins_list(self) -> list[str]:
        import json
        try:
            return json.loads(self.CORS_ORIGINS)
        except (json.JSONDecodeError, TypeError):
            return [s.strip() for s in self.CORS_ORIGINS.split(",") if s.strip()]

    # Budget defaults
    DEFAULT_ALERT_THRESHOLD_PCT: int = 80  # warn at 80% of budget

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


@lru_cache()
def get_settings() -> Settings:
    return Settings()
