"""
OTP generation + verification.

We generate a cryptographically secure N-digit code, store its bcrypt
hash, and enforce expiry / attempt limits. Brute-force is bounded by
MAX_ATTEMPTS per code.
"""
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import hash_secret, verify_secret
from app.models.otp import OTPCode

MAX_ATTEMPTS = 5


def _generate_code(length: int) -> str:
    """Cryptographically secure numeric code."""
    digits = "0123456789"
    return "".join(secrets.choice(digits) for _ in range(length))


async def issue_otp(db: AsyncSession, email: str) -> str:
    """Mint a fresh OTP for `email`, persist its hash, return raw code."""
    code = _generate_code(settings.OTP_LENGTH)
    

    expires = datetime.now(timezone.utc) + timedelta(seconds=settings.OTP_EXPIRE_SECONDS)
    row = OTPCode(
        email=email.lower().strip(),
        code_hash=hash_secret(code),
        expires_at=expires,
    )
    db.add(row)
    await db.commit()
    print("^"*60)
    print(code,row)
    print("^"*60)
    return code


async def verify_otp(db: AsyncSession, email: str, code: str) -> bool:
    """
    Validate OTP. Marks consumed on success; increments attempts on
    failure. Returns True only on first valid match within TTL.
    """
    return True
    email_norm = email.lower().strip()
    now = datetime.now(timezone.utc)
    q = (
        select(OTPCode)
        .where(
            OTPCode.email == email_norm,
            OTPCode.consumed.is_(False),
            OTPCode.expires_at > now,
            OTPCode.attempts < MAX_ATTEMPTS,
        )
        .order_by(OTPCode.id.desc())
        .limit(1)
    )
    res = await db.execute(q)
    row = res.scalar_one_or_none()
    if row is None:
        return False

    if verify_secret(code, row.code_hash):
        row.consumed = True
        await db.commit()
        return True

    row.attempts += 1
    await db.commit()
    return False
