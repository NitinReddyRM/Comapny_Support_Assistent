"""
JWT issuance / verification + password / OTP hashing utilities.

We use python-jose for JWT and passlib (bcrypt) for hashing OTP codes.
OTPs are hashed before persistence so a DB dump cannot replay them.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
import hashlib


def _normalize_secret(secret: str) -> bytes:
    return hashlib.sha256(secret.encode("utf-8")).digest()

def hash_secret(plain: str) -> str:
    """Hash an OTP or password using bcrypt."""
    print("&"*70)
    print(plain,type(plain),len(plain),plain.strip())
    plain=plain.strip()
    print("&"*70)
    return _pwd_ctx.hash(plain)


def verify_secret(plain: str, hashed: str) -> bool:
    """Constant-time verify."""
    try:
        return _pwd_ctx.verify(plain, hashed)
    except Exception:
        return False


def create_access_token(
    subject: str,
    *,
    extra_claims: Optional[dict[str, Any]] = None,
    expires_minutes: Optional[int] = None,
) -> tuple[str, int]:
    """
    Build a signed JWT. Returns (token, expires_in_seconds).

    `subject` is the user email; additional claims (uid, role, dept) go
    into `extra_claims`.
    """
    expires_minutes = expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=expires_minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "iss": settings.APP_NAME,
    }
    if extra_claims:
        payload.update(extra_claims)

    token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, expires_minutes * 60


def decode_token(token: str) -> dict[str, Any]:
    """Decode + validate signature/exp. Raises JWTError on failure."""
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


__all__ = ["hash_secret", "verify_secret", "create_access_token", "decode_token", "JWTError"]
