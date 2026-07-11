from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncGenerator, List, Optional

import boto3
from botocore.config import Config as BotoConfig

from app.config import settings
from app.utils.logger import log_event

# ---------- boto3 clients (lazy, thread-safe) ----------

_boto_cfg = BotoConfig(
    region_name=settings.BEDROCK_REGION,
    retries={"max_attempts": 3, "mode": "standard"},
    read_timeout=60,
    connect_timeout=10,
)

_clients: dict[str, object] = {}
_client_credentials: dict[str, tuple] = {}


def _client(name: str):
    current_creds = (
        settings.AWS_ACCESS_KEY_ID,
        settings.AWS_SECRET_ACCESS_KEY,
        settings.AWS_SESSION_TOKEN,
    )
    if name not in _clients or _client_credentials.get(name) != current_creds:
        kwargs = {"config": _boto_cfg, "region_name": settings.BEDROCK_REGION}
        if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY and settings.AWS_SESSION_TOKEN:
            kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
            kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
            kwargs["aws_session_token"] = settings.AWS_SESSION_TOKEN
        _clients[name] = boto3.client(name, **kwargs)
        _client_credentials[name] = current_creds
    return _clients[name]


def bedrock_runtime():
    return _client("bedrock-runtime")


def bedrock_agent_runtime():
    return _client("bedrock-agent-runtime")


def bedrock_agent():
    return _client("bedrock-agent")


# ---------- Retrieval ----------

# Minimum vector-similarity score we'll trust as a "real" citation. Below
# this we keep the result for ranking only but the answer should warn /
# refuse rather than hallucinate. Tunable per deployment.
MIN_CITATION_SCORE = 0.20


def _normalize_citation(r: dict, dept_code: str | None = None) -> dict:
    """Map a Bedrock retrieval result into our internal citation shape."""
    loc = r.get("location", {}) or {}
    s3 = (loc.get("s3Location") or {}).get("uri")
    meta = r.get("metadata") or {}
    # Departments are stored on every metadata sidecar — prefer that, fall
    # back to inferring from the S3 key prefix.
    dept = (
        meta.get("department")
        or (s3.split("/")[3] if s3 and s3.startswith("s3://") and len(s3.split("/")) >= 4 else dept_code)
    )
    return {
        "title": meta.get("title")
        or meta.get("x-amz-bedrock-kb-source-uri")
        or (s3.split("/")[-1] if s3 else "Document"),
        "s3_uri": s3,
        "page": meta.get("x-amz-bedrock-kb-page-number") or meta.get("page"),
        "snippet": (r.get("content") or {}).get("text", ""),
        "score": r.get("score"),
        "department": dept,
    }


_HYBRID_STORES = {"opensearch", "opensearch-serverless", "aoss"}


def _resolve_search_type() -> Optional[str]:
    """
    Decide the `overrideSearchType` value for the KB retrieve call.

    Precedence:
      1. BEDROCK_KB_SEARCH_TYPE_OVERRIDE (if non-empty, used verbatim).
      2. Derived from BEDROCK_KB_VECTOR_STORE:
           - opensearch / aoss -> HYBRID
           - anything else (s3, pgvector, pinecone, ...) -> None,
             meaning we don't pass the field at all and Bedrock
             defaults to SEMANTIC.
    Returning None is important: S3 Vector buckets reject the field
    entirely, not just the value HYBRID.
    """
    override = (settings.BEDROCK_KB_SEARCH_TYPE_OVERRIDE or "").strip().upper()
    if override in ("HYBRID", "SEMANTIC"):
        return override
    store = (settings.BEDROCK_KB_VECTOR_STORE or "").strip().lower()
    if store in _HYBRID_STORES:
        return "HYBRID"
    return None


def _department_filter(department_codes: list[str]) -> dict:
    """Department dimension: single → `equals`, multi → `orAll`."""
    codes = [c.lower() for c in department_codes if c]
    if not codes:
        return {}
    if len(codes) == 1:
        return {"equals": {"key": "department", "value": codes[0]}}
    return {"orAll": [{"equals": {"key": "department", "value": c}} for c in codes]}


