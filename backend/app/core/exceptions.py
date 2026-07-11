"""Custom application exceptions translated to HTTP errors by handlers."""
from fastapi import HTTPException, status


class AuthError(HTTPException):
    def __init__(self, detail: str = "Authentication failed"):
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


class ForbiddenError(HTTPException):
    def __init__(self, detail: str = "Not authorized"):
        super().__init__(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


class NotFoundError(HTTPException):
    def __init__(self, detail: str = "Not found"):
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


class GuardrailBlocked(HTTPException):
    def __init__(self, reason: str):
        super().__init__(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Blocked: {reason}")


class RateLimited(HTTPException):
    def __init__(self):
        super().__init__(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many requests")
