"""Per-message thumbs up/down feedback."""
import enum
from datetime import datetime
from sqlalchemy import String, DateTime, ForeignKey, Text, Enum as SAEnum, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class FeedbackKind(str, enum.Enum):
    HELPFUL = "HELPFUL"
    NOT_HELPFUL = "NOT_HELPFUL"


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("chat_messages.id", ondelete="CASCADE"), unique=True, index=True
    )
    kind: Mapped[FeedbackKind] = mapped_column(SAEnum(FeedbackKind, name="feedback_kind"))
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
