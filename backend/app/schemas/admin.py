"""Admin-side request/response schemas."""
from datetime import datetime
from typing import Optional, List, Any, Dict
from pydantic import BaseModel, EmailStr, Field, field_validator


def _coerce_optional_str(v: Any) -> Optional[str]:
    """Treat '', '   ', and missing as None.

    The admin UI happily sends empty strings for optional fields when
    inputs are blanked; without this coercion Pydantic accepts the
    empty string for `Optional[str]` but downstream code then has to
    keep checking for it.
    """
    if v is None:
        return None
    if isinstance(v, str):
        stripped = v.strip()
        return stripped or None
    return v


def _coerce_list_of_str(v: Any) -> List[str]:
    """Accept None, str, or list. Strip + dedupe + lowercase."""
    if v is None:
        return []
    if isinstance(v, str):
        v = [v] if v else []
    seen, out = set(), []
    for item in v:
        if not isinstance(item, str):
            continue
        c = item.strip().lower()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


class DepartmentCreate(BaseModel):
    code: str = Field(pattern=r"^[a-z0-9_-]{2,64}$")
    name: str
    description: Optional[str] = None
    support_email: Optional[EmailStr] = None


class DepartmentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    support_email: Optional[EmailStr] = None
    is_active: Optional[bool] = None


class DepartmentOut(BaseModel):
    id: int
    code: str
    name: str
    description: Optional[str] = None
    support_email: Optional[str] = None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None
    role: str = "USER"
    department_code: Optional[str] = None
    # Optional extra departments (only honoured for CROSSADMIN role).
    department_codes: List[str] = Field(default_factory=list)
    # NULL => use system default. 0 => deny chat for this user.
    monthly_token_limit: Optional[int] = None
    # Per-user model preference (one of USER_SELECTABLE_MODELS). NULL => standard.
    preferred_model: Optional[str] = None

    @field_validator("full_name", "department_code", mode="before")
    @classmethod
    def _strip_optional(cls, v):
        return _coerce_optional_str(v)

    @field_validator("department_codes", mode="before")
    @classmethod
    def _normalise_codes(cls, v):
        return _coerce_list_of_str(v)

    @field_validator("role", mode="before")
    @classmethod
    def _strip_role(cls, v):
        if isinstance(v, str):
            return v.strip().upper() or "USER"
        return v

    @field_validator("monthly_token_limit", mode="before")
    @classmethod
    def _coerce_limit(cls, v):
        # Treat "", None, "null" as None (use system default).
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            if not s or s.lower() == "null":
                return None
            return int(s)
        return int(v)


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    department_code: Optional[str] = None
    department_codes: Optional[List[str]] = None
    is_active: Optional[bool] = None
    monthly_token_limit: Optional[int] = None
    preferred_model: Optional[str] = None

    @field_validator("full_name", "department_code", "role", mode="before")
    @classmethod
    def _strip_opt(cls, v):
        v = _coerce_optional_str(v)
        if v is not None and isinstance(v, str):
            return v.strip()
        return v

    @field_validator("department_codes", mode="before")
    @classmethod
    def _normalise_codes(cls, v):
        if v is None:
            return None
        return _coerce_list_of_str(v)

    @field_validator("monthly_token_limit", mode="before")
    @classmethod
    def _coerce_limit(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            if not s or s.lower() == "null":
                return None
            return int(s)
        return int(v)


class UserOut(BaseModel):
    id: int
    email: str
    full_name: Optional[str] = None
    role: str
    department_code: Optional[str] = None
    department_codes: List[str] = Field(default_factory=list)
    is_active: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime
    monthly_token_limit: Optional[int] = None
    # Computed: how many tokens this user has used this calendar month.
    # Populated by the admin endpoint on read; ignored on write.
    monthly_tokens_used: Optional[int] = None
    # Per-user model preference. NULL means the admin-set standard model.
    preferred_model: Optional[str] = None

    model_config = {"from_attributes": True}


class UploadResult(BaseModel):
    s3_key: str
    s3_uri: str
    department_code: str
    # Value may be a string or list of strings (multi-value tags entered
    # comma-separated, e.g. region = ["india", "russia", "usa"]).
    metadata: Dict[str, Any] = Field(default_factory=dict)
    bytes: int
    ingestion_job_id: Optional[str] = None
    document_id: Optional[int] = None


# ---------- KB document management ----------

class KBDocumentOut(BaseModel):
    # `id` is null for S3-only rows that aren't tracked in our DB.
    id: Optional[int] = None
    filename: str
    s3_key: str
    content_type: Optional[str] = None
    size_bytes: int = 0
    department_id: Optional[int] = None
    department_code: Optional[str] = None
    # Arbitrary admin-defined metadata attached at upload, e.g.
    # {"region": "india", "year": "2024"} or {"region": ["india","russia"]}
    # for multi-value tags. Empty for legacy rows.
    metadata: Dict[str, Any] = Field(default_factory=dict)
    uploader_email: Optional[str] = None
    status: str
    ingestion_job_id: Optional[str] = None
    last_ingested_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    # True when the file exists only in S3 and isn't tracked in our DB.
    # The UI uses this to show a different badge ("External").
    external: bool = False

    model_config = {"from_attributes": True}


class KBDocumentListResponse(BaseModel):
    items: List[KBDocumentOut]
    total: int
    page: int
    page_size: int


class BulkUploadItemFailure(BaseModel):
    filename: Optional[str] = None
    error: str


class BulkUploadItemSkipped(BaseModel):
    filename: Optional[str] = None
    reason: str
    document_id: Optional[int] = None


class BulkUploadResult(BaseModel):
    department_code: str
    succeeded: List[KBDocumentOut] = Field(default_factory=list)
    skipped: List[dict] = Field(default_factory=list)
    failed: List[dict] = Field(default_factory=list)


class UserModelsPayload(BaseModel):
    model_ids: List[str] = Field(default_factory=list)


class AnalyticsOverview(BaseModel):
    daily_active_users: int
    total_messages: int
    total_sessions: int
    avg_latency_ms: float
    avg_confidence: float
    feedback_helpful: int
    feedback_not_helpful: int
    guardrail_violations: int
    top_queries: List[dict]
    department_usage: List[dict]
