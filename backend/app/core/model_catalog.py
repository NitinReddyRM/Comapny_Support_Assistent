"""Catalog of selectable Bedrock chat models for the in-chat model switcher.

Configured via `BEDROCK_SELECTABLE_MODELS` (comma-separated "modelId|Label"
pairs). The configured default (`BEDROCK_MODEL_ID`) is always present and
flagged. Only CROSSADMIN / SUPERADMIN may actually switch models — that gate
is enforced in the chat endpoints; this module just describes the options.

NOTE: generation goes through the Bedrock **Converse API**
(`bedrock_service.chat_stream`), which is model-agnostic, so mixing Anthropic
Claude, Amazon Nova, Meta Llama, etc. all work through the same code path.
"""
from __future__ import annotations

from app.config import settings


def _parse(spec: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "|" in part:
            mid, label = part.split("|", 1)
        else:
            mid, label = part, part
        mid = mid.strip()
        label = label.strip() or mid
        if mid and mid not in seen:
            seen.add(mid)
            out.append({"id": mid, "label": label})
    return out


def available_models() -> list[dict]:
    """Ordered list of selectable models with `{id, label, default}`."""
    models = _parse(settings.BEDROCK_SELECTABLE_MODELS)
    default = settings.BEDROCK_MODEL_ID
    ids = {m["id"] for m in models}
    if default and default not in ids:
        models.insert(0, {"id": default, "label": default})
    return [{**m, "default": m["id"] == default} for m in models]


def allowed_model_ids() -> set[str]:
    """The set of model ids a privileged user is permitted to select."""
    return {m["id"] for m in available_models()}


def user_selectable_models() -> list[dict]:
    """Models a regular USER / ADMIN may pick from (configured via
    USER_SELECTABLE_MODELS). Returns `[{id, label}]`."""
    return _parse(settings.USER_SELECTABLE_MODELS)


def user_allowed_model_ids() -> set[str]:
    """The set of model ids a regular user is permitted to select."""
    return {m["id"] for m in user_selectable_models()}
