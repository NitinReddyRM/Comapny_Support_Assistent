"""
Chat endpoints — both REST (one-shot) and WebSocket (streaming).

Pipeline (identical for both):
  1. Rate-limit the user.
  2. Rule engine — instant deterministic reply for greetings / FAQs.
  3. Input guardrails (local heuristics).
  4. Cache lookup.
  5. KB retrieval, scoped to the session's authorised department(s).
  6. Build system prompt + multi-turn history (single- or multi-dept).
  7. Bedrock generation (streaming).
  8. Output guardrails.
  9. Persist user + assistant messages, citations, tokens, latency.
 10. Return suggestions / follow-ups.

Every persisted assistant message records the *response source*:
  - "rule_engine" — answered by the deterministic rule layer
  - "kb"          — answered by the LLM with KB excerpts
  - "llm"         — answered by the LLM without any KB context (fallback)
"""
from __future__ import annotations

import json
import re
import time
from typing import List

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_active_department_codes,
    get_current_department,
    get_current_user,
)
from app.config import settings
from app.core.exceptions import GuardrailBlocked
from app.core.rate_limit import limiter
from app.core.security import decode_token, JWTError
from app.database import AsyncSessionLocal, get_db
from app.core.exceptions import ForbiddenError
from app.core.metadata import normalize_filter_payload, normalize_metadata
from app.core.model_catalog import available_models
from app.services.app_settings import (
    SCOPE_PRIVILEGED, SCOPE_STANDARD, get_active_model, set_active_model,
    get_user_models,
)
from app.models.chat import ChatMessage, ChatSession, MessageRole
from app.models.department import Department
from app.models.kb_document import KBDocument, KBDocumentStatus
from app.models.user import User, UserRole
from app.prompts.system_prompt import build_multi_dept_system_prompt, build_system_prompt
from app.schemas.chat import (
    ChatDiagnostics, ChatMessageOut, ChatRequest, ChatResponse,
    ChatSessionOut, ChatTraceStep, Citation, ModelSelect, SuggestionRequest,
    SuggestionResponse,
)
from app.services import bedrock_service
from app.services.cache import cache
from app.services.diagnostics import (
    Trace, answer_grounding_overlap, compute_hallucination_pct,
)
from app.services.guardrails import check_input, check_output
from app.services.rule_engine import match as rule_match
from app.services.suggestions import autocomplete, derive_follow_ups, get_seed_prompts
from app.services.usage import enforce_monthly_budget
from app.utils.logger import log_event

router = APIRouter(prefix="/chat", tags=["chat"])


# ---------- helpers ----------


def _strip_tail_json(text: str) -> tuple[str, dict]:
    # Case 1: Proper ```json block with closing ```
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if m:
        try:
            return text[:m.start()].rstrip(), json.loads(m.group(1))
        except Exception:
            pass

    # Case 2: ```json block WITHOUT closing ```
    m2 = re.search(r"```json\s*(\{.*?\})\s*$", text, flags=re.DOTALL)
    if m2:
        try:
            return text[:m2.start()].rstrip(), json.loads(m2.group(1))
        except Exception:
            pass

    # Case 3: raw JSON at end
    m3 = re.search(r"(\{.*\})\s*$", text, flags=re.DOTALL)
    if m3:
        try:
            return text[:m3.start()].rstrip(), json.loads(m3.group(1))
        except Exception:
            pass

    return text, {}



async def _load_or_create_session(
    db: AsyncSession, user: User, dept: Department, session_id: int | None
) -> ChatSession:
    if session_id is not None:
        res = await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id, ChatSession.user_id == user.id
            )
        )
        sess = res.scalar_one_or_none()
        if sess:
            if sess.department_id != dept.id:
                # Strict isolation — refuse mixing departments in one thread.
                raise GuardrailBlocked("Session belongs to a different department")
            return sess
    sess = ChatSession(user_id=user.id, department_id=dept.id, title="New Conversation")
    db.add(sess)
    await db.commit()
    await db.refresh(sess)
    return sess


async def _recent_history(db: AsyncSession, session_id: int, limit: int = 10) -> list[dict]:
    res = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(desc(ChatMessage.id))
        .limit(limit)
    )
    msgs = list(reversed(res.scalars().all()))
    return [{"role": m.role.value, "content": m.content} for m in msgs]


async def _dept_names(db: AsyncSession, codes: list[str]) -> list[str]:
    """Resolve dept codes to human-friendly names for the system prompt."""
    if not codes:
        return []
    res = await db.execute(
        select(Department.code, Department.name).where(Department.code.in_(codes))
    )
    by_code = {c: n for c, n in res.all()}
    return [by_code.get(c, c) for c in codes]


