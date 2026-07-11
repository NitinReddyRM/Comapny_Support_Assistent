"""
Knowledge Base document tracking.

Every admin upload creates a row here so we can:
  * Render a management UI (list / search / filter / paginate / delete)
  * Detect duplicate uploads via content hash
  * Audit who uploaded what and when
  * Support soft-delete with a tombstone status, while the actual S3
    object is removed eagerly on delete.
"""
import enum
from datetime import datetime

from sqlalchemy import String, DateTime, ForeignKey, Integer, BigInteger, JSON, Enum as SAEnum, func, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class KBDocumentStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DELETED = "DELETED"            # soft-deleted, S3 object removed
    INGESTING = "INGESTING"        # KB sync in progress
    INGESTION_FAILED = "INGESTION_FAILED"


class KBDocument(Base):
    __tablename__ = "kb_documents"

    id: Mapped[int] = mapped_column(primary_key=True)

    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id", ondelete="CASCADE"), index=True
    )
    department = relationship("Department", lazy="joined")

    # Original filename as uploaded.
    filename: Mapped[str] = mapped_column(String(512))
    # S3 object key (dept/filename). Unique across active docs.
    s3_key: Mapped[str] = mapped_column(String(1024), index=True)
    # Arbitrary, admin-defined metadata for this document — an open-ended
    # dict of {key: value} (e.g. {"region": "india", "year": "2024"}).
    # Every entry is written into the Bedrock KB sidecar so it can be used
    # as a retrieval filter, and surfaced to the chat UI as a facet. NULL
    # / empty on legacy rows. Stored as JSON.
    doc_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)

    # SHA-256 of file contents. Used to short-circuit duplicate uploads
    # against the same department.
    content_hash: Mapped[str] = mapped_column(String(64), index=True)

    uploader_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    uploader_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[KBDocumentStatus] = mapped_column(
        SAEnum(KBDocumentStatus, name="kb_document_status"),
        default=KBDocumentStatus.ACTIVE,
        index=True,
    )

    # Latest ingestion job id, if known.
    ingestion_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_ingested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Fast dedup lookup on (dept, hash, active).
        Index("ix_kbdoc_dept_hash_status", "department_id", "content_hash", "status"),
        # List-by-department-newest-first.
        Index("ix_kbdoc_dept_created", "department_id", "created_at"),
    )
