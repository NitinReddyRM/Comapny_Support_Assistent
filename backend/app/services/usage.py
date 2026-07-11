from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import RateLimited
from app.models.chat import ChatMessage, ChatSession
from app.models.user import User


def month_start_utc(now: datetime | None = None) -> datetime:
    """Return midnight UTC of the 1st of the current month."""
    n = now or datetime.now(timezone.utc)
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def get_user_monthly_tokens(db: AsyncSession, user_id: int) -> int:
    """Sum of input+output tokens for `user_id` since the 1st of this month."""
    start = month_start_utc()
    stmt = (
        select(
            func.coalesce(func.sum(
                func.coalesce(ChatMessage.tokens_input, 0)
                + func.coalesce(ChatMessage.tokens_output, 0)
            ), 0)
        )
        .select_from(ChatMessage)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .where(
            ChatSession.user_id == user_id,
            ChatMessage.created_at >= start,
        )
    )
    res = await db.execute(stmt)
    return int(res.scalar() or 0)


def effective_limit(user: User) -> int:
    """Per-user override (if any) else the system default. 0 means disabled."""
    if user.monthly_token_limit is not None:
        return max(0, int(user.monthly_token_limit))
    return max(0, int(settings.DEFAULT_MONTHLY_TOKEN_LIMIT))


async def enforce_monthly_budget(db: AsyncSession, user: User) -> tuple[int, int]:
    """
    Raise RateLimited if the user has already burned through their monthly
    token budget. Returns (used, limit) on success so the caller can
    surface "X / Y tokens used" telemetry.

    A limit of 0 disables enforcement (effectively unlimited).
    """
    limit = effective_limit(user)
    if limit <= 0:
        return 0, 0
    used = await get_user_monthly_tokens(db, user.id)
    if used >= limit:
        raise RateLimited()
    return used, limit
