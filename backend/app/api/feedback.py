"""
Feedback endpoints. 👍 stores positive feedback; 👎 auto-creates a
ticket, uploads the chat transcript to S3, and emails support.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_current_department
from app.core.exceptions import NotFoundError
from app.database import get_db
from app.models.chat import ChatMessage, ChatSession, MessageRole
from app.models.department import Department
from app.models.feedback import Feedback, FeedbackKind
from app.models.ticket import Ticket, TicketPriority, TicketStatus
from app.models.user import User
from app.schemas.ticket import FeedbackRequest
from app.services import s3_service
from app.services.outlook_service import send_ticket_email
from app.utils.logger import log_event

router = APIRouter(prefix="/feedback", tags=["feedback"])


async def _build_transcript(db: AsyncSession, session_id: int) -> str:
    msgs = (await db.execute(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id)
    )).scalars().all()
    out = []
    for m in msgs:
        out.append(f"[{m.created_at.isoformat()}] {m.role.value.upper()}:\n{m.content}\n")
    return "\n".join(out)


@router.post("", status_code=201)
async def submit_feedback(
    payload: FeedbackRequest,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
    dept: Department = Depends(get_current_department),
    db: AsyncSession = Depends(get_db),
):
    msg = (await db.execute(
        select(ChatMessage).where(ChatMessage.id == payload.message_id)
    )).scalar_one_or_none()
    if not msg or msg.role != MessageRole.ASSISTANT:
        raise NotFoundError("Message not found")

    sess = (await db.execute(
        select(ChatSession).where(ChatSession.id == msg.session_id)
    )).scalar_one_or_none()
    if not sess or sess.user_id != user.id:
        raise NotFoundError("Message not yours")

    kind = FeedbackKind(payload.kind.upper())

    # Upsert: one feedback per message.
    existing = (await db.execute(
        select(Feedback).where(Feedback.message_id == msg.id)
    )).scalar_one_or_none()
    if existing:
        existing.kind = kind
        existing.comment = payload.comment
        fb = existing
    else:
        fb = Feedback(user_id=user.id, message_id=msg.id, kind=kind, comment=payload.comment)
        db.add(fb)
    await db.commit()

    log_event("feedback", "info", "feedback",
              email=user.email, dept=dept.code, message_id=msg.id, kind=kind.value)

    # 👎 → auto-create a ticket + email support.
    if kind == FeedbackKind.NOT_HELPFUL:
        # Find the preceding user message for ticket context.
        prior_user_msg = (await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == msg.session_id,
                   ChatMessage.id < msg.id,
                   ChatMessage.role == MessageRole.USER)
            .order_by(ChatMessage.id.desc())
            .limit(1)
        )).scalar_one_or_none()
        original_query = prior_user_msg.content if prior_user_msg else "(unknown)"

        # Comment is now mandatory from the UI — it carries the user's
        # description of what was wrong. Include it in the ticket subject
        # and email body so support has immediate context.
        user_reason = (payload.comment or "").strip() or "(no reason provided)"
        snippet = user_reason[:80] + ("…" if len(user_reason) > 80 else "")

        ticket = Ticket(
            user_id=user.id,
            department_id=dept.id,
            subject=f"[Not Helpful] {snippet}",
            query=original_query,
            ai_response=msg.content,
            chat_session_id=sess.id,
            status=TicketStatus.OPEN,
            priority=TicketPriority.MEDIUM,
            resolution_notes=None,
        )
        db.add(ticket)
        await db.commit()
        await db.refresh(ticket)

        # Heavy work goes to a background task so the user gets an
        # immediate ACK.
        transcript = await _build_transcript(db, sess.id)
        background.add_task(
            _notify_support_async,
            ticket_id=ticket.id,
            user_email=user.email,
            dept_code=dept.code,
            dept_name=dept.name,
            support_email=dept.support_email or "",
            subject=ticket.subject,
            query=original_query,
            ai_response=msg.content,
            user_reason=user_reason,
            transcript=transcript,
        )

        return {"status": "ticket_created", "ticket_id": ticket.id}

    return {"status": "ok"}


async def _notify_support_async(
    *, ticket_id: int, user_email: str, dept_code: str, dept_name: str,
    support_email: str, subject: str, query: str, ai_response: str,
    user_reason: str, transcript: str,
):
    # Upload the transcript to S3 for retention; the key is stored on
    # the ticket row.
    s3_key = await s3_service.upload_transcript(
        user_email=user_email, department_code=dept_code, content=transcript,
    )

    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        t = (await db.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one_or_none()
        if t and s3_key:
            t.transcript_s3_key = s3_key
            await db.commit()

    body = (
        f"A user reported an unhelpful AI response in {dept_name}.\n\n"
        f"Ticket: #{ticket_id}\n"
        f"User:  {user_email}\n"
        f"Department: {dept_name} ({dept_code})\n\n"
        f"User-reported issue:\n{user_reason}\n\n"
        f"Original query:\n{query}\n\n"
        f"AI response:\n{ai_response}\n\n"
        "Full chat transcript attached."
    )
    recipients = [support_email] if support_email else []
    if not recipients:
        from app.config import settings
        recipients = [settings.SUPERADMIN_EMAIL]

    await send_ticket_email(
        to=recipients,
        subject=f"[Company AI Ticket #{ticket_id}] {subject}",
        body_text=body,
        transcript_text=transcript,
        transcript_filename=f"ticket_{ticket_id}_transcript.txt",
    )
    log_event("feedback", "info", "ticket email dispatched",
              ticket_id=ticket_id, to=recipients)
