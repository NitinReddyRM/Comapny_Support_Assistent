from __future__ import annotations

import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator


@dataclass
class _Step:
    step: str
    status: str
    duration_ms: int
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "status": self.status,
            "duration_ms": int(self.duration_ms),
            "detail": dict(self.detail),
        }


class Trace:
    def __init__(self) -> None:
        self._steps: list[_Step] = []

    def record(
        self,
        step: str,
        status: str,
        *,
        duration_ms: int = 0,
        **detail: Any,
    ) -> None:
        self._steps.append(_Step(
            step=str(step),
            status=str(status),
            duration_ms=max(0, int(duration_ms)),
            detail=detail,
        ))

    @contextmanager
    def timer(self, step: str, *, default_status: str = "ok") -> Iterator["_TimerHandle"]:
        """Time a block and record it as a step. Use ``handle.note(...)`` to
        attach detail keys; ``handle.status(...)`` to override the default.
        """
        t0 = time.monotonic()
        handle = _TimerHandle(default_status=default_status)
        try:
            yield handle
        finally:
            dur_ms = int((time.monotonic() - t0) * 1000)
            self.record(step, handle._status, duration_ms=dur_ms, **handle._detail)

    def to_list(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self._steps]


class _TimerHandle:
    """Mutable handle yielded by ``Trace.timer`` for the block to annotate."""

    def __init__(self, *, default_status: str) -> None:
        self._status: str = default_status
        self._detail: dict[str, Any] = {}

    def status(self, new_status: str) -> None:
        self._status = str(new_status)

    def note(self, **kv: Any) -> None:
        self._detail.update(kv)


# ---------- Hallucination scoring ----------

# Common English stop-words used by ``answer_grounding_overlap`` — kept
# small on purpose so the routine stays microsecond-cheap (no NLTK).
_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "of", "to",
    "in", "on", "at", "by", "for", "from", "with", "as", "is", "are", "was",
    "were", "be", "been", "being", "it", "its", "this", "that", "these",
    "those", "i", "you", "he", "she", "we", "they", "them", "us", "our",
    "your", "their", "my", "me", "him", "her", "his", "hers", "ours",
    "yours", "theirs", "will", "would", "should", "could", "can", "may",
    "might", "must", "do", "does", "did", "done", "have", "has", "had",
    "not", "no", "yes", "so", "than", "such", "very", "just", "also",
    "more", "most", "any", "all", "some", "each", "every", "into", "out",
    "up", "down", "over", "under", "again", "about", "between", "before",
    "after", "while", "when", "where", "what", "which", "who", "whom",
    "how", "why", "there", "here",
})

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")


def _content_tokens(text: str) -> set[str]:
    """Lower-cased content-word set, dropping stop-words and tokens < 3 chars.
    Stable, allocation-light — the heavy lifting is one regex pass."""
    if not text:
        return set()
    out: set[str] = set()
    for m in _WORD_RE.finditer(text):
        tok = m.group(0).lower()
        if tok not in _STOP_WORDS:
            out.add(tok)
    return out


def answer_grounding_overlap(
    answer: str,
    citation_snippets: Iterable[str],
) -> float:
    """Fast lexical-coverage signal: what fraction of the answer's content
    words also appear in the retrieved citation snippets?

    Cheap proxy for "is the answer grounded in the sources" — no LLM,
    no embeddings, pure set ops. Returns 0..1. Returns 0.0 when either
    side is empty.
    """
    ans_tokens = _content_tokens(answer)
    if not ans_tokens:
        return 0.0
    cit_tokens: set[str] = set()
    for snip in citation_snippets:
        if snip:
            cit_tokens |= _content_tokens(snip)
    if not cit_tokens:
        return 0.0
    covered = len(ans_tokens & cit_tokens)
    return covered / len(ans_tokens)


# Canonical refusal phrases the model is instructed to emit when CONTEXT
# is empty or insufficient. A correct refusal is the *opposite* of a
# hallucination — score it low.
_REFUSAL_MARKERS: tuple[str, ...] = (
    "could not find that information",
    "cannot find that information",
    "no information was found",
    "i don't have information",
    "i do not have information",
    "please contact",
)


def is_refusal(answer: str) -> bool:
    """Heuristic detector for the canonical "no coverage → refuse" reply.
    Cheap substring scan over the lower-cased answer."""
    if not answer:
        return False
    a = answer.lower()
    return any(marker in a for marker in _REFUSAL_MARKERS)


def compute_hallucination_pct(
    *,
    model_confidence: float,
    retrieval_score: float,
    has_citations: bool,
    answer_overlap: float | None = None,
    answer_text: str | None = None,
) -> int:
    """Return a 0..100 hallucination score (higher = worse).

    The intuition:
      * A correct refusal ("I could not find that…") is the *opposite* of
        a hallucination. When ``answer_text`` matches the canonical
        refusal markers, return a near-zero score regardless of the
        retrieval signals.
      * With KB citations, groundedness = blend of model self-confidence,
        retrieval top-k score, and (when supplied) answer↔citation lexical
        overlap. Hallucination = (1 - groundedness).
      * Without citations AND no refusal, the answer is unsourced — floor
        the score at 70 % so the UI flags it loudly.

    ``answer_overlap`` is optional so callers that already paid for it
    (via ``answer_grounding_overlap``) can sharpen the signal at no extra
    cost. When omitted, the legacy 50/50 blend is preserved.

    These thresholds mirror the ``_shape_confidence`` helper in the chat
    endpoint so the user-facing confidence chip and the superadmin
    hallucination chip move together.
    """
    # Honest refusal short-circuit — the model said "I don't know" instead
    # of inventing; that's a *correct* outcome.
    if answer_text and is_refusal(answer_text):
        return 5

    mc = max(0.0, min(1.0, float(model_confidence or 0.0)))
    rs = max(0.0, min(1.0, float(retrieval_score or 0.0)))
    if has_citations:
        if answer_overlap is None:
            groundedness_val = 0.5 * mc + 0.5 * rs
        else:
            ao = max(0.0, min(1.0, float(answer_overlap)))
            # 40 % model confidence · 35 % retrieval score · 25 % lexical
            # overlap — the overlap term is the cheapest, sharpest signal
            # for "did the answer actually come from the sources".
            groundedness_val = 0.40 * mc + 0.35 * rs + 0.25 * ao
        hallucination = 1.0 - groundedness_val
    else:
        # No citations and the model didn't refuse — the answer is
        # unsourced. Floor at 70 %; a very confident model can pull it
        # down to ~70 but no further.
        hallucination = max(0.70, 1.0 - 0.30 * mc)
    pct = round(max(0.0, min(1.0, hallucination)) * 100)
    return int(pct)


def groundedness(*, model_confidence: float, retrieval_score: float, has_citations: bool) -> float:
    """Companion to ``compute_hallucination_pct`` — returns the 0..1 blended
    groundedness score (mirrors what the chat endpoint stores in
    ``ChatResponse.confidence``).
    """
    mc = max(0.0, min(1.0, float(model_confidence or 0.0)))
    rs = max(0.0, min(1.0, float(retrieval_score or 0.0)))
    if has_citations:
        return max(0.0, min(1.0, 0.5 * mc + 0.5 * rs))
    return max(0.0, min(0.30, mc))
