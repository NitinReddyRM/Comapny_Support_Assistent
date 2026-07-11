"""Chat-related request/response schemas."""
from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    session_id: Optional[int] = None
    query: str = Field(min_length=1, max_length=4000)
    # Arbitrary metadata facets the user selected to scope this query,
    # e.g. {"region": "india", "year": "2024"}. Each becomes a Bedrock
    # retrieval `equals` filter. Optional — empty means "search the whole
    # department". Values may be a string or a list of strings (match any).
    metadata_filters: Optional[Dict[str, Any]] = None


# ---------- Superadmin diagnostics ----------
#
# A LangGraph-style trace of what the chat pipeline did. Emitted only for
# SUPERADMIN callers (gated server-side). Each step records its outcome,
# how long it took, and a small `detail` payload so a superadmin can
# inspect why an answer turned out the way it did.

class ChatTraceStep(BaseModel):
    # Stable id for the pipeline node, e.g. "rule_engine", "guardrail_in",
    # "cache", "retrieval", "generation", "guardrail_out".
    step: str
    # "ok" | "skipped" | "hit" | "miss" | "blocked" | "error"
    status: str
    duration_ms: int = 0
    # Step-specific extras (citation count, token usage, reasons, etc.).
    detail: Dict[str, Any] = Field(default_factory=dict)


class ChatDiagnostics(BaseModel):
    # 0..100 — higher means LESS grounded / more likely hallucinated.
    hallucination_pct: int = 0
    # Raw model self-reported confidence from the tail JSON block (0..1).
    model_confidence: float = 0.0
    # Avg top-3 KB retrieval score (0..1). 0 when no citations.
    retrieval_score: float = 0.0
    # Final blended groundedness (mirrors ChatResponse.confidence; 0..1).
    groundedness: float = 0.0
    # Ordered step trace.
    trace: List[ChatTraceStep] = Field(default_factory=list)


class ModelSelect(BaseModel):
    """Body for POST /chat/models/active — set the global chat model."""
    model_id: str = Field(min_length=1, max_length=160)


class Citation(BaseModel):
    title: str = ""
    s3_uri: Optional[str] = None
    page: Optional[int] = None
    snippet: Optional[str] = None
    score: Optional[float] = None
    department: Optional[str] = None

    model_config = {"extra": "ignore"}


class ChatResponse(BaseModel):
    session_id: int
    message_id: int
    answer: str
    citations: List[Citation] = Field(default_factory=list)
    confidence: float = 0.0
    suggestions: List[str] = Field(default_factory=list)
    related: List[str] = Field(default_factory=list)
    latency_ms: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    blocked: bool = False
    blocked_reason: Optional[str] = None
    # "rule_engine" | "kb" | "llm" — tells the UI where the answer came from.
    source: str = "llm"
    # Pipeline trace + hallucination score. Populated server-side only for
    # SUPERADMIN callers; None for everyone else so the privileged view
    # never leaks. The UI also defensively hides it from non-superadmins.
    diagnostics: Optional[ChatDiagnostics] = None


class ChatMessageOut(BaseModel):
    id: int
    role: str
    content: str
    citations: Optional[Dict[str, Any]] = None
    confidence: Optional[float] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatSessionOut(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SuggestionRequest(BaseModel):
    prefix: str = Field(min_length=1, max_length=200)


class SuggestionResponse(BaseModel):
    suggestions: List[str]
