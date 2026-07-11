"""User model. Authentication is email + OTP (passwordless)."""
import enum
from datetime import datetime

from sqlalchemy import String, Boolean, DateTime, ForeignKey, Enum as SAEnum, func, Table, Column, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserRole(str, enum.Enum):
    """Application roles.

    The order from least to most privileged is significant — see
    `app.core.rbac._ROLE_ORDER`.

      USER       — end user, single home department.
      ADMIN      — admin of a single department (KB + users in that dept).
      CROSSADMIN — admin across an explicitly-granted set of departments.
      SUPERADMIN — global. Manages departments, can create any role.

    Note: a legacy `DEPT_ADMIN` value previously existed; it is no
    longer used. The `init_db` migration in app.database converts any
    rows still carrying that role to `USER`.
    """
    USER = "USER"
    ADMIN = "ADMIN"
    CROSSADMIN = "CROSSADMIN"
    SUPERADMIN = "SUPERADMIN"


# Many-to-many: a CROSSADMIN (and optionally SUPERADMIN) can be granted
# explicit access to multiple departments. The single `users.department_id`
# stays as the *default / home* department for backward compatibility and
# is the only granted department for USER / ADMIN.
user_departments = Table(
    "user_departments",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("department_id", Integer, ForeignKey("departments.id", ondelete="CASCADE"), primary_key=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    UniqueConstraint("user_id", "department_id", name="uq_user_department"),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role"), default=UserRole.USER, index=True
    )
    # Default / home department. Always the only granted department for
    # USER, ADMIN. For CROSSADMIN this is the first dept they
    # picked at login (used for tickets / default context); additional
    # access is recorded via `extra_departments`.
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    department = relationship("Department", lazy="joined", foreign_keys=[department_id])

    # Additional departments granted to CROSSADMIN users. SUPERADMIN
    # implicitly has access to all departments — we don't fan-out grants
    # for them.
    extra_departments = relationship(
        "Department",
        secondary=user_departments,
        lazy="selectin",
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Per-user monthly LLM token budget (sum of input+output across all
    # the user's chat sessions in the current calendar month, UTC). NULL
    # means "use the system default" (settings.DEFAULT_MONTHLY_TOKEN_LIMIT).
    # Setting it to 0 explicitly disables chat for the user.
    monthly_token_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Per-user model preference. Regular users may pick from the two
    # models defined in USER_SELECTABLE_MODELS. NULL means "use the
    # admin-set standard model".
    preferred_model: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
