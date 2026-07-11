"""Auth-related request/response models."""
from pydantic import BaseModel, EmailStr, Field
from typing import List, Optional


class OTPRequest(BaseModel):
    email: EmailStr


class OTPVerify(BaseModel):
    email: EmailStr
    code: str = Field(min_length=4, max_length=10)


class DepartmentSelect(BaseModel):
    """Legacy single-department selection."""
    department_code: str


class DepartmentsSelect(BaseModel):
    """Multi-department selection (CrossAdmin / SuperAdmin)."""
    department_codes: List[str] = Field(min_length=1, max_length=32)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: "UserPublic"


class UserPublic(BaseModel):
    id: int
    email: EmailStr
    full_name: Optional[str] = None
    role: str
    department_code: Optional[str] = None
    department_name: Optional[str] = None
    # Full set of departments accessible in this session — single element
    # for ordinary roles, multi-element for CrossAdmin / SuperAdmin.
    department_codes: List[str] = []

    model_config = {"from_attributes": True}


TokenResponse.model_rebuild()
