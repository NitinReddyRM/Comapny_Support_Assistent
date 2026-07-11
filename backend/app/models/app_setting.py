"""Generic key/value application settings (singleton-style runtime config).

Currently used for the globally-selected chat model so that whatever a
CROSSADMIN / SUPERADMIN picks becomes the model every user is served.
Kept generic so other runtime toggles can reuse the same table.
"""
from datetime import datetime

from sqlalchemy import String, Text, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