def _build_prompt(
    dept_codes: list[str], dept_names: list[str], context_block: str
) -> str:
    if len(dept_codes) == 1:
        return build_system_prompt(dept_names[0] if dept_names else dept_codes[0], context_block)
    return build_multi_dept_system_prompt(dept_names or dept_codes, context_block)


def _shape_confidence(
    model_conf: float,
    citations: list[dict],
    retrieval_score: float | None = None,
) -> float:
    """Blend the model's self-reported confidence with the retrieval score.

    The model tends to over-confidence; we cap it at the average top-3
    retrieval score plus a small floor so weak retrievals can't claim
    high confidence regardless of what the model says.

    ``retrieval_score`` is an opt-in pass-through so callers that already
    paid the cost of ``average_top_score`` (e.g. for diagnostics) don't
    recompute it.
    """
    retrieval = (
        retrieval_score
        if retrieval_score is not None
        else bedrock_service.average_top_score(citations, k=3)
    )
    if not citations:
        return min(model_conf, 0.3)
    blended = 0.7 * model_conf + 0.3 * retrieval
    return max(0.0, min(1.0, blended))


def _clean_filters(metadata_filters: dict | None) -> dict:
    """Normalise the user's metadata-filter selection into the structured
    rule form used by the retrieval layer:

        {"operator": "AND" | "OR",
         "rules":    [{"key": str, "values": [str, ...]}, ...]}

    Accepts either the legacy flat-dict shape or the new structured one
    (see ``normalize_filter_payload``). Empty selection yields a payload
    with no rules, which the retrieval layer treats as "no metadata
    constraint" (search the whole department)."""
    return normalize_filter_payload(metadata_filters)


def _filter_keys(meta_filters: dict | None) -> list[str]:
    """Sorted list of rule keys, used only for tracing/logging."""
    rules = (meta_filters or {}).get("rules") or []
    return sorted({str(r.get("key") or "") for r in rules if isinstance(r, dict) and r.get("key")})


def _diagnostics_enabled(user: User) -> bool:
    """Diagnostics (hallucination % + LangGraph-style trace) are visible
    only to SUPERADMIN today. The hook is one place so it's easy to widen
    later (e.g. roll out to CROSSADMIN, then ADMIN, then everyone)."""
    return user.role == UserRole.SUPERADMIN


def _build_diagnostics(
    trace: Trace,
    *,
    model_confidence: float,
    citations: list[dict],
    confidence: float,
    answer_text: str = "",
    retrieval_score: float | None = None,
) -> ChatDiagnostics:
    """Snapshot the trace + grounding signals into a response payload.

    Accepts a precomputed ``retrieval_score`` so callers can avoid running
    ``average_top_score`` twice (once here, once in ``_shape_confidence``).
    When ``answer_text`` is provided alongside citations, the cheap
    lexical-overlap signal sharpens the hallucination estimate at near-
    zero CPU cost — no LLM-as-judge, no embeddings.
    """
    if retrieval_score is None:
        retrieval_score = (
            bedrock_service.average_top_score(citations, k=3) if citations else 0.0
        )
    answer_overlap: float | None = None
    if answer_text and citations:
        answer_overlap = answer_grounding_overlap(
            answer_text,
            (c.get("text") or c.get("excerpt") or "" for c in citations),
        )
    return ChatDiagnostics(
        hallucination_pct=compute_hallucination_pct(
            model_confidence=model_confidence,
            retrieval_score=retrieval_score,
            has_citations=bool(citations),
            answer_overlap=answer_overlap,
            answer_text=answer_text or None,
        ),
        model_confidence=round(float(model_confidence or 0.0), 3),
        retrieval_score=round(float(retrieval_score or 0.0), 3),
        groundedness=round(float(confidence or 0.0), 3),
        trace=[ChatTraceStep(**s) for s in trace.to_list()],
    )


async def _facets_for_departments(db: AsyncSession, dept_codes: list[str]) -> dict[str, list[str]]:
    """Distinct metadata {key: [values]} across ACTIVE KB docs in the
    given departments. Powers the chat filter UI."""
    codes = [c.lower() for c in dept_codes if c]
    if not codes:
        return {}
    rows = (await db.execute(
        select(KBDocument.doc_metadata)
        .join(Department, Department.id == KBDocument.department_id)
        .where(
            Department.code.in_(codes),
            KBDocument.status == KBDocumentStatus.ACTIVE,
        )
    )).scalars().all()
    facets: dict[str, set] = {}
    for md in rows:
        if not isinstance(md, dict):
            continue
        for k, v in md.items():
            key = str(k).strip()
            if not key or v is None:
                continue
            # Multi-value tags arrive as a list (from normalize_metadata) or,
            # for legacy rows written before list support, a comma-joined
            # string. Explode either form so each value is its own facet.
            if isinstance(v, (list, tuple, set)):
                vals = [str(x).strip() for x in v]
            else:
                vals = [p.strip() for p in str(v).split(",")]
            for val in vals:
                if val:
                    facets.setdefault(key, set()).add(val)
    return {k: sorted(vs) for k, vs in sorted(facets.items())}


