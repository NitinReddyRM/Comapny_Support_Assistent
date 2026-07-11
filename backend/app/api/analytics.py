"""
Admin analytics — DAU, top queries, dept usage, latency, guardrail counts.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import require_min_role
from app.database import get_db
from app.models.chat import ChatMessage, ChatSession, MessageRole
from app.models.department import Department
from app.models.feedback import Feedback, FeedbackKind
from app.models.user import User, UserRole
from app.schemas.admin import AnalyticsOverview

router = APIRouter(prefix="/analytics", tags=["analytics"])

_AdminUser = Depends(require_min_role(UserRole.ADMIN))


@router.get("/overview", response_model=AnalyticsOverview)
async def overview(_=_AdminUser, db: AsyncSession = Depends(get_db)):
    since = datetime.now(timezone.utc) - timedelta(days=1)

    dau = (await db.execute(
        select(func.count(func.distinct(ChatSession.user_id)))
        .where(ChatSession.updated_at >= since)
    )).scalar() or 0

    total_messages = (await db.execute(
        select(func.count(ChatMessage.id))
        .where(ChatMessage.created_at >= since)
    )).scalar() or 0

    total_sessions = (await db.execute(
        select(func.count(ChatSession.id))
        .where(ChatSession.created_at >= since)
    )).scalar() or 0

    avg_latency = (await db.execute(
        select(func.avg(ChatMessage.latency_ms))
        .where(ChatMessage.role == MessageRole.ASSISTANT,
               ChatMessage.created_at >= since)
    )).scalar() or 0.0

    avg_confidence = (await db.execute(
        select(func.avg(ChatMessage.confidence))
        .where(ChatMessage.role == MessageRole.ASSISTANT,
               ChatMessage.created_at >= since)
    )).scalar() or 0.0

    helpful = (await db.execute(
        select(func.count(Feedback.id))
        .where(Feedback.kind == FeedbackKind.HELPFUL,
               Feedback.created_at >= since)
    )).scalar() or 0
    not_helpful = (await db.execute(
        select(func.count(Feedback.id))
        .where(Feedback.kind == FeedbackKind.NOT_HELPFUL,
               Feedback.created_at >= since)
    )).scalar() or 0

    guardrail_violations = (await db.execute(
        select(func.count(ChatMessage.id))
        .where(ChatMessage.blocked_by_guardrail.is_(True),
               ChatMessage.created_at >= since)
    )).scalar() or 0

    top_q_rows = (await db.execute(
        select(ChatMessage.content, func.count(ChatMessage.id).label("c"))
        .where(ChatMessage.role == MessageRole.USER,
               ChatMessage.created_at >= since)
        .group_by(ChatMessage.content)
        .order_by(func.count(ChatMessage.id).desc())
        .limit(10)
    )).all()
    top_queries = [{"query": r[0][:160], "count": int(r[1])} for r in top_q_rows]

    dept_rows = (await db.execute(
        select(Department.code, func.count(ChatSession.id))
        .join(ChatSession, ChatSession.department_id == Department.id, isouter=True)
        .where(ChatSession.updated_at >= since)
        .group_by(Department.code)
        .order_by(func.count(ChatSession.id).desc())
    )).all()
    department_usage = [{"department": r[0], "sessions": int(r[1] or 0)} for r in dept_rows]

    return AnalyticsOverview(
        daily_active_users=int(dau),
        total_messages=int(total_messages),
        total_sessions=int(total_sessions),
        avg_latency_ms=float(avg_latency),
        avg_confidence=float(avg_confidence),
        feedback_helpful=int(helpful),
        feedback_not_helpful=int(not_helpful),
        guardrail_violations=int(guardrail_violations),
        top_queries=top_queries,
        department_usage=department_usage,
    )
