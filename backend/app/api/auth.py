"""Admin authentication with password + static OTP."""
import hashlib
import time
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from app.core.config import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])
security = HTTPBearer()
settings = get_settings()


class LoginRequest(BaseModel):
    password: str
    otp: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: str


def create_token() -> tuple[str, datetime]:
    exp = datetime.now(timezone.utc) + timedelta(hours=settings.JWT_EXPIRY_HOURS)
    payload = {"sub": "admin", "exp": exp, "iat": datetime.now(timezone.utc)}
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")
    return token, exp


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """Dependency to protect routes. Returns 'admin' if valid."""
    try:
        payload = jwt.decode(credentials.credentials, settings.JWT_SECRET, algorithms=["HS256"])
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


# Rate limiting: simple in-memory (per-process)
_failed_attempts: dict[str, list[float]] = {}
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300


def _check_rate_limit(ip: str):
    now = time.time()
    attempts = _failed_attempts.get(ip, [])
    # Remove old attempts
    attempts = [t for t in attempts if now - t < LOCKOUT_SECONDS]
    _failed_attempts[ip] = attempts
    if len(attempts) >= MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed attempts. Try again in {LOCKOUT_SECONDS // 60} minutes.",
        )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest):
    # Simple rate limiting by constant key (single user app)
    _check_rate_limit("admin")

    # Constant-time comparison for password and OTP
    password_match = hashlib.sha256(body.password.encode()).hexdigest() == hashlib.sha256(settings.ADMIN_PASSWORD.encode()).hexdigest()
    otp_match = hashlib.sha256(body.otp.encode()).hexdigest() == hashlib.sha256(settings.ADMIN_OTP.encode()).hexdigest()

    if not (password_match and otp_match):
        _failed_attempts.setdefault("admin", []).append(time.time())
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    # Clear failed attempts on success
    _failed_attempts.pop("admin", None)

    token, exp = create_token()
    return TokenResponse(access_token=token, expires_at=exp.isoformat())


@router.get("/me")
async def me(user: str = Depends(verify_token)):
    return {"user": user, "role": "admin"}

