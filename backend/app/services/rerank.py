"""
Citation reranker — improves retrieval precision beyond raw vector score.

Dispatch order (first match wins):

  1. AWS Bedrock Rerank API
     Used when `BEDROCK_RERANK_MODEL_ARN` is configured. Models:
       arn:aws:bedrock:<region>::foundation-model/cohere.rerank-v3-5:0
       arn:aws:bedrock:<region>::foundation-model/amazon.rerank-v1:0
     Single API call, server-side rerank, billed per query.

  2. Local FlashRank (open-source, ONNX-based cross-encoder)
     Tiny (~30 MB), no PyTorch dependency, ~5 ms for 25 candidates on CPU.
     Used when flashrank is installed and #1 is unavailable.
     `pip install flashrank` — model auto-downloads on first use.

  3. Passthrough
     Returns input order (KB's native score ordering) unchanged.
     Safe default — code path stays functional even with no rerank stack.

The public surface is one async function, `rerank_citations`, that always
returns a list of citation dicts in the same shape it received them in.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from app.config import settings
from app.utils.logger import log_event

# Lazy singletons — created on first use, reused thereafter.
_flashrank_ranker = None
_flashrank_unavailable = False  # cache the negative result; don't retry every call


# ---------- AWS Bedrock Rerank ----------

async def _aws_rerank(
    query: str, citations: list[dict], top_k: int, model_arn: str
) -> Optional[list[dict]]:
    """Server-side rerank via `bedrock-agent-runtime.rerank()`.

    Returns reranked citations or None on failure (caller falls back).
    """
    from app.services.bedrock_service import bedrock_agent_runtime  # avoid circular import

    sources = [
        {
            "type": "INLINE",
            "inlineDocumentSource": {
                "type": "TEXT",
                "textDocument": {"text": c.get("snippet") or ""},
            },
        }
        for c in citations
    ]

    def _call():
        return bedrock_agent_runtime().rerank(
            queries=[{"type": "TEXT", "textQuery": {"text": query}}],
            sources=sources,
            rerankingConfiguration={
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "numberOfResults": min(top_k, len(citations)),
                    "modelConfiguration": {"modelArn": model_arn},
                },
            },
        )

    try:
        resp = await asyncio.to_thread(_call)
    except Exception as e:
        log_event("ai", "warning", "AWS rerank failed; falling back",
                  error=str(e), model_arn=model_arn)
        return None

    reranked: list[dict] = []
    for r in resp.get("results", []):
        idx = r.get("index")
        if idx is None or idx >= len(citations):
            continue
        c = dict(citations[idx])
        c["rerank_score"] = r.get("relevanceScore")
        reranked.append(c)
    return reranked or None


# ---------- Local FlashRank ----------

def _get_flashrank():
    """Lazy-load FlashRank. Returns the Ranker instance, or None if unavailable."""
    global _flashrank_ranker, _flashrank_unavailable
    if _flashrank_unavailable:
        return None
    if _flashrank_ranker is not None:
        return _flashrank_ranker
    try:
        from flashrank import Ranker  # type: ignore
    except ImportError:
        _flashrank_unavailable = True
        log_event("ai", "info", "flashrank not installed; rerank passthrough",
                  hint="pip install flashrank")
        return None
    try:
        _flashrank_ranker = Ranker(model_name=settings.LOCAL_RERANK_MODEL)
        log_event("ai", "info", "FlashRank loaded", model=settings.LOCAL_RERANK_MODEL)
    except Exception as e:
        _flashrank_unavailable = True
        log_event("ai", "warning", "FlashRank init failed; rerank disabled",
                  error=str(e), model=settings.LOCAL_RERANK_MODEL)
        return None
    return _flashrank_ranker


async def _local_rerank(
    query: str, citations: list[dict], top_k: int
) -> Optional[list[dict]]:
    ranker = _get_flashrank()
    if ranker is None:
        return None

    from flashrank import RerankRequest  # type: ignore

    passages = [
        {"id": i, "text": c.get("snippet") or "", "meta": {}}
        for i, c in enumerate(citations)
    ]

    def _call():
        return ranker.rerank(RerankRequest(query=query, passages=passages))

    try:
        results = await asyncio.to_thread(_call)
    except Exception as e:
        log_event("ai", "warning", "Local rerank failed; falling back",
                  error=str(e))
        return None

    reranked: list[dict] = []
    for r in results[:top_k]:
        idx = r.get("id")
        if idx is None or idx >= len(citations):
            continue
        c = dict(citations[idx])
        c["rerank_score"] = float(r.get("score", 0.0))
        reranked.append(c)
    return reranked or None


# ---------- Public dispatch ----------

async def rerank_citations(
    query: str, citations: list[dict], top_k: int
) -> tuple[list[dict], str]:
    """
    Rerank `citations` against `query`, return (top_k_ordered, backend_used).

    Backends: "bedrock" | "flashrank" | "passthrough". The label is
    surfaced in logs so we can see in production which path ran.
    """
    if not citations or top_k <= 0:
        return citations[:top_k], "passthrough"

    if not settings.RERANK_ENABLED:
        return citations[:top_k], "passthrough"

    if settings.BEDROCK_RERANK_MODEL_ARN:
        out = await _aws_rerank(
            query, citations, top_k, settings.BEDROCK_RERANK_MODEL_ARN
        )
        if out is not None:
            return out[:top_k], "bedrock"
        # fall through to local on failure

    out = await _local_rerank(query, citations, top_k)
    if out is not None:
        return out[:top_k], "flashrank"

    return citations[:top_k], "passthrough"
