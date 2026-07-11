"""
Admin endpoints — departments, users, KB uploads, KB management,
prompts, audit log.

Access matrix:
  * ADMIN       — limited to their own department (KB management + users).
  * CROSSADMIN  — KB read/manage across their granted departments.
  * SUPERADMIN  — everything, including permanent department delete.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, Query, UploadFile
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.core.exceptions import ForbiddenError, GuardrailBlocked, NotFoundError
from app.core.metadata import normalize_metadata
from app.core.model_catalog import available_models
from app.services.app_settings import (
    SCOPE_STANDARD, get_active_model, set_active_model,
    get_user_models as get_user_models_svc,
    set_user_models as set_user_models_svc,
)
from app.core.rbac import (
    can_manage_kb,
    get_accessible_department_codes,
    is_global_admin,
    require_min_role,
    require_role,
    role_rank,
)
from app.database import AsyncSessionLocal, get_db
from app.models.audit import AuditLog
from app.models.department import Department
from app.models.chat import ChatMessage, ChatSession, MessageRole
from app.models.kb_document import KBDocument, KBDocumentStatus
from app.models.user import User, UserRole, user_departments
from app.schemas.admin import (
    DepartmentCreate, DepartmentUpdate, DepartmentOut,
    KBDocumentOut, KBDocumentListResponse,
    UserCreate, UserUpdate, UserOut, UploadResult, BulkUploadResult,
    UserModelsPayload,
)
from app.schemas.chat import ModelSelect
from app.services import bedrock_service, s3_service
from app.services.usage import get_user_monthly_tokens
from app.utils.file_parser import is_supported
from app.utils.logger import log_event

router = APIRouter(prefix="/admin", tags=["admin"])

# Most admin routes require at least ADMIN. Some require ADMIN+/SUPERADMIN
# and they declare that explicitly per-route.
_AdminUser = Depends(require_min_role(UserRole.ADMIN))
# Department management (create / activate / deactivate / delete / patch)
# is open to CROSSADMIN and SUPERADMIN. CROSSADMIN is additionally scoped
# to their granted departments via `_assert_dept_in_scope` below.
_DeptManager = Depends(require_min_role(UserRole.CROSSADMIN))


async def _assert_dept_in_scope(db: AsyncSession, admin: User, dept: Department) -> None:
    """CROSSADMIN may only manage departments they're granted; SUPERADMIN
    may manage any. Raises ForbiddenError otherwise.

    Unlike `get_accessible_department_codes`, this counts a grant even when
    the department is *inactive* — otherwise a CROSSADMIN couldn't
    re-activate a department they own. We read the grants straight from
    the association table to avoid an async lazy-load on the relationship.
    """
    if admin.role == UserRole.SUPERADMIN:
        return
    granted: set[str] = set()
    if admin.department_id:
        home = (await db.execute(
            select(Department.code).where(Department.id == admin.department_id)
        )).scalar_one_or_none()
        if home:
            granted.add(home)
    extra = (await db.execute(
        select(Department.code)
        .join(user_departments, user_departments.c.department_id == Department.id)
        .where(user_departments.c.user_id == admin.id)
    )).scalars().all()
    granted.update(extra)
    if dept.code not in granted:
        raise ForbiddenError("Department not in your scope")


# ---------------------------------------------------------------------------
# Departments
# ---------------------------------------------------------------------------

@router.get("/departments", response_model=List[DepartmentOut])
async def list_departments(
    include_inactive: bool = Query(True),
    admin: User = _AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Departments the caller is allowed to see.

    SUPERADMIN: every department (active + inactive when include_inactive).
    ADMIN: only the home department.
    CROSSADMIN: home + every granted extra department.
    """
    if admin.role == UserRole.SUPERADMIN:
        q = select(Department).order_by(Department.name)
        if not include_inactive:
            q = q.where(Department.is_active.is_(True))
        res = await db.execute(q)
        return list(res.scalars())

    accessible = await get_accessible_department_codes(db, admin)
    if not accessible:
        return []
    q = select(Department).where(Department.code.in_(accessible)).order_by(Department.name)
    if not include_inactive:
        q = q.where(Department.is_active.is_(True))
    res = await db.execute(q)
    return list(res.scalars())


@router.post("/departments", response_model=DepartmentOut, status_code=201)
async def create_department(
    payload: DepartmentCreate,
    admin: User = _DeptManager,
    db: AsyncSession = Depends(get_db),
):
    """Create a department. SUPERADMIN and CROSSADMIN may both do this.

    When a CROSSADMIN creates one, it's auto-granted to them (added to
    their `extra_departments`) so they can immediately manage it — without
    this they'd create a dept they can't see.
    """
    exists = (await db.execute(
        select(Department).where(Department.code == payload.code)
    )).scalar_one_or_none()
    if exists:
        raise GuardrailBlocked("Department code already exists")
    dept = Department(
        code=payload.code.lower(), name=payload.name,
        description=payload.description, support_email=payload.support_email,
    )
    db.add(dept)
    await db.flush()  # need dept.id for the grant row

    # Auto-grant to a CROSSADMIN creator so it lands in their scope.
    if admin.role == UserRole.CROSSADMIN:
        await db.execute(
            user_departments.insert(),
            [{"user_id": admin.id, "department_id": dept.id}],
        )

    db.add(AuditLog(user_id=admin.id, user_email=admin.email,
                    action="DEPT_CREATE", resource_type="department",
                    resource_id=payload.code, details=payload.model_dump()))
    await db.commit()
    await db.refresh(dept)
    log_event("admin", "info", "dept created", code=dept.code, by=admin.email)
    return dept


@router.patch("/departments/{dept_id}", response_model=DepartmentOut)
async def update_department(
    dept_id: int, payload: DepartmentUpdate,
    admin: User = _DeptManager,
    db: AsyncSession = Depends(get_db),
):
    d = (await db.execute(select(Department).where(Department.id == dept_id))).scalar_one_or_none()
    if not d:
        raise NotFoundError()
    await _assert_dept_in_scope(db, admin, d)
    data = payload.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(d, k, v)
    db.add(AuditLog(user_id=admin.id, user_email=admin.email,
                    action="DEPT_UPDATE", resource_type="department",
                    resource_id=str(dept_id), details=data))
    await db.commit()
    await db.refresh(d)
    return d


@router.post("/departments/{dept_id}/activate", response_model=DepartmentOut)
async def activate_department(
    dept_id: int,
    admin: User = _DeptManager,
    db: AsyncSession = Depends(get_db),
):
    """Re-enable a previously deactivated department."""
    d = (await db.execute(select(Department).where(Department.id == dept_id))).scalar_one_or_none()
    if not d:
        raise NotFoundError()
    await _assert_dept_in_scope(db, admin, d)
    d.is_active = True
    db.add(AuditLog(user_id=admin.id, user_email=admin.email,
                    action="DEPT_ACTIVATE", resource_type="department",
                    resource_id=str(dept_id)))
    await db.commit()
    await db.refresh(d)
    log_event("admin", "info", "dept activated", code=d.code, by=admin.email)
    return d