def _rule_clause(rule: dict) -> dict | None:
    """Turn one structured rule ``{"key", "values"}`` into a single Bedrock
    filter clause.

    * one value     → ``equals``
    * many values   → ``orAll([equals, …])`` (match ANY value for the key)

    Keys/values are used verbatim (they were normalised on the way in and
    must match exactly what ingestion wrote to the sidecar). Returns
    ``None`` for an empty/invalid rule so the caller can skip it.
    """
    key = str(rule.get("key") or "").strip()
    if not key:
        return None
    raw_vals = rule.get("values")
    if raw_vals is None:
        raw_vals = [rule.get("value")] if rule.get("value") is not None else []
    if not isinstance(raw_vals, (list, tuple, set)):
        raw_vals = [raw_vals]
    vals = [str(x).strip() for x in raw_vals if str(x).strip()]
    if not vals:
        return None
    if len(vals) == 1:
        return {"equals": {"key": key, "value": vals[0]}}
    return {"orAll": [{"equals": {"key": key, "value": x}} for x in vals]}


def _metadata_clauses_from_filter(metadata_filters) -> tuple[list[dict], str]:
    """Build the per-rule clauses + the rule-combination operator.

    Accepts both filter shapes:

    * Structured form ``{"operator": "AND"|"OR", "rules": [...]}`` —
      preferred, comes from ``normalize_filter_payload``.
    * Legacy flat-dict form ``{key: value | [values]}`` — one rule per
      key, AND combined (mirrors the old behaviour).

    Returns ``(clauses, operator)`` where ``operator`` is ``"AND"`` or
    ``"OR"``. ``clauses`` may be empty if nothing was selected.
    """
    if not metadata_filters:
        return [], "AND"

    if isinstance(metadata_filters, dict) and isinstance(metadata_filters.get("rules"), list):
        op = str(metadata_filters.get("operator") or "AND").upper()
        operator = "OR" if op == "OR" else "AND"
        rules = metadata_filters["rules"]
    elif isinstance(metadata_filters, dict):
        operator = "AND"
        rules = [{"key": k, "values": v if isinstance(v, list) else [v]}
                 for k, v in metadata_filters.items()]
    else:
        return [], "AND"

    clauses: list[dict] = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        c = _rule_clause(r)
        if c is not None:
            clauses.append(c)
    return clauses, operator


# Backward-compat alias used by other modules / tests that imported the
# previous helper name.
def _metadata_clauses(metadata_filters) -> list[dict]:
    clauses, _ = _metadata_clauses_from_filter(metadata_filters)
    return clauses


def _build_filter(department_codes: list[str], metadata_filters=None) -> dict:
    """Compose the Bedrock vectorSearchConfiguration filter.

    The department dimension is ALWAYS an AND constraint (a user can't
    OR-out of their authorised departments). Within metadata, rules are
    combined according to the user's chosen operator: AND requires every
    rule to match, OR requires any rule to match. Within a single rule,
    multiple values are an implicit OR (so the user can ask for
    region=india OR usa as one rule).
    """
    dept_f = _department_filter(department_codes)
    meta_clauses, meta_op = _metadata_clauses_from_filter(metadata_filters)

    if not meta_clauses:
        return dept_f or {}

    meta_combined = (
        meta_clauses[0]
        if len(meta_clauses) == 1
        else ({"orAll": meta_clauses} if meta_op == "OR" else {"andAll": meta_clauses})
    )

    if not dept_f:
        return meta_combined
    return {"andAll": [dept_f, meta_combined]}


