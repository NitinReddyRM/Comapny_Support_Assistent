"""
Authentication endpoints — passwordless email OTP.

Flow:
  1. POST /auth/otp/request   -> mints OTP, emails it (SES)
  2. POST /auth/otp/verify    -> validates code, returns short-lived JWT
                                 (departments NOT yet selected)
  3a. POST /auth/department   -> picks a single dept (legacy/single users)
  3b. POST /auth/departments  -> picks one OR many depts (CrossAdmin /
                                 SuperAdmin); returns final JWT carrying
                                 `dept` (active) and `depts` (granted set).
"""
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.config import settings
from app.core.exceptions import AuthError, ForbiddenError, NotFoundError
from app.core.otp import issue_otp, verify_otp
from app.core.rate_limit import limiter
from app.core.rbac import (
    get_accessible_department_codes,
    is_cross_admin,
    is_global_admin,
)
from app.core.security import create_access_token
from app.database import get_db
from app.models.department import Department
from app.models.user import User, UserRole
from app.models.audit import AuditLog
from app.schemas.auth import (
    DepartmentSelect,
    DepartmentsSelect,
    OTPRequest,
    OTPVerify,
    TokenResponse,
    UserPublic,
)
from app.services.ses_service import send_otp_email
from app.services.usage import (
    effective_limit,
    get_user_monthly_tokens,
    month_start_utc,
)
from app.utils.logger import log_event

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_public(u: User, dept: Department | None, dept_codes: List[str] | None = None) -> UserPublic:
    role = u.role.value if hasattr(u.role, "value") else str(u.role)
    home = dept or u.department
    return UserPublic(
        id=u.id,
        email=u.email,
        full_name=u.full_name,
        role=role,
        department_code=home.code if home else None,
        department_name=home.name if home else None,
        department_codes=dept_codes or ([home.code] if home else []),
    )


def _multi_dept_role(user: User) -> bool:
    """Roles permitted to span multiple departments at once."""
    return user.role in (UserRole.CROSSADMIN, UserRole.SUPERADMIN)


# ---------------------------------------------------------------------------
# OTP request / verify
# ---------------------------------------------------------------------------