@router.post("/departments/{dept_id}/deactivate", response_model=DepartmentOut)
async def deactivate_department(
    dept_id: int,
    admin: User = _DeptManager,
    db: AsyncSession = Depends(get_db),
):
    """Soft-disable a department. Users will not be able to select it
    until it is re-activated. Data is preserved."""
    d = (await db.execute(select(Department).where(Department.id == dept_id))).scalar_one_or_none()
    if not d:
        raise NotFoundError()
    await _assert_dept_in_scope(db, admin, d)
    d.is_active = False
    db.add(AuditLog(user_id=admin.id, user_email=admin.email,
                    action="DEPT_DEACTIVATE", resource_type="department",
                    resource_id=str(dept_id)))
    await db.commit()
    await db.refresh(d)
    log_event("admin", "info", "dept deactivated", code=d.code, by=admin.email)
    return d


@router.delete("/departments/{dept_id}", status_code=204)
async def delete_department(
    dept_id: int,
    admin: User = _DeptManager,
    db: AsyncSession = Depends(get_db),
):
    """**Permanent** department delete. SUPERADMIN may delete any;
    CROSSADMIN may delete a department in their granted scope.

    Refuses if any active KB documents reference this department. The
    caller (UI) must show a confirmation dialog before invoking this.
    """
    d = (await db.execute(select(Department).where(Department.id == dept_id))).scalar_one_or_none()
    if not d:
        raise NotFoundError()
    await _assert_dept_in_scope(db, admin, d)

    # Refuse if there are active KB docs.
    kb_count = (await db.execute(
        select(func.count(KBDocument.id)).where(
            KBDocument.department_id == dept_id,
            KBDocument.status != KBDocumentStatus.DELETED,
        )
    )).scalar() or 0
    if kb_count:
        raise GuardrailBlocked(
            f"Department has {kb_count} active KB document(s). Delete them first.")

    # Detach explicit CrossAdmin grants tied to this dept.
    await db.execute(
        user_departments.delete().where(user_departments.c.department_id == dept_id)
    )
    db.add(AuditLog(user_id=admin.id, user_email=admin.email,
                    action="DEPT_DELETE", resource_type="department",
                    resource_id=str(dept_id),
                    details={"code": d.code, "name": d.name}))
    await db.delete(d)
    await db.commit()
    log_event("admin", "warning", "dept hard-deleted",
              code=d.code, by=admin.email)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@router.get("/users", response_model=List[UserOut])
