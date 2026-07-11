"""Helpdesk ticket endpoints — user-side + admin-side."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user, get_current_department
from app.core.exceptions import NotFoundError, ForbiddenError
from app.database import get_db
from app.models.department import Department
from app.models.ticket import Ticket, TicketComment, TicketStatus, TicketPriority
from app.models.user import User, UserRole
from app.schemas.ticket import (
    TicketCreate, TicketUpdate, TicketOut, TicketCommentCreate, TicketCommentOut,
)
from app.utils.logger import log_event

router = APIRouter(prefix="/tickets", tags=["tickets"])


def _is_admin(u: User) -> bool:
    return u.role in (UserRole.ADMIN, UserRole.CROSSADMIN, UserRole.SUPERADMIN)


def _is_dept_scoped_admin(u: User) -> bool:
    """Admin variants that see only their own dept's tickets.

    SUPERADMIN sees everything; ADMIN and CROSSADMIN see only the
    ticket(s) in their scope.
    """
    return u.role in (UserRole.ADMIN, UserRole.CROSSADMIN)


@router.post("", response_model=TicketOut, status_code=201)
async def create_ticket(
    payload: TicketCreate,
    user: User = Depends(get_current_user),
    dept: Department = Depends(get_current_department),
    db: AsyncSession = Depends(get_db),
):
    t = Ticket(
        user_id=user.id, department_id=dept.id,
        subject=payload.subject, query=payload.query,
        ai_response=payload.ai_response, chat_session_id=payload.chat_session_id,
        priority=TicketPriority(payload.priority.upper()) if payload.priority else TicketPriority.MEDIUM,
    )
    db.add(t)
    await db.commit()
    # Reload with comments eager-loaded so TicketOut can serialise safely.
    await db.refresh(t, attribute_names=["comments"])
    
    log_event("audit", "info", "ticket created", ticket_id=t.id, email=user.email)
    return t
    

@router.get("", response_model=List[TicketOut])
async def list_tickets(
    status: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # `selectinload(Ticket.comments)` is *required* — Pydantic touches the
    # `.comments` relationship while serialising TicketOut; async SQLAlchemy
    # cannot perform a lazy load inside the serialization path and would
    # raise MissingGreenlet.
    q = select(Ticket).options(selectinload(Ticket.comments))
    if not _is_admin(user):
        q = q.where(Ticket.user_id == user.id)
    elif _is_dept_scoped_admin(user):
        # ADMIN and CROSSADMIN see only tickets in their scope.
        from app.core.rbac import get_accessible_department_codes
        from app.models.department import Department
        scope = await get_accessible_department_codes(db, user)
        if not scope:
            return []
        dept_ids = (await db.execute(
            select(Department.id).where(Department.code.in_(scope))
        )).scalars().all()
        if not dept_ids:
            return []
        q = q.where(Ticket.department_id.in_(list(dept_ids)))
    if status:
        q = q.where(Ticket.status == TicketStatus(status.upper()))
    q = q.order_by(desc(Ticket.updated_at)).limit(200)
    rows = (await db.execute(q)).scalars().all()
    return rows


@router.get("/{ticket_id}", response_model=TicketOut)
async def get_ticket(
    ticket_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = (await db.execute(
        select(Ticket)
        .options(selectinload(Ticket.comments))
        .where(Ticket.id == ticket_id)
    )).scalar_one_or_none()
    if not t:
        raise NotFoundError("Ticket not found")
    if not _is_admin(user) and t.user_id != user.id:
        raise ForbiddenError()
    if _is_dept_scoped_admin(user):
        from app.core.rbac import get_accessible_department_codes
        from app.models.department import Department
        scope = set(await get_accessible_department_codes(db, user))
        # Resolve ticket's dept code for the check.
        d = (await db.execute(
            select(Department.code).where(Department.id == t.department_id)
        )).scalar_one_or_none()
        if not d or d not in scope:
            raise ForbiddenError()
    return t


@router.patch("/{ticket_id}", response_model=TicketOut)
async def update_ticket(
    ticket_id: int,
    payload: TicketUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not _is_admin(user):
        raise ForbiddenError("Admins only")

    # selectinload(comments) here is the single source of truth: once
    # the relationship is loaded on the original SELECT, the subsequent
    # commit/refresh keeps it populated and Pydantic can serialise
    # TicketOut without triggering an illegal async lazy-load.
    t = (await db.execute(
        select(Ticket)
        .options(selectinload(Ticket.comments))
        .where(Ticket.id == ticket_id)
    )).scalar_one_or_none()
    if not t:
        raise NotFoundError()
    if _is_dept_scoped_admin(user):
        from app.core.rbac import get_accessible_department_codes
        from app.models.department import Department
        scope = set(await get_accessible_department_codes(db, user))
        d = (await db.execute(
            select(Department.code).where(Department.id == t.department_id)
        )).scalar_one_or_none()
        if not d or d not in scope:
            raise ForbiddenError()

    if payload.status:
        t.status = TicketStatus(payload.status.upper())
    if payload.priority:
        t.priority = TicketPriority(payload.priority.upper())
    if payload.assignee_id is not None:
        t.assignee_id = payload.assignee_id
    if payload.resolution_notes is not None:
        t.resolution_notes = payload.resolution_notes

    await db.commit()
    # Only refresh the scalar columns we just wrote — `comments` is
    # already loaded by selectinload and must not be touched here
    # (a bare refresh(t) would drop it from the loaded set).
    await db.refresh(
        t,
        attribute_names=["status", "priority", "assignee_id",
                         "resolution_notes", "updated_at"],
    )

    log_event(
        "audit", "info", "ticket updated",
        ticket_id=t.id, by=user.email, status=t.status.value,
    )
    return t


@router.post("/{ticket_id}/comments", response_model=TicketCommentOut)
async def add_comment(
    ticket_id: int,
    payload: TicketCommentCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = (await db.execute(select(Ticket).where(Ticket.id == ticket_id))).scalar_one_or_none()
    if not t:
        raise NotFoundError()
    if not _is_admin(user) and t.user_id != user.id:
        raise ForbiddenError()
    c = TicketComment(
        ticket_id=t.id, author_id=user.id,
        body=payload.body, is_internal=payload.is_internal,
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c
