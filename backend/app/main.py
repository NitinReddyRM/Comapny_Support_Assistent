"""
FastAPI application entry point.

  * Wires routers
  * CORS, request-id / access logging middleware
  * DB bootstrap + seed of default departments and superadmin
  * Mounts the frontend as static files in dev for one-command serving
"""
from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.api import admin, analytics, auth, chat, feedback, tickets
from app.config import settings
from app.core.rate_limit import limiter
from app.database import AsyncSessionLocal, init_db
from app.models.department import Department
from app.models.user import User, UserRole
from app.services.cache import cache
from app.utils.logger import log_event


DEFAULT_DEPTS = [
    ("hr", "Human Resources", "Policies, leave, payroll, benefits"),
    ("finance", "Finance", "Expenses, reimbursements, invoicing"),
    ("it", "Information Technology", "Devices, accounts, VPN, helpdesk"),
    ("legal", "Legal", "Contracts, NDAs, compliance"),
    ("operations", "Operations", "Facilities, logistics"),
    ("security", "Security", "Incidents, access, policies"),
    ("procurement", "Procurement", "Vendors, purchase orders"),
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    await limiter.init()
    await cache.init()
    await _seed_defaults()
    log_event("api", "info", "app started", env=settings.APP_ENV, name=settings.APP_NAME)
    yield
    log_event("api", "info", "app stopped")


async def _seed_defaults():
    async with AsyncSessionLocal() as db:
        # Seed departments
        for code, name, desc in DEFAULT_DEPTS:
            existing = (await db.execute(select(Department).where(Department.code == code))).scalar_one_or_none()
            if not existing:
                db.add(Department(code=code, name=name, description=desc,
                                  support_email=f"{code}-support@company.example"))
        # Seed superadmin
        sa_email = settings.SUPERADMIN_EMAIL.lower()
        sa = (await db.execute(select(User).where(User.email == sa_email))).scalar_one_or_none()
        if not sa:
            db.add(User(email=sa_email, full_name="Super Admin", role=UserRole.SUPERADMIN))
        await db.commit()


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

# ---------- Middleware ----------

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
    t0 = time.monotonic()
    try:
        response = await call_next(request)
        latency_ms = int((time.monotonic() - t0) * 1000)
        response.headers["X-Request-Id"] = rid
        log_event(
            "api", "info", "request",
            rid=rid, method=request.method, path=request.url.path,
            status=response.status_code, latency_ms=latency_ms,
            ip=request.client.host if request.client else None,
        )
        return response
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        log_event("errors", "error", "unhandled",
                  rid=rid, path=request.url.path, error=str(e), latency_ms=latency_ms)
        return JSONResponse(status_code=500, content={"detail": "Internal server error", "rid": rid})


# ---------- Routers ----------

app.include_router(auth.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(feedback.router, prefix="/api/v1")
app.include_router(tickets.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(analytics.router, prefix="/api/v1")


@app.get("/api/health")
async def health():
    return {"status": "ok", "env": settings.APP_ENV, "version": "1.0.0"}


# ---------- Static frontend (dev convenience) ----------

_frontend = Path(__file__).resolve().parents[2] / "frontend"
if _frontend.exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app", host=settings.APP_HOST, port=settings.APP_PORT,
        reload=settings.APP_DEBUG,
    )
