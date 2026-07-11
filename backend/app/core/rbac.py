"""
Role-based access helpers.

Used as FastAPI dependencies to enforce minimum role / matching
department on a per-route basis. Centralizes the rules for *which
departments a user can see*, so callers never have to special-case
SUPERADMIN / CROSSADMIN.
"""
from typing import Iterable, List, Set

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError
from app.models.user import User, UserRole
from app.models.department import Department
from app.api.deps import get_current_user

# Ordered for "at least this role" checks.
_ROLE_ORDER = {
    UserRole.USER: 0,
    UserRole.ADMIN: 1,
    UserRole.CROSSADMIN: 2,
    UserRole.SUPERADMIN: 3,
}


def role_rank(role: UserRole) -> int:
    return _ROLE_ORDER.get(role, -1)


def require_role(*roles: UserRole):
    """Dependency factory: only the listed roles may pass."""
    allowed = set(roles)

    async def _checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise ForbiddenError("Insufficient role")
        return user

    return _checker


def require_min_role(min_role: UserRole):
    """Dependency factory: user must have at least `min_role`."""
    threshold = _ROLE_ORDER[min_role]

    async def _checker(user: User = Depends(get_current_user)) -> User:
        if _ROLE_ORDER.get(user.role, -1) < threshold:
            raise ForbiddenError("Insufficient role")
        return user

    return _checker


def is_global_admin(user: User) -> bool:
    """SUPERADMIN can see/do anything across departments."""
    return user.role == UserRole.SUPERADMIN


def is_cross_admin(user: User) -> bool:
    """Operator with explicit grants to multiple departments."""
    return user.role == UserRole.CROSSADMIN


def is_admin_like(user: User) -> bool:
    """Any admin variant (used for management UIs / RBAC gates)."""
    return user.role in (UserRole.ADMIN, UserRole.CROSSADMIN, UserRole.SUPERADMIN)


def can_manage_kb(user: User) -> bool:
    """Who can upload/delete KB files. Scope of *which* files is then
    enforced row-by-row in the admin endpoints."""
    return user.role in (UserRole.ADMIN, UserRole.CROSSADMIN, UserRole.SUPERADMIN)


async def get_accessible_department_codes(db: AsyncSession, user: User) -> List[str]:
    """Return the set of department `code`s this user may read from.

    - SUPERADMIN: every active department (global).
    - CROSSADMIN: home dept + every dept in `extra_departments`.
    - ADMIN:     ONLY their home department. ADMINs are scoped to a
                 single department for both viewing and management.
    - USER: only their home dept.
    """
    if user.role == UserRole.SUPERADMIN:
        rows = (await db.execute(
            select(Department.code).where(Department.is_active.is_(True))
        )).scalars().all()
        return list(rows)

    if user.role == UserRole.CROSSADMIN:
        codes: Set[str] = set()
        if user.department and user.department.is_active:
            codes.add(user.department.code)
        for d in (user.extra_departments or []):
            if d.is_active:
                codes.add(d.code)
        return sorted(codes)

    # ADMIN / USER — pinned to home dept only.
    if user.department and user.department.is_active:
        return [user.department.code]
    return []


def require_same_department(user: User, dept_id: int) -> None:
    """Enforce that a USER only acts on their own department.

    SUPERADMIN bypasses. ADMIN must match its single home dept.
    CROSSADMIN callers that need granted-set matching should use
    `get_accessible_department_codes` instead.
    """
    if user.role == UserRole.SUPERADMIN:
        return
    if user.role == UserRole.CROSSADMIN:
        # Caller is responsible for granted-set checking.
        return
    if user.department_id != dept_id:
        raise ForbiddenError("Cross-department access denied")