async def retrieve_for_departments(
    query: str,
    department_codes: list[str],
    *,
    top_k: int | None = None,
    metadata_filters: dict | None = None,
) -> list[dict]:
    """
    Run a Bedrock KB retrieve call constrained to one or more departments
    and, optionally, any number of arbitrary metadata facets.

    Returns a list of citation dicts ordered by score (descending):
      [{title, s3_uri, page, snippet, score, department}, ...]

    The metadata filter is the *only* mechanism preventing cross-department
    (and cross-facet) leakage at the retrieval layer. Documents are tagged
    with `department: <code>` plus whatever custom metadata the admin
    attached at upload (see `s3_service.upload_kb_document`).
    """
    if not settings.BEDROCK_KB_ID:
        log_event("ai", "warning", "BEDROCK_KB_ID not set; returning empty retrieval")
        return []

    if not department_codes:
        log_event("ai", "warning", "retrieve called with no departments")
        return []

    num_results = top_k or settings.BEDROCK_KB_NUM_RESULTS
    # When spanning multiple departments, ask for proportionally more so
    # each dept has a fair chance to surface results before we re-rank.
    effective = num_results * max(1, len(department_codes))
    # If reranking is enabled, over-fetch up to RERANK_CANDIDATE_K so the
    # reranker has a richer candidate pool to choose from.
    rerank_floor = settings.RERANK_CANDIDATE_K if settings.RERANK_ENABLED else num_results
    effective = min(max(effective, rerank_floor), max(num_results, 50))

    vsc: dict = {"numberOfResults": effective}
    search_type = _resolve_search_type()
    if search_type:
        vsc["overrideSearchType"] = search_type
    flt = _build_filter(department_codes, metadata_filters)
    if flt:
        vsc["filter"] = flt

    def _call():
        return bedrock_agent_runtime().retrieve(
            knowledgeBaseId=settings.BEDROCK_KB_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": vsc},
        )

    t0 = time.monotonic()
    try:
        resp = await asyncio.to_thread(_call)
    except Exception as e:
        log_event("errors", "error", "Bedrock KB retrieve failed", error=str(e))
        return []

    latency_ms = int((time.monotonic() - t0) * 1000)

    # Normalise and pre-sort by KB score. We DO NOT drop low-score
    # results here — the prompt + caller logic interprets them via
    # MIN_CITATION_SCORE. This keeps the option open to display "I'm
    # not sure" with a weak source attached.
    citations = [
        _normalize_citation(r, department_codes[0] if len(department_codes) == 1 else None)
        for r in resp.get("retrievalResults", [])
    ]
    citations.sort(key=lambda c: (c.get("score") or 0.0), reverse=True)

    # Rerank the over-fetched candidate pool, then clip to requested top-k.
    # Import locally to avoid a circular import via app.services.rerank.
    from app.services.rerank import rerank_citations
    citations, rerank_backend = await rerank_citations(query, citations, num_results)
    
    # Pull rule keys + operator for the log line. Works for both the new
    # structured payload and the legacy flat-dict shape.
    if isinstance(metadata_filters, dict) and isinstance(metadata_filters.get("rules"), list):
        _keys = sorted({str(r.get("key") or "") for r in metadata_filters["rules"]
                        if isinstance(r, dict) and r.get("key")})
        _op = str(metadata_filters.get("operator") or "AND").upper()
    elif isinstance(metadata_filters, dict):
        _keys = sorted(metadata_filters.keys())
        _op = "AND"
    else:
        _keys, _op = [], "AND"
    log_event(
        "ai", "info", "KB retrieve",
        depts=",".join(department_codes),
        meta_filters=",".join(_keys) or "(none)",
        meta_op=_op,
        results=len(citations),
        latency_ms=latency_ms,
        search_type=search_type or "SEMANTIC",
        store=settings.BEDROCK_KB_VECTOR_STORE,
        rerank=rerank_backend,
    )
    return citations


# Backward-compat alias — keep the single-dept signature working for any
# downstream caller / future migration.
async def retrieve_for_department(query: str, department_code: str) -> list[dict]:
    return await retrieve_for_departments(query, [department_code])


def average_top_score(citations: list[dict], k: int = 5) -> float:
    """Mean score over the top-k citations, used for confidence shaping."""
    if not citations:
        return 0.0
    scores = [c.get("score") or 0.0 for c in citations[:k]]
    return sum(scores) / max(1, len(scores))


def _build_context_block(citations: list[dict]) -> str:
    """Concatenate retrieved snippets into the prompt context window.

    Each excerpt is labelled with its source department so a multi-dept
    answer can faithfully attribute each fact to the right dept.
    """
    parts: list[str] = []
    for i, c in enumerate(citations, 1):
        dept = c.get("department") or "unknown"
        page = c.get("page") or "?"
        title = c.get("title") or "Document"
        snippet = (c.get("snippet") or "").strip()
        parts.append(
            f"[Doc {i} · dept={dept} · {title} · p.{page}]\n{snippet}"
        )
    return "\n\n".join(parts)


# ---------- Generation (streaming) ----------