@router.get("/facets")
async def chat_facets(
    user: User = Depends(get_current_user),
    dept_codes: list[str] = Depends(get_active_department_codes),
    db: AsyncSession = Depends(get_db),
):
    """Metadata facets the chat UI offers as filters, scoped to the
    departments the caller may query. Shape: {"facets": {key: [values]}}.
    """
    return {"facets": await _facets_for_departments(db, dept_codes)}


_MODEL_SWITCH_ROLES = (UserRole.CROSSADMIN, UserRole.SUPERADMIN)


def _scope_for_role(role: UserRole) -> str:
    """CROSSADMIN / SUPERADMIN run on the 'privileged' model (which they
    set from the chat window); everyone else runs on the 'standard' model
    (set from the admin portal)."""
    return SCOPE_PRIVILEGED if role in _MODEL_SWITCH_ROLES else SCOPE_STANDARD


@router.get("/models")
async def chat_models(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Catalog + the model THIS caller runs on (role-scoped).

    For CROSSADMIN / SUPERADMIN `active` is their own (privileged) model
    and `can_switch` is true — the chat-window picker sets that model.
    For regular users, they get the 2 user-selectable models and can
    pick between them; their `preferred_model` is returned as `active`.
    """
    if user.role in _MODEL_SWITCH_ROLES:
        active = await get_active_model(db, SCOPE_PRIVILEGED)
        models = available_models()
        return {"models": models, "active": active, "can_switch": True}

    user_model_ids = set(await get_user_models(db))
    std_active = await get_active_model(db, SCOPE_STANDARD)
    if user_model_ids:
        all_m = available_models()
        user_models = [m for m in all_m if m["id"] in user_model_ids]
        active = (user.preferred_model
                  if (user.preferred_model and user.preferred_model in user_model_ids)
                  else std_active)
        return {"models": user_models, "active": active, "can_switch": True, "scope": "user"}
    label = next((m["label"] for m in available_models() if m["id"] == std_active), std_active)
    return {"models": [{"id": std_active, "label": label, "default": True}], "active": std_active, "can_switch": False}


@router.post("/models/active")
async def set_chat_model(
    payload: ModelSelect,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set the caller's active model.

    CROSSADMIN / SUPERADMIN set the shared privileged-scope model.
    Regular users set their own per-user preference (from the 2
    user-selectable models configured in USER_SELECTABLE_MODELS).
    """
    if user.role in _MODEL_SWITCH_ROLES:
        try:
            active = await set_active_model(db, SCOPE_PRIVILEGED, payload.model_id)
        except ValueError as e:
            raise GuardrailBlocked(str(e))
        log_event("admin", "info", "privileged model changed", model=active, by=user.email)
        return {"active": active}

    allowed = set(await get_user_models(db))
    if not allowed:
        raise ForbiddenError("No user-accessible models have been configured by admin")
    if payload.model_id not in allowed:
        raise GuardrailBlocked(f"Model '{payload.model_id}' is not available for users")
    user.preferred_model = payload.model_id
    await db.commit()
    log_event("chat", "info", "user model preference set", model=payload.model_id, by=user.email)
    return {"active": payload.model_id}


# ---------- REST one-shot ----------

@router.post("/query", response_model=ChatResponse)
async def chat_query(
    payload: ChatRequest,
    user: User = Depends(get_current_user),
    dept: Department = Depends(get_current_department),
    dept_codes: list[str] = Depends(get_active_department_codes),
    db: AsyncSession = Depends(get_db),
):
    await limiter.check(f"chat:{user.id}", limit=settings.RATE_LIMIT_PER_MINUTE)
    # Hard per-user monthly token budget — short-circuits before any
    # Bedrock cost is incurred. Rule-engine replies cost 0 tokens, so
    # we *only* check on the LLM path below (after rule_match misses).
    t0 = time.monotonic()
    query = payload.query.strip()
    # Arbitrary metadata facets selected by the user in the chat UI.
    meta_filters = _clean_filters(payload.metadata_filters)
    # Model resolution: privileged roles use their scope model; regular users
    # use their preferred_model (if set and in the admin-allowed set) or the
    # admin-set standard model.
    if user.role in _MODEL_SWITCH_ROLES:
        model_id = await get_active_model(db, SCOPE_PRIVILEGED)
    else:
        _user_model_ids = set(await get_user_models(db))
        if user.preferred_model and user.preferred_model in _user_model_ids:
            model_id = user.preferred_model
        else:
            model_id = await get_active_model(db, SCOPE_STANDARD)
    # Pipeline trace + per-step timings — surfaced to SUPERADMIN only.
    trace = Trace()
    is_priv = _diagnostics_enabled(user)

    # 1. Rule engine — instant reply for greetings / FAQs / identity.
    rule = rule_match(query)
    if rule is not None:
        trace.record("rule_engine", "ok", intent=rule.intent, confidence=rule.confidence)
        sess = await _load_or_create_session(db, user, dept, payload.session_id)

        # Persist user + assistant messages so transcripts stay complete.
        user_msg = ChatMessage(session_id=sess.id, role=MessageRole.USER, content=query)
        db.add(user_msg)
        await db.flush()

        asst_msg = ChatMessage(
            session_id=sess.id, role=MessageRole.ASSISTANT, content=rule.answer,
            citations={"items": [], "source": "rule_engine", "intent": rule.intent},
            confidence=rule.confidence,
            tokens_input=0, tokens_output=0,
            latency_ms=int((time.monotonic() - t0) * 1000),
            model_id="rule_engine",
            blocked_by_guardrail=False,
        )
        db.add(asst_msg)
        if sess.title == "New Conversation":
            sess.title = (query[:60] + "…") if len(query) > 60 else query
        await db.commit()
        await db.refresh(asst_msg)

        log_event("chatbot", "info", "rule reply",
                  email=user.email, dept=dept.code, intent=rule.intent,
                  source="rule_engine", hallucination_pct=0)

        return ChatResponse(
            session_id=sess.id, message_id=asst_msg.id, answer=rule.answer,
            citations=[], confidence=rule.confidence,
            suggestions=rule.suggestions[:4],
            related=get_seed_prompts(dept.code, 3),
            latency_ms=int((time.monotonic() - t0) * 1000),
            tokens_input=0, tokens_output=0,
            blocked=False, source="rule_engine",
            diagnostics=(_build_diagnostics(
                trace, model_confidence=rule.confidence, citations=[],
                confidence=rule.confidence,
            ) if is_priv else None),
        )
    else:
        trace.record("rule_engine", "skipped")

    # 2. Input guardrails
    gi = check_input(query)
    trace.record(
        "guardrail_in", "blocked" if not gi.allowed else "ok",
        reasons=list(gi.reasons), redacted=(gi.redacted_text != query),
    )
    if not gi.allowed:
        log_event("guardrail", "warning", "Input blocked",
                  email=user.email, dept=dept.code, reasons=gi.reasons)
        # Persist the block as a chat message so the admin's
        # "Guardrail blocks" modal surfaces input-side blocks too.
        # Without this row, only output-side blocks ever appeared,
        # making the admin think nothing was being caught.
        sess = await _load_or_create_session(db, user, dept, payload.session_id)
        user_msg = ChatMessage(session_id=sess.id, role=MessageRole.USER, content=query)
        db.add(user_msg)
        await db.flush()
        db.add(ChatMessage(
            session_id=sess.id, role=MessageRole.ASSISTANT,
            content="(blocked by input guardrails)",
            citations={"items": [], "source": "guardrail", "depts": dept_codes},
            confidence=0.0,
            tokens_input=0, tokens_output=0,
            latency_ms=int((time.monotonic() - t0) * 1000),
            model_id="guardrail",
            blocked_by_guardrail=True,
            block_reasons={"stage": "input", "reasons": list(gi.reasons)},
        ))
        await db.commit()
        raise GuardrailBlocked("; ".join(gi.reasons))

    # 3. Cache (keyed by the *sorted* dept set, the selected metadata
    #    facets, AND the model so different combinations don't collide).
    filter_sig = ";".join(f"{k}={v}" for k, v in sorted(meta_filters.items())) or "all"
    cache_dept = ("+".join(sorted(dept_codes)) or dept.code) + "::" + filter_sig + "::" + model_id
    cached = await cache.get(cache_dept, query)
    if cached:
        trace.record("cache", "hit", key=cache_dept)
        log_event("performance", "info", "cache hit", email=user.email, dept=cache_dept)
        cached["session_id"] = cached.get("session_id") or 0
        # The cached payload was built for whatever caller warmed it; for a
        # SUPERADMIN we still want to show *something*, so we synthesise a
        # tiny diagnostics object from the cached confidence + the cache-hit
        # trace. The expensive signals (retrieval_score, model_confidence)
        # are unavailable here — they're 0 by design.
        if is_priv:
            cached_resp = ChatResponse(**cached)
            cached_resp.diagnostics = _build_diagnostics(
                trace, model_confidence=cached_resp.confidence,
                citations=[c.model_dump() for c in cached_resp.citations],
                confidence=cached_resp.confidence,
            )
            return cached_resp
        return ChatResponse(**cached)
    trace.record("cache", "miss", key=cache_dept)

    # 3b. Monthly token budget — enforced only on the paid LLM path.
    # Rule-engine + cache hits cost 0 tokens and already returned above.
    await enforce_monthly_budget(db, user)

    # 4. Session + history (anchored to the *active* dept)
    sess = await _load_or_create_session(db, user, dept, payload.session_id)
    history = await _recent_history(db, sess.id)

    # 5. KB retrieval — span all authorised departments for the session,
    #    narrowed to the selected metadata facets (andAll of equals).
    with trace.timer("retrieval") as _t_ret:
        citations = await bedrock_service.retrieve_for_departments(
            gi.redacted_text, dept_codes, metadata_filters=meta_filters)
        _ret_top = bedrock_service.average_top_score(citations, k=3) if citations else 0.0
        _t_ret.note(
            citations_count=len(citations),
            top_score=round(float(_ret_top), 3),
            depts=dept_codes,
            filters=_filter_keys(meta_filters),
            filter_op=(meta_filters or {}).get("operator", "AND"),
        )
    context_block = bedrock_service.build_context_block(citations)
    dept_names = await _dept_names(db, dept_codes)
    system_prompt = _build_prompt(dept_codes, dept_names, context_block)

    # 6. Generation
    with trace.timer("generation") as _t_gen:
        answer_chunks: list[str] = []
        input_tokens = output_tokens = 0
        async for ev in bedrock_service.chat_stream(
            system_prompt=system_prompt, history=history, user_query=gi.redacted_text,
            model_id=model_id,
        ):
            if ev["type"] == "delta":
                answer_chunks.append(ev["text"])
            elif ev["type"] == "usage":
                input_tokens = ev.get("input", 0)
                output_tokens = ev.get("output", 0)
        _t_gen.note(
            tokens_in=int(input_tokens), tokens_out=int(output_tokens),
            model_id=model_id,
        )

    raw = "".join(answer_chunks)
    clean, tail = _strip_tail_json(raw)
    model_conf = float(tail.get("confidence", 0.5))
    print("$"*80)
    print("citations",citations)
    print("$"*80)
    print("$"*80)
    print("_ret_top",_ret_top)
    print("$"*80)
    confidence = _shape_confidence(model_conf, citations, retrieval_score=_ret_top)
    suggestions = list(tail.get("suggestions") or derive_follow_ups(raw, dept.code))

    # 7. Output guardrails
    go = check_output(clean, dept.code)
    trace.record(
        "guardrail_out", "blocked" if not go.allowed else "ok",
        reasons=list(go.reasons), redacted=(go.redacted_text != clean),
    )
    if not go.allowed:
        log_event("guardrail", "warning", "Output blocked",
                  email=user.email, dept=dept.code, reasons=go.reasons)
        clean = "I cannot share that information."
        confidence = 0.0
    else:
        clean = go.redacted_text

    latency_ms = int((time.monotonic() - t0) * 1000)
    response_source = "kb" if citations else "llm"

    # 8. Persist
    user_msg = ChatMessage(session_id=sess.id, role=MessageRole.USER, content=query)
    db.add(user_msg)
    await db.flush()

    asst_msg = ChatMessage(
        session_id=sess.id, role=MessageRole.ASSISTANT, content=clean,
        citations={"items": citations, "source": response_source,
                   "depts": dept_codes},
        confidence=confidence,
        tokens_input=input_tokens, tokens_output=output_tokens,
        latency_ms=latency_ms, model_id=model_id,
        blocked_by_guardrail=not go.allowed,
        block_reasons=({"stage": "output", "reasons": list(go.reasons)}
                       if not go.allowed else None),
    )
    db.add(asst_msg)

    if sess.title == "New Conversation":
        sess.title = (query[:60] + "…") if len(query) > 60 else query

    await db.commit()
    await db.refresh(asst_msg)

    diagnostics = _build_diagnostics(
        trace, model_confidence=model_conf, citations=citations, confidence=confidence,
        answer_text=clean, retrieval_score=_ret_top,
    )

    resp = ChatResponse(
        session_id=sess.id, message_id=asst_msg.id, answer=clean,
        citations=[Citation(**c) for c in citations[:6]],
        confidence=confidence, suggestions=suggestions[:4],
        related=get_seed_prompts(dept.code, 3),
        latency_ms=latency_ms, tokens_input=input_tokens, tokens_output=output_tokens,
        blocked=not go.allowed,
        blocked_reason="; ".join(go.reasons) if not go.allowed else None,
        source=response_source,
        diagnostics=diagnostics if is_priv else None,
    )

    if not resp.blocked:
        # Cache the *non-privileged* shape so non-superadmins served from
        # cache never accidentally receive someone else's diagnostics.
        cached_payload = resp.model_dump()
        cached_payload["diagnostics"] = None
        await cache.set(cache_dept, query, cached_payload)

    log_event("chatbot", "info", "chat",
              email=user.email, dept=dept.code, depts=dept_codes,
              session_id=sess.id, latency_ms=latency_ms,
              tokens_in=input_tokens, tokens_out=output_tokens,
              confidence=confidence, source=response_source,
              hallucination_pct=diagnostics.hallucination_pct)
    return resp


# ---------- WebSocket streaming ----------

@router.websocket("/ws")
async def chat_ws(websocket: WebSocket):
    """
    Streaming chat. Client connects with ?token=<jwt> and sends JSON
    messages: {"type":"query","query":"...", "session_id": int|null}.
    Server emits a stream of {"type":"delta|done|meta|error|rule", ...}.
    """
    await websocket.accept()
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        payload = decode_token(token)
    except JWTError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    email = payload.get("sub")
    dept_code = payload.get("dept")
    depts_claim = payload.get("depts")
    if not (email and dept_code):
        await websocket.send_json({"type": "error", "message": "Department not selected"})
        await websocket.close()
        return

    session_depts: list[str] = (
        [str(c) for c in depts_claim if c] if isinstance(depts_claim, list) and depts_claim
        else [dept_code]
    )

    async with AsyncSessionLocal() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        dept = (await db.execute(select(Department).where(Department.code == dept_code))).scalar_one_or_none()
        if not user or not dept:
            await websocket.send_json({"type": "error", "message": "Auth context invalid"})
            await websocket.close()
            return

        # Filter to currently-active dept codes (server-side; the client's
        # JWT might reference a dept that's been deactivated since login).
        active_rows = await db.execute(
            select(Department.code).where(
                Department.code.in_(session_depts),
                Department.is_active.is_(True),
            )
        )
        session_depts = [r for r in active_rows.scalars().all()] or [dept.code]

        try:
            while True:
                msg = await websocket.receive_json()
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue
                if msg.get("type") != "query":
                    continue

                query = (msg.get("query") or "").strip()
                session_id = msg.get("session_id")
                meta_filters = _clean_filters(msg.get("metadata_filters"))
                if user.role in _MODEL_SWITCH_ROLES:
                    model_id = await get_active_model(db, SCOPE_PRIVILEGED)
                else:
                    _ws_user_ids = set(await get_user_models(db))
                    if user.preferred_model and user.preferred_model in _ws_user_ids:
                        model_id = user.preferred_model
                    else:
                        model_id = await get_active_model(db, SCOPE_STANDARD)
                if not query:
                    continue

                try:
                    await limiter.check(f"chat:{user.id}", limit=settings.RATE_LIMIT_PER_MINUTE)
                except Exception:
                    await websocket.send_json({"type": "error", "message": "Rate limited"})
                    continue

                t0 = time.monotonic()
                # Fresh trace per WS query — same shape as the REST path.
                trace = Trace()
                is_priv = _diagnostics_enabled(user)

                # Rule engine fast-path
                rule = rule_match(query)
                if rule is not None:
                    trace.record("rule_engine", "ok", intent=rule.intent, confidence=rule.confidence)
                    sess = await _load_or_create_session(db, user, dept, session_id)
                    user_msg = ChatMessage(session_id=sess.id, role=MessageRole.USER, content=query)
                    db.add(user_msg)
                    await db.flush()
                    asst_msg = ChatMessage(
                        session_id=sess.id, role=MessageRole.ASSISTANT, content=rule.answer,
                        citations={"items": [], "source": "rule_engine", "intent": rule.intent},
                        confidence=rule.confidence,
                        tokens_input=0, tokens_output=0,
                        latency_ms=int((time.monotonic() - t0) * 1000),
                        model_id="rule_engine",
                        blocked_by_guardrail=False,
                    )
                    db.add(asst_msg)
                    if sess.title == "New Conversation":
                        sess.title = (query[:60] + "…") if len(query) > 60 else query
                    await db.commit()
                    await db.refresh(asst_msg)

                    rule_diag = _build_diagnostics(
                        trace, model_confidence=rule.confidence, citations=[],
                        confidence=rule.confidence,
                    )
                    await websocket.send_json({
                        "type": "rule",
                        "session_id": sess.id,
                        "message_id": asst_msg.id,
                        "answer": rule.answer,
                        "confidence": rule.confidence,
                        "suggestions": rule.suggestions[:4],
                        "latency_ms": int((time.monotonic() - t0) * 1000),
                        "source": "rule_engine",
                        "intent": rule.intent,
                        "diagnostics": rule_diag.model_dump() if is_priv else None,
                    })
                    log_event("chatbot", "info", "ws rule reply",
                              email=email, intent=rule.intent, source="rule_engine",
                              hallucination_pct=0)
                    continue
                else:
                    trace.record("rule_engine", "skipped")

                gi = check_input(query)
                trace.record(
                    "guardrail_in", "blocked" if not gi.allowed else "ok",
                    reasons=list(gi.reasons), redacted=(gi.redacted_text != query),
                )
                if not gi.allowed:
                    log_event("guardrail", "warning", "WS input blocked",
                              email=email, dept=dept_code, reasons=gi.reasons)
                    # Persist so the admin "Guardrail blocks" modal sees
                    # WS-path input blocks. Without this the events list
                    # never grew because only output-stage blocks wrote
                    # a row.
                    try:
                        sess = await _load_or_create_session(db, user, dept, session_id)
                        user_msg = ChatMessage(session_id=sess.id, role=MessageRole.USER, content=query)
                        db.add(user_msg)
                        await db.flush()
                        db.add(ChatMessage(
                            session_id=sess.id, role=MessageRole.ASSISTANT,
                            content="(blocked by input guardrails)",
                            citations={"items": [], "source": "guardrail",
                                       "depts": session_depts},
                            confidence=0.0,
                            tokens_input=0, tokens_output=0,
                            latency_ms=int((time.monotonic() - t0) * 1000),
                            model_id="guardrail",
                            blocked_by_guardrail=True,
                            block_reasons={"stage": "input",
                                           "reasons": list(gi.reasons)},
                        ))
                        await db.commit()
                    except Exception as persist_err:
                        log_event("errors", "error",
                                  "failed to persist WS input block",
                                  error=str(persist_err))
                    await websocket.send_json({"type": "blocked", "reasons": gi.reasons})
                    continue

                # Per-user monthly token budget — refuse before paying for
                # KB retrieve + Bedrock. We catch the RateLimited HTTP
                # exception so we can emit a typed WS message instead of
                # bubbling.
                try:
                    await enforce_monthly_budget(db, user)
                except Exception:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Monthly token budget exceeded. Contact your administrator.",
                        "code": "TOKEN_BUDGET_EXCEEDED",
                    })
                    continue

                sess = await _load_or_create_session(db, user, dept, session_id)
                history = await _recent_history(db, sess.id)

                # Retrieval across all authorised departments, narrowed
                # to the user-selected metadata facets (andAll of equals).
                with trace.timer("retrieval") as _t_ret:
                    citations = await bedrock_service.retrieve_for_departments(
                        gi.redacted_text, session_depts, metadata_filters=meta_filters,
                    )
                    _ret_top = bedrock_service.average_top_score(citations, k=3) if citations else 0.0
                    _t_ret.note(
                        citations_count=len(citations),
                        top_score=round(float(_ret_top), 3),
                        depts=session_depts,
                        filters=_filter_keys(meta_filters),
                        filter_op=(meta_filters or {}).get("operator", "AND"),
                    )
                await websocket.send_json({
                    "type": "meta",
                    "session_id": sess.id,
                    "depts": session_depts,
                    "citations": [
                        {k: v for k, v in c.items()
                         if k in ("title", "s3_uri", "page", "score", "department")}
                        for c in citations[:6]
                    ],
                })

                dept_names = await _dept_names(db, session_depts)
                system_prompt = _build_prompt(
                    session_depts, dept_names, bedrock_service.build_context_block(citations)
                )

                with trace.timer("generation") as _t_gen:
                    full: list[str] = []
                    input_tokens = output_tokens = 0
                    async for ev in bedrock_service.chat_stream(
                        system_prompt=system_prompt, history=history, user_query=gi.redacted_text,
                        model_id=model_id,
                    ):
                        if ev["type"] == "delta":
                            full.append(ev["text"])
                            await websocket.send_json({"type": "delta", "text": ev["text"]})
                        elif ev["type"] == "usage":
                            input_tokens = ev.get("input", 0)
                            output_tokens = ev.get("output", 0)
                    _t_gen.note(
                        tokens_in=int(input_tokens), tokens_out=int(output_tokens),
                        model_id=model_id,
                    )

                raw = "".join(full)
                print("&"*80)
                print(raw)
                print("^"*80)
                clean, tail = _strip_tail_json(raw)
                print("!"*80)
                print("clean",clean)
                print("!"*80)
                print("^"*80)
                print("tail:",tail,"""tail.get("confidence", 0.5)""",tail.get("confidence", 0.5))
                print("^"*80)

                model_conf = float(tail.get("confidence", 0.5))
                confidence = _shape_confidence(model_conf, citations, retrieval_score=_ret_top)
                suggestions = list(tail.get("suggestions") or derive_follow_ups(raw, dept.code))
                print("!"*80)
                print("confidence:",confidence)
                print("!"*80)
                go = check_output(clean, dept.code)
                trace.record(
                    "guardrail_out", "blocked" if not go.allowed else "ok",
                    reasons=list(go.reasons), redacted=(go.redacted_text != clean),
                )
                if not go.allowed:
                    log_event("guardrail", "warning", "WS output blocked",
                              email=email, dept=dept_code, reasons=go.reasons)
                    clean = "I cannot share that information."
                    confidence = 0.0
                else:
                    clean = go.redacted_text

                latency_ms = int((time.monotonic() - t0) * 1000)
                response_source = "kb" if citations else "llm"

                user_msg = ChatMessage(session_id=sess.id, role=MessageRole.USER, content=query)
                db.add(user_msg)
                await db.flush()
                asst_msg = ChatMessage(
                    session_id=sess.id, role=MessageRole.ASSISTANT, content=clean,
                    citations={"items": citations, "source": response_source,
                               "depts": session_depts},
                    confidence=confidence,
                    tokens_input=input_tokens, tokens_output=output_tokens,
                    latency_ms=latency_ms, model_id=model_id,
                    blocked_by_guardrail=not go.allowed,
                    block_reasons=({"stage": "output", "reasons": list(go.reasons)}
                                   if not go.allowed else None),
                )
                db.add(asst_msg)
                if sess.title == "New Conversation":
                    sess.title = (query[:60] + "…") if len(query) > 60 else query
                await db.commit()
                await db.refresh(asst_msg)

                done_diag = _build_diagnostics(
                    trace, model_confidence=model_conf, citations=citations,
                    confidence=confidence,
                    answer_text=clean, retrieval_score=_ret_top,
                )
                await websocket.send_json({
                    "type": "done",
                    "session_id": sess.id,
                    "message_id": asst_msg.id,
                    "answer": clean,
                    "confidence": confidence,
                    "suggestions": suggestions[:4],
                    "latency_ms": latency_ms,
                    "tokens_input": input_tokens,
                    "tokens_output": output_tokens,
                    "blocked": not go.allowed,
                    "source": response_source,
                    "diagnostics": done_diag.model_dump() if is_priv else None,
                })

                log_event("chatbot", "info", "ws chat",
                          email=email, dept=dept.code, depts=session_depts,
                          session_id=sess.id, latency_ms=latency_ms,
                          tokens_in=input_tokens, tokens_out=output_tokens,
                          source=response_source,
                          hallucination_pct=done_diag.hallucination_pct)
        except WebSocketDisconnect:
            return
        except Exception as e:
            log_event("errors", "error", "WS error", error=str(e))
            try:
                await websocket.send_json({"type": "error", "message": "Server error"})
                await websocket.close()
            except Exception:
                pass


