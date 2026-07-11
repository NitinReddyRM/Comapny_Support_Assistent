"""Runtime app-settings helpers, backed by the `app_settings` table.

The chat model is split into TWO scopes so the two audiences are managed
independently:

  * "privileged"  — used by CROSSADMIN / SUPERADMIN. Set by THEM from the
                    chat-window model picker. Never touched by the admin
                    portal.
  * "standard"    — used by USER / ADMIN. Set from the admin portal. Never
                    affects CROSSADMIN / SUPERADMIN.

Each falls back to the configured default (`BEDROCK_MODEL_ID`) when unset
or when a stored value is no longer in the allow-list.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.model_catalog import allowed_model_ids
from app.models.app_setting import AppSetting

SCOPE_PRIVILEGED = "privileged"
SCOPE_STANDARD = "standard"

_KEYS = {
    SCOPE_PRIVILEGED: "active_model_privileged",
    SCOPE_STANDARD: "active_model_standard",
}


def _key(scope: str) -> str:
    try:
        return _KEYS[scope]
    except KeyError:
        raise ValueError(f"Unknown model scope '{scope}'")


async def _get(db: AsyncSession, key: str) -> str | None:
    row = (await db.execute(
        select(AppSetting).where(AppSetting.key == key)
    )).scalar_one_or_none()
    return row.value if row else None


async def _set(db: AsyncSession, key: str, value: str) -> None:
    row = (await db.execute(
        select(AppSetting).where(AppSetting.key == key)
    )).scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    await db.commit()


async def get_active_model(db: AsyncSession, scope: str) -> str:
    """The model generated with for the given scope.

    Falls back to the configured default if unset or no longer offered.
    """
    stored = await _get(db, _key(scope))
    if stored and stored in allowed_model_ids():
        return stored
    return settings.BEDROCK_MODEL_ID


async def set_active_model(db: AsyncSession, scope: str, model_id: str) -> str:
    """Persist the active model for a scope. Caller enforces RBAC; this
    only validates the model id is one we offer."""
    if not model_id or model_id not in allowed_model_ids():
        raise ValueError(f"Model '{model_id}' is not selectable")
    await _set(db, _key(scope), model_id)
    return model_id


# ---- User-accessible model set (admin-configurable) ----------------------

_KEY_USER_MODELS = "user_selectable_models"


async def get_user_models(db: AsyncSession) -> list[str]:
    """Model IDs regular users can switch between.

    Stored in AppSetting by admin via POST /admin/user-models.
    Falls back to USER_SELECTABLE_MODELS env var when the admin has not
    configured anything yet.
    """
    stored = await _get(db, _KEY_USER_MODELS)
    if stored is not None:
        return [mid.strip() for mid in stored.split(",") if mid.strip()]
    from app.core.model_catalog import user_selectable_models
    return [m["id"] for m in user_selectable_models()]


async def set_user_models(db: AsyncSession, model_ids: list[str]) -> None:
    """Persist the admin-chosen model IDs that regular users may pick from."""
    await _set(db, _KEY_USER_MODELS, ",".join(model_ids))