async def chat_stream(
    *,
    system_prompt: str,
    history: list[dict],
    user_query: str,
    model_id: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> AsyncGenerator[dict, None]:
    """
    Stream a response from a Bedrock chat model via the **Converse API**.

    Converse is model-agnostic, so the SAME code path streams Anthropic
    Claude, Amazon Nova, Meta Llama, etc. — which is what lets the admin
    model-switcher work without per-provider request bodies.

    Yields events of shape:
      {"type": "delta", "text": "..."}
      {"type": "usage", "input": N, "output": M}
      {"type": "done"}
    """
    model_id = model_id or settings.BEDROCK_MODEL_ID
    temperature = temperature if temperature is not None else settings.BEDROCK_TEMPERATURE
    max_tokens = max_tokens or settings.BEDROCK_MAX_TOKENS

    # Converse message format: content is a list of blocks.
    #
    # Bedrock Converse requires:
    #   (a) the first message is `user`
    #   (b) roles strictly alternate (no two user or two assistant in a row)
    # Two ways this can break in the wild:
    #   1. The in-chat "delete message" feature lets a user remove the
    #      opening user turn — history would then start with `assistant`.
    #   2. The "last N turns" slice can cut in the middle of a user/asst
    #      pair (e.g. for [u1,a1,u2,a2] the last 3 is [a1,u2,a2] which
    #      starts with assistant).
    # Sanitise here so every caller is protected.

    raw_history = history[-6:]  # wider window so the slice still leaves 3
    # Collapse runs of the same role: keep the most recent message of each
    # run so we never emit user/user or assistant/assistant pairs.
    collapsed: list[dict] = []
    for m in raw_history:
        role = "user" if m.get("role") == "user" else "assistant"
        text = m.get("content") or ""
        if not text:
            continue
        if collapsed and collapsed[-1]["role"] == role:
            collapsed[-1]["content"][0]["text"] = text
        else:
            collapsed.append({"role": role, "content": [{"text": text}]})
    # Cap context to last 3 turns (same context budget as before).
    messages = collapsed[-3:]
    # The slice can have left an orphaned leading `assistant` turn — drop
    # any leading non-user message so Converse's "must start with user"
    # rule holds. Mutate `messages` in place.
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    # If the last sanitised turn is `user`, appending another `user` for the
    # new query would break alternation — drop that trailing user (its
    # content is effectively superseded by the new query).
    if messages and messages[-1]["role"] == "user":
        messages.pop()
    messages.append({"role": "user", "content": [{"text": user_query}]})

    kwargs: dict = dict(
        modelId=model_id,
        messages=messages,
        inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
    )
    if system_prompt:
        kwargs["system"] = [{"text": system_prompt}]
    if settings.BEDROCK_GUARDRAIL_ID:
        kwargs["guardrailConfig"] = {
            "guardrailIdentifier": settings.BEDROCK_GUARDRAIL_ID,
            "guardrailVersion": settings.BEDROCK_GUARDRAIL_VERSION,
        }

    def _invoke():
        return bedrock_runtime().converse_stream(**kwargs)

    try:
        resp = await asyncio.to_thread(_invoke)
    except Exception as e:
        log_event("errors", "error", "Bedrock converse failed", error=str(e), model=model_id)
        yield {"type": "delta", "text": f"\n[error] {e}"}
        yield {"type": "done"}
        return

    stream = resp.get("stream")
    input_tokens = 0
    output_tokens = 0

    try:
        for event in stream:
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {}) or {}
                text = delta.get("text")
                if text:
                    yield {"type": "delta", "text": text}
                    # cooperative scheduling so other coroutines can run
                    await asyncio.sleep(0)
            elif "metadata" in event:
                usage = event["metadata"].get("usage", {}) or {}
                input_tokens = usage.get("inputTokens", input_tokens)
                output_tokens = usage.get("outputTokens", output_tokens)
            elif "messageStop" in event:
                # End of turn; usage arrives in the trailing metadata event.
                continue
    except Exception as e:
        log_event("errors", "error", "Bedrock converse stream error",
                  error=str(e), model=model_id)
        yield {"type": "delta", "text": f"\n[error] {e}"}

    yield {"type": "usage", "input": input_tokens, "output": output_tokens}
    yield {"type": "done"}


# ---------- KB ingestion trigger ----------

async def start_kb_ingestion() -> Optional[str]:
    """Trigger a KB data-source sync after S3 uploads. Returns job id."""
    if not (settings.BEDROCK_KB_ID and settings.BEDROCK_KB_DATA_SOURCE_ID):
        log_event("admin", "warning", "KB ingestion skipped: KB or DS id missing")
        return None

    def _call():
        return bedrock_agent().start_ingestion_job(
            knowledgeBaseId=settings.BEDROCK_KB_ID,
            dataSourceId=settings.BEDROCK_KB_DATA_SOURCE_ID,
            description="Triggered by Company AI admin upload",
        )

    try:
        resp = await asyncio.to_thread(_call)
        job_id = (resp.get("ingestionJob") or {}).get("ingestionJobId")
        log_event("admin", "info", "KB ingestion started", job_id=job_id)
        return job_id
    except Exception as e:
        log_event("errors", "error", "KB ingestion failed", error=str(e))
        return None


def build_context_block(citations: list[dict]) -> str:
    """Exposed for routers / tests."""
    return _build_context_block(citations)
