"""Helpers for normalising arbitrary KB-document metadata.

The same normalisation runs on BOTH sides of the feature:

  * upload  — what the admin types becomes the sidecar `metadataAttributes`
  * chat    — what the user picks becomes the Bedrock retrieval `filter`

Because Bedrock `equals` is an exact string match, the key/value an admin
writes must be byte-identical to the key/value a user later filters on.
Centralising the rules here is what guarantees that. Keys are constrained
to a Bedrock-friendly charset (lowercased, ``[a-z0-9_]``); values are
trimmed but otherwise preserved.

Multi-value support: an admin may type comma-separated values in a single
value field (e.g. ``region = india, russia, USA``) and the normaliser
turns that into a list ``["india", "russia", "USA"]``. Each element then
becomes an indexable entry in the Bedrock STRING_LIST sidecar attribute,
and a chat-side ``equals`` filter on any one of them matches the document.
"""
from __future__ import annotations

import re

_KEY_RE = re.compile(r"[^a-z0-9_]+")

MAX_KEYS = 25            # guard against absurd payloads
MAX_KEY_LEN = 64
MAX_VALUE_LEN = 512
MAX_VALUES_PER_KEY = 50  # cap exploded list length per key


def normalize_key(key: str) -> str:
    """Lowercase, collapse illegal chars to '_', trim, clamp length."""
    k = _KEY_RE.sub("_", str(key or "").strip().lower()).strip("_")
    return k[:MAX_KEY_LEN]


def _clean_scalar(value) -> str:
    return str("" if value is None else value).strip()[:MAX_VALUE_LEN]


def normalize_value(value):
    """Normalise a value to either a single string or a list of strings.

    * list/tuple/set            → list (each element trimmed, comma-split too)
    * ``"india, russia, usa"``  → ``["india", "russia", "usa"]``
    * ``"india"``               → ``"india"``
    * empty in any form         → ``""``

    Lists are deduped (preserving first-seen order) and capped at
    ``MAX_VALUES_PER_KEY``. A list that collapses to a single element after
    dedupe is returned as a scalar so the simple, single-value case stays
    a plain string in the sidecar.
    """
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for v in value:
            for piece in str("" if v is None else v).split(","):
                p = piece.strip()[:MAX_VALUE_LEN]
                if p and p not in items:
                    items.append(p)
                    if len(items) >= MAX_VALUES_PER_KEY:
                        break
            if len(items) >= MAX_VALUES_PER_KEY:
                break
        if not items:
            return ""
        return items[0] if len(items) == 1 else items

    raw = _clean_scalar(value)
    if "," not in raw:
        return raw
    items: list[str] = []
    for piece in raw.split(","):
        p = piece.strip()[:MAX_VALUE_LEN]
        if p and p not in items:
            items.append(p)
            if len(items) >= MAX_VALUES_PER_KEY:
                break
    if not items:
        return ""
    return items[0] if len(items) == 1 else items


def normalize_metadata(meta: dict | None) -> dict[str, str | list[str]]:
    """Normalise an arbitrary {key: value} dict to a clean {key: value}.

    Each value is either a single trimmed string or a list of strings
    (the admin typed comma-separated input or the client sent an array).
    Drops empty keys/values, dedupes (last wins), and caps the number of
    entries so a malicious or buggy client can't attach hundreds.
    """
    out: dict[str, str | list[str]] = {}
    for k, v in (meta or {}).items():
        nk = normalize_key(k)
        if not nk:
            continue
        nv = normalize_value(v)
        if not nv:
            continue
        out[nk] = nv
        if len(out) >= MAX_KEYS:
            break
    return out


def normalize_filter_payload(payload) -> dict:
    """Normalise the chat-side metadata-filter payload to the canonical
    structured form used by the retrieval layer:

        {"operator": "AND" | "OR",
         "rules":    [{"key": str, "values": [str, ...]}, ...]}

    Two input shapes are accepted:

    1. **Legacy dict**  ``{key: value | [values], ...}``
       Each entry becomes one rule; rules are combined with ``AND``.

    2. **Structured**  ``{"operator": "...", "rules": [{key, values|value}, ...]}``
       The operator is normalised to ``"AND"`` or ``"OR"``; unknown
       values fall back to ``"AND"``. Each rule's values are normalised
       through ``normalize_value`` then forced to a list (a single value
       becomes a one-element list so the wire format is uniform).

    Empty or invalid entries are dropped. The result is always a valid
    structured filter, possibly with an empty ``rules`` list (which the
    retrieval layer treats as "no metadata constraint").
    """
    operator = "AND"
    raw_rules: list[tuple[str, object]] = []

    if isinstance(payload, dict) and isinstance(payload.get("rules"), list):
        # Structured shape.
        op_in = str(payload.get("operator") or "").strip().upper()
        operator = "OR" if op_in == "OR" else "AND"
        for r in payload["rules"]:
            if not isinstance(r, dict):
                continue
            k = r.get("key")
            # `values` (list/scalar) preferred, but accept `value` for
            # ergonomics on the wire.
            v = r.get("values", r.get("value"))
            raw_rules.append((k, v))
    elif isinstance(payload, dict):
        # Legacy flat dict → one rule per key, AND combined.
        for k, v in payload.items():
            raw_rules.append((k, v))

    rules: list[dict] = []
    seen_keys: set[str] = set()
    for k, v in raw_rules:
        nk = normalize_key(k)
        if not nk or nk in seen_keys:
            continue
        nv = normalize_value(v)
        if not nv:
            continue
        values = nv if isinstance(nv, list) else [nv]
        rules.append({"key": nk, "values": values})
        seen_keys.add(nk)
        if len(rules) >= MAX_KEYS:
            break

    return {"operator": operator, "rules": rules}
