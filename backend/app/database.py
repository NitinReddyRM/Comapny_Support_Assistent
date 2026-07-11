"""
Async SQLAlchemy engine + session factory.

We expose:
- `engine` / `AsyncSessionLocal` for async usage in API handlers
- `Base` for declarative models
- `get_db()` dependency for FastAPI routes
- `init_db()` bootstrap that creates tables on first run

Async engine uses asyncpg; the sync URL is only used by Alembic.
"""
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


# SQLite doesn't support pool_size / max_overflow; only pass them for
# server-style backends (Postgres / MySQL).
_engine_kwargs: dict = {"echo": False, "pool_pre_ping": True}
if not settings.DATABASE_URL.startswith("sqlite"):
    _engine_kwargs["pool_size"] = 10
    _engine_kwargs["max_overflow"] = 20

engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a transactional session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Create all tables. Use Alembic for real migrations in production."""
    # Import models so they register on Base.metadata.
    from app.models import (  # noqa: F401
        user,
        department,
        chat,
        feedback,
        ticket,
        audit,
        otp,
        kb_document,
        app_setting,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # ---- Lightweight migrations --------------------------------
        # The deprecated DEPT_ADMIN role no longer exists in UserRole.
        # Any rows still carrying it from an older install would fail
        # to deserialize on next load — convert them to USER here.
        try:
            await conn.execute(
                text("UPDATE users SET role = 'USER' WHERE role = 'DEPT_ADMIN'")
            )
        except Exception:
            # If the SAEnum native type forbids the value we still want
            # the app to start; the SQLAlchemy enum check would already
            # have failed at SELECT-time. Logged but non-fatal.
            pass

        # Add monthly_token_limit column to users on existing installs.
        # SQLite: ALTER TABLE ... ADD COLUMN throws if it already exists,
        # so swallow that one error. Postgres handles IF NOT EXISTS natively.
        is_sqlite = settings.DATABASE_URL.startswith("sqlite")
        try:
            if is_sqlite:
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN monthly_token_limit INTEGER")
                )
            else:
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN IF NOT EXISTS monthly_token_limit INTEGER")
                )
        except Exception:
            # Column already exists (SQLite path) — nothing to do.
            pass

        # Add block_reasons JSON column to chat_messages on existing installs.
        try:
            if is_sqlite:
                # SQLite stores JSON as TEXT; the ORM column type maps to JSON
                # but DDL-wise it's just a textual column.
                await conn.execute(
                    text("ALTER TABLE chat_messages ADD COLUMN block_reasons JSON")
                )
            else:
                await conn.execute(
                    text("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS block_reasons JSON")
                )
        except Exception:
            pass

        # Add `doc_metadata` JSON column to kb_documents (arbitrary
        # admin-defined {key: value} metadata used for retrieval filters).
        try:
            if is_sqlite:
                await conn.execute(
                    text("ALTER TABLE kb_documents ADD COLUMN doc_metadata JSON")
                )
            else:
                await conn.execute(
                    text("ALTER TABLE kb_documents ADD COLUMN IF NOT EXISTS doc_metadata JSON")
                )
        except Exception:
            pass

        # Add preferred_model column to users on existing installs.
        try:
            if is_sqlite:
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN preferred_model VARCHAR(512)")
                )
            else:
                await conn.execute(
                    text("ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_model VARCHAR(512)")
                )
        except Exception:
            pass
