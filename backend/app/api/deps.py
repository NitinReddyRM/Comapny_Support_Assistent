"""
Shared FastAPI dependencies.

`get_current_user` validates the JWT, loads the user, and ensures the
user has selected a department (claim `dept`). Any router that operates
on user data should depend on this — never trust client-side state.
"""
from typing import Optional

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import AuthError
from app.core.security import decode_token, JWTError
from app.database import get_db
from app.models.user import User
from app.models.department import Department


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(None),
) -> User:
    token: Optional[str] = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
    # Allow token in query for WebSocket upgrade (browsers can't set
    # custom headers on the ws:// handshake easily).
    if not token:
        token = request.query_params.get("token")
    if not token:
        raise AuthError("Missing bearer token")

    try:
        payload = decode_token(token)
    except JWTError as e:
        raise AuthError(f"Invalid token: {e}")

    email = payload.get("sub")
    if not email:
        raise AuthError("Token missing subject")

    res = await db.execute(
        select(User)
        .options(selectinload(User.extra_departments))
        .where(User.email == email)
    )
    user = res.scalar_one_or_none()
    if user is None or not user.is_active:
        raise AuthError("User not found or inactive")

    # Surface JWT claims on the user object (transient). `dept` is the
    # currently-active dept; `depts` is the full granted set for the
    # session — present for multi-dept roles.
    user.active_department_code = payload.get("dept")
    depts_claim = payload.get("depts")
    if isinstance(depts_claim, list):
        user.active_department_codes = [str(c) for c in depts_claim if c]
    elif user.active_department_code:
        user.active_department_codes = [user.active_department_code]
    else:
        user.active_department_codes = []
    return user


async def get_current_department(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Department:
    """Return the *active* department for the session (for audit, ticket
    routing, single-dept retrieval, …)."""
    code = getattr(user, "active_department_code", None) or (
        user.department.code if user.department else None
    )
    if not code:
        raise AuthError("Department not selected")
    res = await db.execute(select(Department).where(Department.code == code))
    dept = res.scalar_one_or_none()
    if not dept or not dept.is_active:
        raise AuthError("Department not available")
    return dept


async def get_active_department_codes(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[str]:
    """Return the full set of department codes the current session may
    query against — single-element list for normal users, multi-element
    for CrossAdmin / SuperAdmin who picked several at login.

    Filtered to *active* departments only.
    """
    codes = getattr(user, "active_department_codes", None) or []
    if not codes:
        # Fallback to home department (legacy single-dept JWT).
        code = getattr(user, "active_department_code", None) or (
            user.department.code if user.department else None
        )
        if not code:
            raise AuthError("Department not selected")
        codes = [code]

    res = await db.execute(
        select(Department.code).where(
            Department.code.in_(codes),
            Department.is_active.is_(True),
        )
    )
    active = [r for r in res.scalars().all()]
    if not active:
        raise AuthError("No active department for this session")
    return active