async def list_users(
    admin: User = _AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Users the caller can see.

    SUPERADMIN: every user.
    ADMIN: users whose home dept matches theirs.
    CROSSADMIN: users whose home dept is in their granted set OR whose
                extra dept grants overlap with theirs.
    """
    base = select(User).options(selectinload(User.extra_departments))

    if admin.role == UserRole.SUPERADMIN:
        stmt = base.order_by(User.created_at.desc()).limit(500)
    else:
        accessible_ids: list[int] = []
        accessible_codes = await get_accessible_department_codes(db, admin)
        if accessible_codes:
            dept_rows = (await db.execute(
                select(Department.id).where(Department.code.in_(accessible_codes))
            )).scalars().all()
            accessible_ids = list(dept_rows)
        if not accessible_ids:
            return []
        # Users whose home dept overlaps with our scope. We deliberately
        # don't widen via extra_departments here — that would let an ADMIN
        # see CrossAdmins from other depts, which contradicts the spec.
        stmt = (
            base.where(User.department_id.in_(accessible_ids))
            .order_by(User.created_at.desc())
            .limit(500)
        )

    res = await db.execute(stmt)
    out: List[UserOut] = []
    for u in res.scalars():
        used = await get_user_monthly_tokens(db, u.id)
        out.append(UserOut(
            id=u.id, email=u.email, full_name=u.full_name, role=u.role.value,
            department_code=u.department.code if u.department else None,
            department_codes=[d.code for d in (u.extra_departments or [])],
            is_active=u.is_active, last_login_at=u.last_login_at, created_at=u.created_at,
            monthly_token_limit=u.monthly_token_limit,
            monthly_tokens_used=used,
            preferred_model=u.preferred_model,
        ))
    return out


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    payload: UserCreate, admin: User = _AdminUser, db: AsyncSession = Depends(get_db),
):
    if (await db.execute(
        select(User).where(User.email == payload.email.lower())
    )).scalar_one_or_none():
        raise GuardrailBlocked("Email already exists")

    try:
        role = UserRole(payload.role)
    except ValueError:
        raise GuardrailBlocked(f"Unknown role '{payload.role}'")

    # ----- Scope guard --------------------------------------------------
    # Non-SUPERADMIN creators cannot:
    #   * mint a user with a role at or above their own rank
    #   * grant access to a department they themselves don't have
    if admin.role != UserRole.SUPERADMIN:
        if role_rank(role) >= role_rank(admin.role):
            raise ForbiddenError(
                f"You cannot create a user with role '{role.value}'"
            )
        scope = set(await get_accessible_department_codes(db, admin))
        requested = set()
        if payload.department_code:
            requested.add(payload.department_code.lower().strip())
        for c in (payload.department_codes or []):
            if c and c.strip():
                requested.add(c.strip().lower())
        outside = requested - scope
        if outside:
            raise ForbiddenError(
                f"Department(s) not in your scope: {', '.join(sorted(outside))}"
            )

    # Normalise the department input. For CROSSADMIN we accept the list
    # `department_codes`; the first entry is also used as the home dept
    # so audit / ticket routing have a sensible default. Single-dept
    # roles only honour `department_code`.
    if role == UserRole.CROSSADMIN:
        codes = [c.strip().lower() for c in (payload.department_codes or []) if c and c.strip()]
        if payload.department_code and payload.department_code.lower() not in codes:
            codes.insert(0, payload.department_code.lower())
        if not codes:
            raise GuardrailBlocked("CROSSADMIN requires at least one department")
        rows = (await db.execute(
            select(Department).where(Department.code.in_(codes))
        )).scalars().all()
        by_code = {d.code: d for d in rows}
        missing = [c for c in codes if c not in by_code]
        if missing:
            raise GuardrailBlocked(f"Unknown department(s): {', '.join(missing)}")
        home = by_code[codes[0]]
        u = User(
            email=payload.email.lower(), full_name=payload.full_name,
            role=role, department_id=home.id,
            monthly_token_limit=payload.monthly_token_limit,
            preferred_model=payload.preferred_model,
        )
        db.add(u)
        await db.flush()
        # Insert directly into the association table — assigning to
        # `u.extra_departments` would force SQLAlchemy to lazy-load the
        # (empty) existing collection to compute the delta, which fails
        # synchronously inside an async session ("greenlet_spawn has
        # not been called").
        await db.execute(
            user_departments.insert(),
            [{"user_id": u.id, "department_id": by_code[c].id} for c in codes],
        )
        granted_codes = codes
    else:
        dept_id = None
        if payload.department_code:
            d = (await db.execute(
                select(Department).where(Department.code == payload.department_code.lower())
            )).scalar_one_or_none()
            dept_id = d.id if d else None
        u = User(
            email=payload.email.lower(), full_name=payload.full_name,
            role=role, department_id=dept_id,
            monthly_token_limit=payload.monthly_token_limit,
            preferred_model=payload.preferred_model,
        )
        db.add(u)
        await db.flush()
        granted_codes = [payload.department_code.lower()] if payload.department_code else []

    db.add(AuditLog(user_id=admin.id, user_email=admin.email,
                    action="USER_CREATE", resource_type="user",
                    resource_id=payload.email,
                    details={"role": role.value, "depts": granted_codes}))
    await db.commit()

    # Re-query so `department` (lazy="joined") + `extra_departments`
    # (selectinload) are populated. Accessing `u.department.code`
    # directly on the freshly-constructed instance would trigger a sync
    # lazy load and raise `greenlet_spawn has not been called` in async
    # contexts.
    fresh = (await db.execute(
        select(User)
        .options(selectinload(User.extra_departments))
        .where(User.id == u.id)
    )).scalar_one()
    return UserOut(
        id=fresh.id, email=fresh.email, full_name=fresh.full_name,
        role=fresh.role.value,
        department_code=fresh.department.code if fresh.department else None,
        department_codes=[d.code for d in (fresh.extra_departments or [])],
        is_active=fresh.is_active, last_login_at=fresh.last_login_at,
        created_at=fresh.created_at,
        monthly_token_limit=fresh.monthly_token_limit,
        monthly_tokens_used=0,
        preferred_model=fresh.preferred_model,
    )


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int, payload: UserUpdate,
    admin: User = _AdminUser, db: AsyncSession = Depends(get_db),
):
    u = (await db.execute(
        select(User)
        .options(selectinload(User.extra_departments))
        .where(User.id == user_id)
    )).scalar_one_or_none()
    if not u:
        raise NotFoundError()

    _fields = payload.model_dump(exclude_unset=True)

    # An admin/cross-admin may set OTHER users' token budgets but never
    # their own — only a SUPERADMIN can change a SUPERADMIN-or-self budget.
    if (admin.id == u.id and admin.role != UserRole.SUPERADMIN
            and "monthly_token_limit" in _fields):
        raise ForbiddenError("You cannot change your own token limit")

    # ----- Scope guard --------------------------------------------------
    # Non-SUPERADMIN editors can only modify users whose current home
    # dept is in their scope, and cannot promote a user to a role at or
    # above their own rank or assign depts they don't own.
    if admin.role != UserRole.SUPERADMIN:
        admin_scope = set(await get_accessible_department_codes(db, admin))
        existing_dept_code = u.department.code if u.department else None
        if existing_dept_code and existing_dept_code not in admin_scope:
            raise ForbiddenError("That user belongs to a department outside your scope")

        requested = set()
        if payload.department_code:
            requested.add(payload.department_code.lower().strip())
        for c in (payload.department_codes or []):
            if c and c.strip():
                requested.add(c.strip().lower())
        outside = requested - admin_scope
        if outside:
            raise ForbiddenError(
                f"Department(s) not in your scope: {', '.join(sorted(outside))}"
            )
        if payload.role:
            try:
                new_role = UserRole(payload.role)
            except ValueError:
                raise GuardrailBlocked(f"Unknown role '{payload.role}'")
            if role_rank(new_role) >= role_rank(admin.role):
                raise ForbiddenError(
                    f"You cannot assign role '{new_role.value}'"
                )

    # Use exclude_unset so the caller can EXPLICITLY clear nullable
    # fields by sending null (e.g. monthly_token_limit: null to revert
    # a user back to the system default). exclude_none would drop the
    # null and silently leave the override in place.
    data = payload.model_dump(exclude_unset=True)

    if "role" in data:
        new_role_val = data.pop("role")
        if new_role_val is not None:
            try:
                u.role = UserRole(new_role_val)
            except ValueError:
                raise GuardrailBlocked(f"Unknown role '{new_role_val}'")

    if "department_code" in data:
        code = data.pop("department_code")
        if code:
            d = (await db.execute(
                select(Department).where(Department.code == code.lower())
            )).scalar_one_or_none()
            u.department_id = d.id if d else None
        else:
            u.department_id = None

    if "department_codes" in data:
        codes = data.pop("department_codes") or []
        # Always wipe existing grants via the association table so we
        # don't depend on the relationship being fully loaded.
        await db.execute(
            user_departments.delete().where(user_departments.c.user_id == u.id)
        )
        if u.role == UserRole.CROSSADMIN and codes:
            rows = (await db.execute(
                select(Department).where(Department.code.in_([c.lower() for c in codes]))
            )).scalars().all()
            if rows:
                await db.execute(
                    user_departments.insert(),
                    [{"user_id": u.id, "department_id": d.id} for d in rows],
                )

    for k, v in data.items():
        setattr(u, k, v)

    db.add(AuditLog(user_id=admin.id, user_email=admin.email,
                    action="USER_UPDATE", resource_type="user",
                    resource_id=str(user_id), details=payload.model_dump(exclude_none=True)))
    await db.commit()

    # Re-query for the response so we don't trigger a sync lazy-load on
    # `u.department` (same trap as create_user).
    fresh = (await db.execute(
        select(User)
        .options(selectinload(User.extra_departments))
        .where(User.id == u.id)
    )).scalar_one()
    used = await get_user_monthly_tokens(db, fresh.id)
    return UserOut(
        id=fresh.id, email=fresh.email, full_name=fresh.full_name,
        role=fresh.role.value,
        department_code=fresh.department.code if fresh.department else None,
        department_codes=[d.code for d in (fresh.extra_departments or [])],
        is_active=fresh.is_active, last_login_at=fresh.last_login_at,
        created_at=fresh.created_at,
        monthly_token_limit=fresh.monthly_token_limit,
        monthly_tokens_used=used,
        preferred_model=fresh.preferred_model,
    )


async def _scope_check_user(db: AsyncSession, admin: User, target: User) -> None:
    """Shared RBAC for activate/deactivate: non-SUPERADMIN can only touch
    users whose home dept is in their scope, and never themselves."""
    if admin.id == target.id:
        raise GuardrailBlocked("You cannot change your own access")
    if admin.role == UserRole.SUPERADMIN:
        return
    scope = set(await get_accessible_department_codes(db, admin))
    target_dept = target.department.code if target.department else None
    if target_dept and target_dept not in scope:
        raise ForbiddenError("That user belongs to a department outside your scope")
    if role_rank(target.role) >= role_rank(admin.role):
        raise ForbiddenError("You cannot manage a user with equal or higher role")


@router.post("/users/{user_id}/deactivate", response_model=UserOut)
async def deactivate_user(
    user_id: int, admin: User = _AdminUser, db: AsyncSession = Depends(get_db),
):
    """Soft-disable a user. Login is rejected immediately
    (see `app.api.deps.get_current_user`) but data is preserved.
    Reversible via /activate."""
    u = (await db.execute(
        select(User)
        .options(selectinload(User.extra_departments))
        .where(User.id == user_id)
    )).scalar_one_or_none()
    if not u:
        raise NotFoundError()
    await _scope_check_user(db, admin, u)
    u.is_active = False
    db.add(AuditLog(user_id=admin.id, user_email=admin.email,
                    action="USER_DEACTIVATE", resource_type="user",
                    resource_id=str(user_id),
                    details={"email": u.email}))
    await db.commit()
    await db.refresh(u)
    log_event("admin", "info", "user deactivated", email=u.email, by=admin.email)
    used = await get_user_monthly_tokens(db, u.id)
    return UserOut(
        id=u.id, email=u.email, full_name=u.full_name, role=u.role.value,
        department_code=u.department.code if u.department else None,
        department_codes=[d.code for d in (u.extra_departments or [])],
        is_active=u.is_active, last_login_at=u.last_login_at, created_at=u.created_at,
        monthly_token_limit=u.monthly_token_limit, monthly_tokens_used=used,
        preferred_model=u.preferred_model,
    )


@router.post("/users/{user_id}/activate", response_model=UserOut)
async def activate_user(
    user_id: int, admin: User = _AdminUser, db: AsyncSession = Depends(get_db),
):
    u = (await db.execute(
        select(User)
        .options(selectinload(User.extra_departments))
        .where(User.id == user_id)
    )).scalar_one_or_none()
    if not u:
        raise NotFoundError()
    await _scope_check_user(db, admin, u)
    u.is_active = True
    db.add(AuditLog(user_id=admin.id, user_email=admin.email,
                    action="USER_ACTIVATE", resource_type="user",
                    resource_id=str(user_id),
                    details={"email": u.email}))
    await db.commit()
    await db.refresh(u)
    log_event("admin", "info", "user re-activated", email=u.email, by=admin.email)
    used = await get_user_monthly_tokens(db, u.id)
    return UserOut(
        id=u.id, email=u.email, full_name=u.full_name, role=u.role.value,
        department_code=u.department.code if u.department else None,
        department_codes=[d.code for d in (u.extra_departments or [])],
        is_active=u.is_active, last_login_at=u.last_login_at, created_at=u.created_at,
        monthly_token_limit=u.monthly_token_limit, monthly_tokens_used=used,
        preferred_model=u.preferred_model,
    )


# ---------------------------------------------------------------------------
# Knowledge Base — uploads
# ---------------------------------------------------------------------------

MAX_UPLOAD_MB = 50


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _check_dept_access(db: AsyncSession, admin: User, dept_code: str) -> Department:
    """Resolve dept by code and assert the admin can manage it.

    - SUPERADMIN: any active dept.
    - ADMIN: only their home dept.
    - CROSSADMIN: must be in their granted set.
    """
    code = dept_code.lower().strip()
    d = (await db.execute(
        select(Department).where(Department.code == code)
    )).scalar_one_or_none()
    if not d or not d.is_active:
        raise NotFoundError("Department not found")

    if admin.role == UserRole.SUPERADMIN:
        return d

    granted = set(await get_accessible_department_codes(db, admin))
    if code not in granted:
        raise ForbiddenError(
            f"Department '{code}' is not in your scope"
        )
    return d


def _parse_metadata_form(raw: Optional[str]) -> dict:
    """Parse + normalise the `metadata` form field (a JSON object string).

    Tolerant: blank/invalid JSON yields no metadata rather than a 400, so
    a stray value never blocks an upload. Normalisation (lowercased keys,
    trimmed values) keeps upload-side tags byte-identical to chat-side
    filters.
    """
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return normalize_metadata(parsed)


async def _persist_upload(
    db: AsyncSession,
    *,
    admin: User,
    dept: Department,
    file: UploadFile,
    data: bytes,
    metadata: Optional[dict] = None,
) -> KBDocumentOut:
    """Hash → dedup → S3 → KBDocument row → audit. Idempotent on duplicate."""
    filename = file.filename or "untitled"
    if not is_supported(filename):
        raise GuardrailBlocked(f"Unsupported file type: {filename}")
    if not data:
        raise GuardrailBlocked(f"Empty file: {filename}")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise GuardrailBlocked(
            f"File '{filename}' too large (>{MAX_UPLOAD_MB} MB)"
        )

    meta = normalize_metadata(metadata)
    content_hash = _hash_bytes(data)

    # Dedup: same dept + same hash + active → return existing row. If the
    # file is already tracked but the admin supplied new metadata, refresh
    # the tags (DB + sidecar) so re-uploading is the way to re-tag.
    existing = (await db.execute(
        select(KBDocument).where(
            KBDocument.department_id == dept.id,
            KBDocument.content_hash == content_hash,
            KBDocument.status != KBDocumentStatus.DELETED,
        )
    )).scalar_one_or_none()
    if existing:
        if meta and (existing.doc_metadata or {}) != meta:
            existing.doc_metadata = meta
            await s3_service.upload_kb_document(
                department_code=dept.code, filename=filename, data=data,
                uploader_email=admin.email, metadata=meta,
            )
            db.add(AuditLog(
                user_id=admin.id, user_email=admin.email,
                action="KB_RETAG", resource_type="kb_document",
                resource_id=existing.s3_key,
                details={"dept": dept.code, "metadata": meta, "filename": filename},
            ))
            await db.commit()
            await db.refresh(existing)
        else:
            log_event("admin", "info", "duplicate upload skipped",
                      dept=dept.code, filename=filename, by=admin.email)
        return _doc_to_out(existing, dept.code)

    upload = await s3_service.upload_kb_document(
        department_code=dept.code, filename=filename, data=data,
        uploader_email=admin.email, metadata=meta,
    )

    doc = KBDocument(
        department_id=dept.id, filename=filename,
        s3_key=upload["s3_key"], content_type=file.content_type,
        size_bytes=len(data), content_hash=content_hash,
        doc_metadata=meta,
        uploader_id=admin.id, uploader_email=admin.email,
        # File is in S3 but not yet ingested. The admin must click Sync.
        status=KBDocumentStatus.ACTIVE,
    )
    db.add(doc)
    db.add(AuditLog(
        user_id=admin.id, user_email=admin.email,
        action="KB_UPLOAD", resource_type="kb_document",
        resource_id=upload["s3_key"],
        details={"dept": dept.code, "metadata": meta,
                 "bytes": len(data), "filename": filename},
    ))
    await db.commit()
    await db.refresh(doc)
    return _doc_to_out(doc, dept.code)


@router.post("/upload", response_model=UploadResult)
async def upload_document(
    department_code: str = Form(...),
    file: UploadFile = File(...),
    metadata: Optional[str] = Form(None),
    admin: User = _AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Single-file upload.

    The file lands in S3 under <dept>/<file> immediately. **KB ingestion
    is NOT triggered automatically** — the admin must click the Sync
    button afterwards. The newly-created row is marked ACTIVE; its
    `last_ingested_at` stays NULL until a sync runs. `metadata` is an
    optional JSON object of arbitrary {key: value} tags written into the
    Bedrock sidecar and usable as retrieval filters.
    """
    dept = await _check_dept_access(db, admin, department_code)
    meta = _parse_metadata_form(metadata)
    data = await file.read()
    doc = await _persist_upload(db, admin=admin, dept=dept, file=file,
                                data=data, metadata=meta)

    return UploadResult(
        s3_key=doc.s3_key,
        s3_uri=f"s3://{settings.S3_BUCKET_NAME}/{doc.s3_key}",
        department_code=dept.code, metadata=doc.metadata, bytes=doc.size_bytes,
        document_id=doc.id,
    )


@router.post("/upload/bulk", response_model=BulkUploadResult)
async def upload_documents_bulk(
    department_code: str = Form(...),
    files: List[UploadFile] = File(...),
    metadata: Optional[str] = Form(None),
    admin: User = _AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Multi-file upload. Each file is hashed and de-duplicated against
    the department; results are returned per-file so the UI can render
    success/skipped/failed indicators. The optional `metadata` JSON is
    applied to every file in the batch.

    **KB ingestion is NOT triggered automatically** — the admin must
    click the Sync button afterwards. This is intentional so a batch
    of N files only causes one ingestion job, when the admin is ready.
    """
    if not can_manage_kb(admin):
        raise ForbiddenError("KB management not permitted")
    dept = await _check_dept_access(db, admin, department_code)
    meta = _parse_metadata_form(metadata)

    succeeded: List[KBDocumentOut] = []
    skipped: List[dict] = []
    failed: List[dict] = []

    for f in files:
        try:
            data = await f.read()
        except Exception as e:
            failed.append({"filename": f.filename, "error": f"read failed: {e}"})
            continue
        try:
            before = (await db.execute(
                select(KBDocument).where(
                    KBDocument.department_id == dept.id,
                    KBDocument.content_hash == _hash_bytes(data),
                    KBDocument.status != KBDocumentStatus.DELETED,
                )
            )).scalar_one_or_none()
            doc = await _persist_upload(db, admin=admin, dept=dept, file=f,
                                        data=data, metadata=meta)
            if before and before.id == doc.id:
                skipped.append({"filename": f.filename, "reason": "duplicate", "document_id": doc.id})
            else:
                succeeded.append(doc)
        except GuardrailBlocked as e:
            failed.append({"filename": f.filename, "error": e.detail})
        except Exception as e:
            log_event("errors", "error", "bulk upload item failed",
                      file=f.filename, error=str(e))
            failed.append({"filename": f.filename, "error": str(e)})

    return BulkUploadResult(
        department_code=dept.code,
        succeeded=succeeded,
        skipped=skipped,
        failed=failed,
    )


@router.post("/kb/resync", status_code=202)
async def trigger_kb_resync(
    background: BackgroundTasks,
    admin: User = _AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Run a Bedrock KB ingestion sync **on demand**.

    Updates `last_ingested_at` for every non-deleted document so the UI
    can show "synced X minutes ago". This is the ONLY code path that
    triggers ingestion — uploads and deletes do not.
    """
    job_id = await bedrock_service.start_kb_ingestion()

    if job_id:
        now = datetime.now(timezone.utc)
        rows = (await db.execute(
            select(KBDocument).where(
                KBDocument.status != KBDocumentStatus.DELETED,
            )
        )).scalars().all()
        for d in rows:
            d.ingestion_job_id = job_id
            d.last_ingested_at = now
            if d.status == KBDocumentStatus.INGESTION_FAILED:
                d.status = KBDocumentStatus.ACTIVE
        await db.commit()

    db.add(AuditLog(
        user_id=admin.id, user_email=admin.email,
        action="KB_SYNC", resource_type="kb",
        details={"job_id": job_id},
    ))
    await db.commit()
    log_event("admin", "info", "manual KB resync requested",
              by=admin.email, job_id=job_id)
    return {"status": "queued", "ingestion_job_id": job_id}


# ---------------------------------------------------------------------------
# Knowledge Base — list / search / delete
# ---------------------------------------------------------------------------

def _doc_to_out(d: KBDocument, dept_code: str | None = None) -> KBDocumentOut:
    return KBDocumentOut(
        id=d.id,
        filename=d.filename,
        s3_key=d.s3_key,
        content_type=d.content_type,
        size_bytes=d.size_bytes,
        department_id=d.department_id,
        department_code=dept_code or (d.department.code if d.department else ""),
        metadata=d.doc_metadata or {},
        uploader_email=d.uploader_email,
        status=d.status.value if hasattr(d.status, "value") else str(d.status),
        ingestion_job_id=d.ingestion_job_id,
        last_ingested_at=d.last_ingested_at,
        created_at=d.created_at,
        updated_at=d.updated_at,
        # DB-tracked files are never external. External files have id=null.
        external=False,
    )


def _scope_codes_for_admin(admin: User, accessible: list[str]) -> Optional[list[str]]:
    """Return the dept codes a given admin's KB list should be limited to.

    `None` means "no dept restriction" (SUPERADMIN only). An empty list
    means "no access" (callers should short-circuit to empty results).
    Every other role is pinned to whatever `accessible` resolved to.
    """
    if admin.role == UserRole.SUPERADMIN:
        return None
    return list(accessible)


@router.get("/kb/documents", response_model=KBDocumentListResponse)
async def list_kb_documents(
    department_code: Optional[str] = Query(None),
    q: Optional[str] = Query(None, description="case-insensitive filename match"),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    include_s3: bool = Query(True, description="merge S3 objects not yet tracked in DB"),
    admin: User = _AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """List KB documents, scoped to the admin's authority.

    Combines two sources:
      * `kb_documents` rows — anything uploaded through this app.
      * Raw S3 objects under <dept>/ prefixes — files put there directly
        (e.g. by `aws s3 cp`) that the app hasn't seen yet. These come
        back with `external=true` and `id=null` so the UI can show a
        distinct badge. They can't be deleted via this UI until they're
        adopted — that's a guardrail against accidental destruction of
        objects we don't own the audit trail for.

    Pagination + search + filter run on the merged set so the page
    numbers stay accurate.
    """
    accessible = await get_accessible_department_codes(db, admin)
    scope = _scope_codes_for_admin(admin, accessible)
    if scope is not None and not scope:
        return KBDocumentListResponse(items=[], total=0, page=page, page_size=page_size)

    # Validate the caller-supplied dept filter against the admin's scope.
    if department_code:
        code = department_code.lower().strip()
        if scope is not None and code not in set(scope):
            raise ForbiddenError("Not authorised for this department")
        active_filter_codes: Optional[list[str]] = [code]
    else:
        active_filter_codes = scope  # None or list

    # --- 1. DB rows -------------------------------------------------------
    stmt = (
        select(KBDocument, Department.code)
        .join(Department, Department.id == KBDocument.department_id)
    )
    if active_filter_codes is not None:
        stmt = stmt.where(Department.code.in_(active_filter_codes))

    if status:
        try:
            stmt = stmt.where(KBDocument.status == KBDocumentStatus(status.upper()))
        except ValueError:
            raise GuardrailBlocked(f"Unknown status '{status}'")
    else:
        stmt = stmt.where(KBDocument.status != KBDocumentStatus.DELETED)

    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(func.lower(KBDocument.filename).like(like))

    db_rows = (await db.execute(stmt)).all()
    db_items = [_doc_to_out(d, code) for (d, code) in db_rows]
    tracked_keys = {it.s3_key for it in db_items}

    # --- 2. Merge S3 objects ---------------------------------------------
    merged: list[KBDocumentOut] = list(db_items)

    if include_s3 and not status:
        # Resolve dept codes to dept ids for the synthetic rows.
        dept_rows = (await db.execute(select(Department))).scalars().all()
        dept_by_code = {d.code: d for d in dept_rows}

        s3_codes = active_filter_codes if active_filter_codes is not None else None
        s3_objects = await s3_service.list_kb_objects(department_codes=s3_codes)

        for obj in s3_objects:
            if obj["s3_key"] in tracked_keys:
                continue
            # Respect the search filter on synthetic rows too.
            if q and q.strip().lower() not in obj["filename"].lower():
                continue
            dept = dept_by_code.get(obj["department_code"])
            created = obj.get("last_modified") or datetime.now(timezone.utc)
            merged.append(KBDocumentOut(
                id=None,
                filename=obj["filename"],
                s3_key=obj["s3_key"],
                content_type=None,
                size_bytes=obj["size_bytes"],
                department_id=dept.id if dept else None,
                department_code=obj["department_code"],
                metadata={},
                uploader_email=None,
                status="EXTERNAL",
                ingestion_job_id=None,
                last_ingested_at=None,
                created_at=created,
                updated_at=None,
                external=True,
            ))

    # --- 3. Sort + paginate ----------------------------------------------
    # SQLite returns timezone-naive datetimes for our DB rows; boto3
    # returns timezone-aware (UTC) for S3 objects. Mixing them in a sort
    # raises `TypeError: can't compare offset-naive and offset-aware`.
    # Normalise everything to UTC-aware before sorting.
    def _aware(dt):
        if dt is None:
            return datetime.fromtimestamp(0, tz=timezone.utc)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    merged.sort(key=lambda x: _aware(x.created_at), reverse=True)
    total = len(merged)
    start = (page - 1) * page_size
    end = start + page_size
    items = merged[start:end]
    return KBDocumentListResponse(items=items, total=total, page=page, page_size=page_size)


@router.delete("/kb/objects", status_code=200)
async def delete_kb_object(
    s3_key: str = Query(..., description="S3 key under <bucket>/<dept>/..."),
    admin: User = _AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Delete an *externally-uploaded* S3 object.

    Used by the KB UI for entries that exist in S3 but have no
    `kb_documents` row (i.e. files uploaded via `aws s3 cp`).

    RBAC: the dept prefix in the key must be in the admin's scope.
    """
    if not can_manage_kb(admin):
        raise ForbiddenError("KB management not permitted")
    key = (s3_key or "").strip()
    if not key or "/" not in key:
        raise GuardrailBlocked("Invalid s3_key")
    dept_code = key.split("/", 1)[0].lower()

    if admin.role != UserRole.SUPERADMIN:
        accessible = set(await get_accessible_department_codes(db, admin))
        if dept_code not in accessible:
            raise ForbiddenError("Department not in your scope")

    # If a DB row already covers this S3 key, route through the normal
    # soft-delete path so the audit trail / tombstone are consistent.
    doc = (await db.execute(
        select(KBDocument).where(KBDocument.s3_key == key)
    )).scalar_one_or_none()
    if doc and doc.status != KBDocumentStatus.DELETED:
        s3_ok = await s3_service.delete_kb_document(key)
        doc.status = KBDocumentStatus.DELETED
        doc.deleted_at = datetime.now(timezone.utc)
        db.add(AuditLog(
            user_id=admin.id, user_email=admin.email,
            action="KB_DELETE", resource_type="kb_document",
            resource_id=key,
            details={"dept": dept_code, "filename": doc.filename,
                     "s3_removed": s3_ok, "external_path": True},
        ))
        await db.commit()
        return {"status": "deleted", "s3_key": key, "s3_removed": s3_ok}

    # External-only: drop the S3 object and write an audit row.
    s3_ok = await s3_service.delete_kb_document(key)
    db.add(AuditLog(
        user_id=admin.id, user_email=admin.email,
        action="KB_DELETE_EXTERNAL", resource_type="s3",
        resource_id=key,
        details={"dept": dept_code, "s3_removed": s3_ok},
    ))
    await db.commit()
    log_event("admin", "info", "external kb file deleted",
              key=key, by=admin.email, s3_removed=s3_ok)
    return {"status": "deleted", "s3_key": key, "s3_removed": s3_ok}


@router.post("/kb/adopt-external", response_model=KBDocumentOut)
async def adopt_external_kb_file(
    s3_key: str = Query(..., description="Full S3 key (dept/filename)"),
    payload: dict = Body(default_factory=dict),
    admin: User = _AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Create a database record for an externally-uploaded S3 file so it can
    be managed (edited, deleted) from the admin UI.

    Body: {"metadata": {...}} where metadata is optional.
    This is a one-time operation per external file. Once adopted, it has an ID
    and can be edited via the normal PATCH /kb/documents/{id}/metadata endpoint.
    """
    if not can_manage_kb(admin):
        raise ForbiddenError("KB management not permitted")

    if not s3_key or "/" not in s3_key:
        raise GuardrailBlocked("Invalid S3 key format (should be dept/filename)")

    dept_code = s3_key.split("/", 1)[0]
    filename = s3_key.split("/", 1)[1] if len(s3_key.split("/", 1)) > 1 else "file"
    metadata = payload.get("metadata") if isinstance(payload, dict) else None

    # Find or create the department.
    dept = (await db.execute(
        select(Department).where(
            Department.code == dept_code.lower()
        )
    )).scalar_one_or_none()
    if not dept:
        raise NotFoundError(f"Department {dept_code} not found")

    # Check if already tracked in DB.
    existing = (await db.execute(
        select(KBDocument).where(
            KBDocument.s3_key == s3_key,
            KBDocument.status != KBDocumentStatus.DELETED,
        )
    )).scalar_one_or_none()
    if existing:
        # Already tracked — just update metadata if provided.
        if metadata:
            meta = normalize_metadata(metadata)
            existing.doc_metadata = meta
            db.add(existing)
            await db.commit()
        await db.refresh(existing)
        return _doc_to_out(existing, dept_code)

    # Create a new tracked record for this external file.
    # We don't have the original hash/size, so use placeholders.
    doc = KBDocument(
        department_id=dept.id,
        filename=filename,
        s3_key=s3_key,
        content_type="application/octet-stream",
        size_bytes=0,  # unknown
        content_hash="",  # no hash (external)
        doc_metadata=normalize_metadata(metadata) if metadata else {},
        uploader_id=admin.id,
        uploader_email=admin.email,
        status=KBDocumentStatus.ACTIVE,
    )
    db.add(doc)
    db.add(AuditLog(
        user_id=admin.id, user_email=admin.email,
        action="KB_ADOPT", resource_type="kb_document",
        resource_id=s3_key,
        details={"dept": dept_code, "filename": filename},
    ))
    await db.commit()
    await db.refresh(doc)
    log_event("admin", "info", "external kb file adopted",
              s3_key=s3_key, by=admin.email)
    return _doc_to_out(doc, dept_code)


@router.patch("/kb/documents/{doc_id}/metadata", response_model=KBDocumentOut)
async def update_kb_document_metadata(
    doc_id: int,
    payload: dict,
    admin: User = _AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Replace a KB document's metadata in-place.

    Body shape:
        {"metadata": {"region": "india", "year": "2024", ...}}

    Both the DB row's ``doc_metadata`` column AND the Bedrock KB sidecar
    (``<s3_key>.metadata.json``) are rewritten so the new tags become
    retrieval-time filters after the next KB sync. The file body is NOT
    re-uploaded.

    Same RBAC as delete: SUPERADMIN any dept; other admins only the
    departments they're scoped to.
    """
    if not can_manage_kb(admin):
        raise ForbiddenError("KB management not permitted")

    doc = (await db.execute(
        select(KBDocument)
        .options(selectinload(KBDocument.department))
        .where(KBDocument.id == doc_id)
    )).scalar_one_or_none()
    if not doc:
        raise NotFoundError("Document not found")
    if doc.status == KBDocumentStatus.DELETED:
        raise GuardrailBlocked("Cannot edit a deleted document")

    if admin.role != UserRole.SUPERADMIN:
        granted = set(await get_accessible_department_codes(db, admin))
        if doc.department and doc.department.code not in granted:
            raise ForbiddenError("Department not in your scope")

    raw_meta = payload.get("metadata") if isinstance(payload, dict) else None
    if not isinstance(raw_meta, dict):
        raise GuardrailBlocked("`metadata` must be an object")
    meta = normalize_metadata(raw_meta)

    # Mirror DB + S3 sidecar. Sidecar failure is logged but doesn't roll
    # back the DB update — the next KB sync will reconcile.
    doc.doc_metadata = meta
    sidecar_ok = await s3_service.update_kb_metadata(
        s3_key=doc.s3_key,
        department_code=doc.department.code if doc.department else "",
        filename=doc.filename,
        uploader_email=doc.uploader_email or admin.email,
        metadata=meta,
    )
    db.add(AuditLog(
        user_id=admin.id, user_email=admin.email,
        action="KB_RETAG", resource_type="kb_document",
        resource_id=doc.s3_key,
        details={
            "dept": doc.department.code if doc.department else None,
            "filename": doc.filename,
            "metadata": meta,
            "sidecar_updated": sidecar_ok,
        },
    ))
    await db.commit()
    await db.refresh(doc)
    log_event("admin", "info", "kb metadata updated",
              s3_key=doc.s3_key, by=admin.email, sidecar=sidecar_ok)
    return _doc_to_out(doc, doc.department.code if doc.department else None)


@router.delete("/kb/documents/{doc_id}", status_code=200)
async def delete_kb_document(
    doc_id: int,
    admin: User = _AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a KB doc, removing the underlying S3 object.

    Returns the ingestion job id (if any) so the UI can prompt the admin
    to wait for the resync to complete.
    """
    if not can_manage_kb(admin):
        raise ForbiddenError("KB management not permitted")

    doc = (await db.execute(
        select(KBDocument)
        .options(selectinload(KBDocument.department))
        .where(KBDocument.id == doc_id)
    )).scalar_one_or_none()
    if not doc:
        raise NotFoundError("Document not found")
    if doc.status == KBDocumentStatus.DELETED:
        raise GuardrailBlocked("Document already deleted")

    # RBAC: ensure admin may manage this dept.
    if admin.role != UserRole.SUPERADMIN:
        granted = set(await get_accessible_department_codes(db, admin))
        if doc.department and doc.department.code not in granted:
            raise ForbiddenError("Department not in your scope")

    # Remove the S3 object (best-effort). Audit + soft-delete regardless
    # so the management UI never shows phantom rows.
    s3_ok = await s3_service.delete_kb_document(doc.s3_key)
    doc.status = KBDocumentStatus.DELETED
    doc.deleted_at = datetime.now(timezone.utc)

    db.add(AuditLog(
        user_id=admin.id, user_email=admin.email,
        action="KB_DELETE", resource_type="kb_document",
        resource_id=doc.s3_key,
        details={"dept": doc.department.code if doc.department else None,
                 "filename": doc.filename, "s3_removed": s3_ok},
    ))
    await db.commit()

    log_event("admin", "info", "kb doc deleted",
              s3_key=doc.s3_key, by=admin.email, s3_removed=s3_ok)

    # **Auto-sync is disabled.** The UI will prompt the admin to click
    # the Sync button so the embeddings drop on their schedule.
    return {
        "status": "deleted",
        "document_id": doc.id,
        "s3_removed": s3_ok,
        "ingestion_job_id": None,
    }


# ---------------------------------------------------------------------------
# Guardrail events (clicked from the Overview "Guardrail blocks" metric)
# ---------------------------------------------------------------------------

@router.get("/guardrail-events")
async def list_guardrail_events(
    limit: int = Query(50, ge=1, le=500),
    admin: User = _AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Recent assistant messages that were blocked by a guardrail.

    Returned shape is denormalised for direct rendering in the admin
    modal — it bundles per-message detail (model, citations metadata,
    block reasons, user, dept) with the active retrieval-stack config
    (vector store, search type, rerank backend). The stack fields are
    config-wide today; if you later attach them per-message, just
    promote them from the `stack` block to per-row fields.
    """
    rows = (await db.execute(
        select(ChatMessage, ChatSession, User, Department)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .join(User, User.id == ChatSession.user_id, isouter=True)
        .join(Department, Department.id == ChatSession.department_id, isouter=True)
        .where(ChatMessage.blocked_by_guardrail.is_(True))
        .order_by(ChatMessage.id.desc())
        .limit(limit)
    )).all()

    events = []
    for (msg, sess, u, d) in rows:
        cit = msg.citations or {}
        # Trim citation items so the response stays small.
        slim_items = [
            {
                "title": c.get("title"),
                "department": c.get("department"),
                "page": c.get("page"),
                "score": c.get("score"),
                "rerank_score": c.get("rerank_score"),
            }
            for c in (cit.get("items") or [])[:5]
        ]
        events.append({
            "id": msg.id,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
            "session_id": sess.id if sess else None,
            "user_email": u.email if u else None,
            "department_code": d.code if d else None,
            "model_id": msg.model_id,
            "confidence": msg.confidence,
            "latency_ms": msg.latency_ms,
            "tokens_input": msg.tokens_input,
            "tokens_output": msg.tokens_output,
            "block_reasons": msg.block_reasons,
            "answer_excerpt": (msg.content or "")[:240],
            "citations": {
                "source": cit.get("source"),
                "depts": cit.get("depts"),
                "items": slim_items,
                "count": len(cit.get("items") or []),
            },
        })

    return {
        "events": events,
        "stack": {
            "vector_store": settings.BEDROCK_KB_VECTOR_STORE,
            "search_type_override": settings.BEDROCK_KB_SEARCH_TYPE_OVERRIDE or None,
            "rerank_enabled": settings.RERANK_ENABLED,
            "rerank_aws_model_arn": settings.BEDROCK_RERANK_MODEL_ARN or None,
            "rerank_local_model": settings.LOCAL_RERANK_MODEL,
            "default_model_id": settings.BEDROCK_MODEL_ID,
            "embedding_model_id": settings.BEDROCK_EMBEDDING_MODEL_ID,
            "guardrail_id": settings.BEDROCK_GUARDRAIL_ID or None,
            "guardrail_version": settings.BEDROCK_GUARDRAIL_VERSION,
        },
    }


# ---------------------------------------------------------------------------
# Dept admins lookup (used by chat when a user's only dept is deactivated)
# ---------------------------------------------------------------------------

@router.get("/dept-admins/{code}", include_in_schema=False)
async def admins_for_dept(
    code: str,
    admin: User = _AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Admin-only convenience route. The public-facing variant lives
    in auth.py (no admin token required) so end users can call it."""
    return await _admins_for_dept(db, code)


async def _admins_for_dept(db: AsyncSession, code: str) -> list[dict]:
    """Return contact details for everyone who can manage `code`.

    Includes: SUPERADMIN (always), ADMIN with that home dept,
    CROSSADMIN with that code in their extra grants. Used when a user
    lands on the chat page only to discover their dept has been
    deactivated and needs to know whom to email.
    """
    c = (code or "").lower().strip()
    if not c:
        return []
    dept = (await db.execute(
        select(Department).where(Department.code == c)
    )).scalar_one_or_none()

    # Superadmins always count.
    super_rows = (await db.execute(
        select(User).where(
            User.role == UserRole.SUPERADMIN,
            User.is_active.is_(True),
        )
    )).scalars().all()

    admin_rows: list = []
    crossadmin_rows: list = []
    if dept:
        admin_rows = (await db.execute(
            select(User).where(
                User.role == UserRole.ADMIN,
                User.department_id == dept.id,
                User.is_active.is_(True),
            )
        )).scalars().all()
        crossadmin_rows = (await db.execute(
            select(User)
            .join(user_departments, user_departments.c.user_id == User.id)
            .where(
                User.role == UserRole.CROSSADMIN,
                User.is_active.is_(True),
                user_departments.c.department_id == dept.id,
            )
        )).scalars().all()

    seen = set()
    out = []
    for u in list(super_rows) + list(admin_rows) + list(crossadmin_rows):
        if u.email in seen:
            continue
        seen.add(u.email)
        out.append({
            "email": u.email,
            "name": u.full_name,
            "role": u.role.value if hasattr(u.role, "value") else str(u.role),
        })
    return out


# ---------------------------------------------------------------------------
# AI model for Users & Admins (the "standard" scope)
#
# This is DISTINCT from the CrossAdmin/SuperAdmin model (which they pick in
# the chat window). Changing it here only affects USER and ADMIN roles.
# ---------------------------------------------------------------------------

_STD_MODEL_SETTERS = (UserRole.CROSSADMIN, UserRole.SUPERADMIN)


@router.get("/model")
async def get_standard_model(
    admin: User = _AdminUser, db: AsyncSession = Depends(get_db),
):
    """The model served to regular Users & Admins. Any admin may view it;
    only CrossAdmin / SuperAdmin may change it (see POST)."""
    active = await get_active_model(db, SCOPE_STANDARD)
    return {
        "models": available_models(),
        "active": active,
        "can_switch": admin.role in _STD_MODEL_SETTERS,
        "scope": "standard",
        "default_monthly_token_limit": int(settings.DEFAULT_MONTHLY_TOKEN_LIMIT),
    }


@router.post("/model")
async def set_standard_model(
    payload: ModelSelect,
    admin: User = _AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Set the model used by Users & Admins. CrossAdmin / SuperAdmin only.
    Does NOT affect CrossAdmin / SuperAdmin's own chats."""
    if admin.role not in _STD_MODEL_SETTERS:
        raise ForbiddenError("Only CrossAdmin / SuperAdmin may change this model")
    try:
        active = await set_active_model(db, SCOPE_STANDARD, payload.model_id)
    except ValueError as e:
        raise GuardrailBlocked(str(e))
    db.add(AuditLog(
        user_id=admin.id, user_email=admin.email,
        action="STD_MODEL_SET", resource_type="app_setting",
        details={"model": active},
    ))
    await db.commit()
    log_event("admin", "info", "standard model changed", model=active, by=admin.email)
    return {"active": active}


# ---------------------------------------------------------------------------
# User-accessible model set — admin picks which models users may switch between
# ---------------------------------------------------------------------------

@router.get("/user-models")
async def get_user_models_endpoint(
    admin: User = _AdminUser, db: AsyncSession = Depends(get_db),
):
    """Full model catalog + which model IDs are currently available to users.

    Any admin may view; CrossAdmin / SuperAdmin may change via POST.
    """
    all_models = available_models()
    user_model_ids = await get_user_models_svc(db)
    user_model_id_set = set(user_model_ids)
    return {
        "all_models": all_models,
        "user_model_ids": [m["id"] for m in all_models if m["id"] in user_model_id_set],
        "can_manage": admin.role in _STD_MODEL_SETTERS,
    }


@router.post("/user-models")
async def set_user_models_endpoint(
    payload: UserModelsPayload,
    admin: User = _AdminUser,
    db: AsyncSession = Depends(get_db),
):
    """Set which models regular users may pick from in chat.
    CrossAdmin / SuperAdmin only."""
    if admin.role not in _STD_MODEL_SETTERS:
        raise ForbiddenError("Only CrossAdmin / SuperAdmin may change user model access")
    valid_ids = {m["id"] for m in available_models()}
    invalid = [mid for mid in payload.model_ids if mid not in valid_ids]
    if invalid:
        raise GuardrailBlocked(f"Unknown model IDs: {', '.join(invalid[:3])}")
    await set_user_models_svc(db, payload.model_ids)
    db.add(AuditLog(
        user_id=admin.id, user_email=admin.email,
        action="USER_MODELS_SET", resource_type="app_setting",
        details={"model_ids": payload.model_ids, "count": len(payload.model_ids)},
    ))
    await db.commit()
    log_event("admin", "info", "user model access updated",
              by=admin.email, count=len(payload.model_ids))
    return {"user_model_ids": payload.model_ids}


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@router.get("/audit")
async def list_audit(
    limit: int = 100, _=_AdminUser, db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500))
    )
    return [
        {
            "id": a.id, "user_email": a.user_email, "action": a.action,
            "resource_type": a.resource_type, "resource_id": a.resource_id,
            "ip": a.ip_address, "created_at": a.created_at.isoformat(),
            "details": a.details,
        }
        for a in res.scalars()
    ]