# ---------- History / export ----------

@router.get("/sessions", response_model=List[ChatSessionOut])
async def list_sessions(
    user: User = Depends(get_current_user),
    dept: Department = Depends(get_current_department),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(ChatSession)
        .where(ChatSession.user_id == user.id, ChatSession.department_id == dept.id)
        .order_by(desc(ChatSession.updated_at))
        .limit(50)
    )
    return list(res.scalars())


@router.get("/sessions/{session_id}", response_model=List[ChatMessageOut])
async def get_session(
    session_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user.id)
    )
    sess = res.scalar_one_or_none()
    if not sess:
        from app.core.exceptions import NotFoundError
        raise NotFoundError("Session not found")
    msgs = (await db.execute(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id)
    )).scalars().all()
    return list(msgs)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user.id)
    )
    sess = res.scalar_one_or_none()
    if sess:
        await db.delete(sess)
        await db.commit()


@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user.id)
    )
    sess = res.scalar_one_or_none()
    if not sess:
        from app.core.exceptions import NotFoundError
        raise NotFoundError("Session not found")
    msgs = (await db.execute(
        select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id)
    )).scalars().all()
    lines = [f"# Conversation: {sess.title}", f"# Exported: {sess.updated_at.isoformat()}", ""]
    for m in msgs:
        lines.append(f"## {m.role.value.upper()} [{m.created_at.isoformat()}]")
        lines.append(m.content)
        lines.append("")
    return {"filename": f"company-ai-session-{session_id}.txt", "content": "\n".join(lines)}


# ---------- Auto-complete / suggestions ----------

@router.get("/suggestions/seed", response_model=SuggestionResponse)
async def seed_suggestions(dept: Department = Depends(get_current_department)):
    return SuggestionResponse(suggestions=get_seed_prompts(dept.code, 6))


@router.post("/suggestions/autocomplete", response_model=SuggestionResponse)
async def suggestions_autocomplete(
    payload: SuggestionRequest, dept: Department = Depends(get_current_department),
):
    return SuggestionResponse(suggestions=autocomplete(dept.code, payload.prefix, limit=8))
