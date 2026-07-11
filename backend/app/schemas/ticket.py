"""Ticketing schemas."""
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel


class TicketCreate(BaseModel):
    subject: str
    query: str
    ai_response: Optional[str] = None
    chat_session_id: Optional[int] = None
    priority: Optional[str] = "MEDIUM"


class TicketUpdate(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None
    assignee_id: Optional[int] = None
    resolution_notes: Optional[str] = None


class TicketCommentCreate(BaseModel):
    body: str
    is_internal: bool = False


class TicketCommentOut(BaseModel):
    id: int
    author_id: int
    body: str
    is_internal: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TicketOut(BaseModel):
    id: int
    user_id: int
    department_id: int
    subject: str
    query: str
    ai_response: Optional[str] = None
    status: str
    priority: str
    assignee_id: Optional[int] = None
    resolution_notes: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    comments: List[TicketCommentOut] = []

    model_config = {"from_attributes": True}


class FeedbackRequest(BaseModel):
    message_id: int
    kind: str  # HELPFUL | NOT_HELPFUL
    comment: Optional[str] = None