@router.post("/otp/request", status_code=204)
async def request_otp(payload: OTPRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Mint and email an OTP. Auto-creates the user on first request."""
    print("%"*60)
    print("/otp/request")
    print("%"*60)
    ip = request.client.host if request.client else "?"
    await limiter.check(f"otp:{payload.email}:{ip}", limit=5, window_seconds=300)

    email = payload.email.lower().strip()
    res = await db.execute(select(User).where(User.email == email))
    print("%"*60)
    print(email,res)
    print("%"*60)
    user = res.scalar_one_or_none()
    if user is None:
        # First-time login: provision a USER (SUPERADMIN if matches env).
        role = UserRole.SUPERADMIN if email == settings.SUPERADMIN_EMAIL.lower() else UserRole.USER
        user = User(email=email, role=role)
        db.add(user)
        await db.commit()
        await db.refresh(user)

    code = await issue_otp(db, email)
    # await send_otp_email(email, code)

    db.add(AuditLog(
        user_id=user.id, user_email=email, action="OTP_REQUESTED",
        ip_address=ip, details={"email": email},
    ))
    await db.commit()
    log_event("auth", "info", "OTP issued", email=email, ip=ip)


@router.post("/otp/verify", response_model=TokenResponse)
async def verify_otp_endpoint(
    payload: OTPVerify, request: Request, db: AsyncSession = Depends(get_db),
):
    """Verify OTP, return a partial JWT (no dept yet). Client must then call
    /auth/department (single) or /auth/departments (multi)."""
    ip = request.client.host if request.client else "?"
    await limiter.check(f"otpv:{payload.email}:{ip}", limit=10, window_seconds=300)

    email = payload.email.lower().strip()
    ok = await verify_otp(db, email, payload.code)
    if not ok:
        log_event("security", "warning", "OTP failed", email=email, ip=ip)
        raise AuthError("Invalid or expired code")

    res = await db.execute(
        select(User)
        .options(selectinload(User.extra_departments))
        .where(User.email == email)
    )
    user = res.scalar_one_or_none()
    if not user:
        raise AuthError("User not found")

    user.last_login_at = datetime.now(timezone.utc)
    db.add(AuditLog(
        user_id=user.id, user_email=email, action="OTP_VERIFIED", ip_address=ip,
    ))
    await db.commit()

    token, exp = create_access_token(
        subject=user.email,
        extra_claims={"uid": user.id, "role": user.role.value, "stage": "pending_dept"},
        expires_minutes=15,
    )
    log_event("auth", "info", "OTP verified", email=email)
    return TokenResponse(
        access_token=token, expires_in=exp,
        user=_user_public(user, user.department),
    )


# ---------------------------------------------------------------------------
# Department selection
# ---------------------------------------------------------------------------

@router.post("/department", response_model=TokenResponse)
async def select_department(
    payload: DepartmentSelect,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Legacy single-department selection. Re-issues the full-scope JWT."""
    res = await db.execute(select(Department).where(Department.code == payload.department_code.lower()))
    dept = res.scalar_one_or_none()
    if not dept or not dept.is_active:
        raise NotFoundError("Unknown or inactive department")

    # First-login auto-assign: a freshly-provisioned USER without a
    # home dept picks one here. Everyone else must already match.
    if user.role == UserRole.USER and user.department_id is None:
        user.department_id = dept.id
        await db.commit()
        await db.refresh(user)

    # Scope check — must be in the caller's accessible set.
    granted = await get_accessible_department_codes(db, user)
    if dept.code not in granted:
        raise ForbiddenError("Department not in your scope")

    if user.role == UserRole.CROSSADMIN:
        # Full granted set is exposed via `depts` so the client can
        # query across all of them.
        depts = granted
    elif user.role == UserRole.SUPERADMIN:
        depts = granted
    else:
        depts = [dept.code]

    token, exp = create_access_token(
        subject=user.email,
        extra_claims={
            "uid": user.id,
            "role": user.role.value,
            "dept": dept.code,
            "depts": depts,
            "stage": "active",
        },
    )
    db.add(AuditLog(
        user_id=user.id, user_email=user.email, action="DEPT_SELECTED",
        details={"dept": dept.code},
    ))
    await db.commit()
    log_event("auth", "info", "Department selected", email=user.email, dept=dept.code)
    return TokenResponse(
        access_token=token,
        expires_in=exp,
        user=_user_public(user, dept, depts),
    )


@router.post("/departments", response_model=TokenResponse)
async def select_departments(
    payload: DepartmentsSelect,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Select one OR many departments.

    Single-dept roles must pass exactly one code. CrossAdmin / SuperAdmin
    may pass any subset of their granted (or all, for SuperAdmin)
    departments. The first code becomes the *active* dept used for ticket
    creation, audit context, etc.
    """
    codes = [c.lower().strip() for c in (payload.department_codes or []) if c and c.strip()]
    if not codes:
        raise NotFoundError("No departments selected")

    # Deduplicate while preserving order.
    seen, ordered = set(), []
    for c in codes:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    codes = ordered

    if not _multi_dept_role(user) and len(codes) > 1:
        raise ForbiddenError("Your role can only select a single department")

    # Resolve to Department rows + sanity-check active.
    res = await db.execute(
        select(Department).where(Department.code.in_(codes), Department.is_active.is_(True))
    )
    depts_by_code = {d.code: d for d in res.scalars().all()}
    missing = [c for c in codes if c not in depts_by_code]
    if missing:
        raise NotFoundError(f"Unknown or inactive department(s): {', '.join(missing)}")

    selected = [depts_by_code[c] for c in codes]
    active = selected[0]

    # Enforce that the selection is a subset of what the user is *allowed*
    # to see. SUPERADMIN/ADMIN-via-allowlist pass; CROSSADMIN must be
    # granted; single-dept users must match home dept.
    granted_codes = set(await get_accessible_department_codes(db, user))
    if user.role == UserRole.CROSSADMIN:
        bad = [c for c in codes if c not in granted_codes]
        if bad:
            raise ForbiddenError(f"Department(s) not granted: {', '.join(bad)}")
    elif user.role == UserRole.USER:
        if user.department_id is None:
            user.department_id = active.id
            await db.commit()
        elif user.department_id != active.id:
            raise ForbiddenError("Cannot access another department")
    elif user.role == UserRole.ADMIN:
        if active.code not in granted_codes:
            raise ForbiddenError("Department not in your scope")
    # SUPERADMIN may access any active department.

    token, exp = create_access_token(
        subject=user.email,
        extra_claims={
            "uid": user.id,
            "role": user.role.value,
            "dept": active.code,
            "depts": codes,
            "stage": "active",
        },
    )
    db.add(AuditLog(
        user_id=user.id, user_email=user.email, action="DEPTS_SELECTED",
        details={"active": active.code, "all": codes},
    ))
    await db.commit()
    log_event("auth", "info", "Departments selected",
              email=user.email, dept=active.code, depts=codes)
    return TokenResponse(
        access_token=token,
        expires_in=exp,
        user=_user_public(user, active, codes),
    )


# ---------------------------------------------------------------------------
# Identity / catalog
# ---------------------------------------------------------------------------

@router.get("/me", response_model=UserPublic)
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Return the caller's identity + ACTIVE department context.

    JWT-encoded codes may be stale (a dept can be deactivated after the
    user logged in), so we re-resolve everything against `is_active=True`
    here. That guarantees the chat sidebar's brand chip, the multi-dept
    count, and any /auth/me-driven UI never surface a dead department.
    """
    # Authoritative set: every dept the user is currently allowed to read.
    active_codes = set(await get_accessible_department_codes(db, user))

    # Resolve the home/active dept claim from the JWT — but only honour
    # it if that dept is still active and in the user's accessible set.
    home_code = getattr(user, "active_department_code", None) \
        or (user.department.code if user.department else None)
    home: Department | None = None
    if home_code and home_code in active_codes:
        home = (await db.execute(
            select(Department).where(
                Department.code == home_code,
                Department.is_active.is_(True),
            )
        )).scalar_one_or_none()
    # Fallback: if the JWT-claimed home is gone, pick any active code.
    if not home and active_codes:
        first = sorted(active_codes)[0]
        home = (await db.execute(
            select(Department).where(Department.code == first)
        )).scalar_one_or_none()

    # Intersect JWT-encoded multi-dept claim with the active set so a
    # multi-dept user doesn't keep showing a deactivated dept in the
    # sidebar count.
    jwt_codes = getattr(user, "active_department_codes", None) or []
    if jwt_codes:
        codes = sorted(set(jwt_codes) & active_codes)
    else:
        codes = sorted(active_codes)

    return _user_public(user, home, codes)


def _next_month_start(d):
    """First instant of next calendar month, UTC."""
    from calendar import monthrange
    last_day = monthrange(d.year, d.month)[1]
    return d.replace(day=last_day, hour=23, minute=59, second=59, microsecond=0) \
        .replace(hour=0, minute=0, second=0, microsecond=0) \
        + timedelta(days=1)


@router.get("/dept-admins/{code}")
async def public_dept_admins(
    code: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Auth-only contact lookup. Chat shows this when a logged-in user
    discovers their only dept is inactive and can't message anyone."""
    # Lightweight scope check: the user must already have or have-had
    # a grant for this dept (otherwise we'd be doxxing admin emails).
    granted = set(await get_accessible_department_codes(db, user))
    home = (user.department.code if user.department else None)
    allowed = {*(granted or set()), *(([home] if home else []))}
    if code.lower() not in {c.lower() for c in allowed}:
        raise ForbiddenError("Not your department")
    # Reuse the admin module's resolver — pure-DB, no admin gate.
    from app.api.admin import _admins_for_dept
    return await _admins_for_dept(db, code)


@router.get("/me/usage")
async def my_usage(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Real-time per-user monthly LLM-token budget for the chat UI.

    Returns the calendar-month window, current spend, configured cap,
    and whether the cap comes from the user row or the system default.
    Cheap query: a single indexed SUM on chat_messages.
    """
    used = await get_user_monthly_tokens(db, user.id)
    limit = effective_limit(user)
    period_start = month_start_utc()
    period_end = _next_month_start(period_start)
    source = "user" if user.monthly_token_limit is not None else "system_default"
    return {
        "used": used,
        "limit": limit,
        "remaining": max(0, limit - used) if limit > 0 else None,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "limit_source": source,
        "exceeded": (limit > 0 and used >= limit),
    }


@router.get("/departments")
async def list_departments(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List the departments this user may select at login.

    - USER: their home dept only (or all active if no home has been
      assigned yet — happens on first login).
    - ADMIN / CROSSADMIN / SUPERADMIN: every active department they're
      entitled to (CROSSADMIN is filtered to their grants).
    """
    granted = set(await get_accessible_department_codes(db, user))
    # First-login USERS won't have a home dept yet: show all active so they
    # can pick once.
    if not granted and user.role == UserRole.USER:
        res = await db.execute(
            select(Department).where(Department.is_active.is_(True)).order_by(Department.name)
        )
        return [{"code": d.code, "name": d.name, "description": d.description}
                for d in res.scalars()]

    res = await db.execute(
        select(Department)
        .where(Department.is_active.is_(True), Department.code.in_(granted))
        .order_by(Department.name)
    )
    return [{"code": d.code, "name": d.name, "description": d.description}
            for d in res.scalars()]
