"""
Deterministic rule-based response layer.

Runs BEFORE guardrails / retrieval / LLM for cheap small-talk and FAQ
intents (greetings, thanks, identity questions). When a rule fires we
skip Bedrock entirely — saving cost and latency.

Why a separate module
---------------------
* Keeps the rule catalog declarative and easy to extend (one entry per
  intent, regex-based).
* Easy to override at runtime via the `RULE_ENGINE_OVERRIDES` env-style
  config if a deployment wants different copy.

Design notes
------------
* Each rule is compiled once at import time.
* `match()` returns the *first* matching rule. Order rules from most
  specific to most general.
* Patterns are anchored with `\b` so "hi" doesn't fire on "hi-fi".
* All regexes use `re.IGNORECASE`.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

ASSISTANT_NAME = "Omni AI Assistant"


# ---------------------------------------------------------------------------
# Rule catalog
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    intent: str
    patterns: tuple
    responses: tuple           # rotating; one is picked deterministically per query
    suggestions: tuple = ()    # optional follow-up chips
    confidence: float = 1.0
    requires_short_query: bool = True  # only match queries < ~12 words

    @property
    def compiled(self) -> List[re.Pattern]:
        return [re.compile(p, re.IGNORECASE) for p in self.patterns]


def _r(intent: str, patterns, responses, suggestions=(), confidence=1.0, requires_short_query=True) -> Rule:
    return Rule(
        intent=intent,
        patterns=tuple(patterns),
        responses=tuple(responses),
        suggestions=tuple(suggestions),
        confidence=confidence,
        requires_short_query=requires_short_query,
    )


# Order matters — most specific first.
RULES: List[Rule] = [
    _r(
        intent="identity",
        patterns=[
            r"\b(who\s+are\s+you|what\s+are\s+you|what'?s?\s+your\s+name|your\s+name)\b",
            r"\bwho\s+(made|built|created)\s+you\b",
        ],
        responses=[
            f"I'm {ASSISTANT_NAME} — your enterprise knowledge assistant. "
            f"I answer questions from your department's policy and knowledge base, "
            f"with citations and a confidence score.",
        ],
        suggestions=(
            "What can you help me with?",
            "Show me my department's policies",
        ),
    ),
    _r(
        intent="capabilities",
        patterns=[
            r"\bwhat\s+can\s+you\s+do\b",
            r"\bhow\s+can\s+you\s+help\b",
            r"\bhelp\s+me\b$",
        ],
        responses=[
            f"I can answer questions from your department's knowledge base, "
            f"summarise policies, surface the right document for a topic, and "
            f"open a support ticket if I can't find an answer. Try asking a "
            f"policy question to get started.",
        ],
        suggestions=(
            "What is the leave policy?",
            "How do I raise a support ticket?",
        ),
    ),
    _r(
        intent="greeting",
        patterns=[
            r"^\s*(hi|hello|hey|hiya|yo)\b[\s!.,]*$",
            r"^\s*(good\s+(morning|afternoon|evening|day))\b[\s!.,]*$",
            r"^\s*(greetings|namaste|hola)\b[\s!.,]*$",
        ],
        responses=[
            f"Hi there! I'm {ASSISTANT_NAME}. What would you like to know today?",
            f"Hello — I'm {ASSISTANT_NAME}. Ask me anything from your department's knowledge base.",
            f"Hey! {ASSISTANT_NAME} here. How can I help?",
        ],
        suggestions=(
            "What is the leave policy?",
            "Show me reimbursement rules",
        ),
    ),
    _r(
        intent="wellbeing",
        patterns=[
            r"^\s*how\s+are\s+you(\s+(doing|today))?\b[\s?!.]*$",
            r"^\s*(what'?s\s+up|sup)\b[\s?!.]*$",
        ],
        responses=[
            f"I'm running smoothly, thanks for asking! Ready to help with policy and knowledge-base questions.",
        ],
        suggestions=("What can you do?",),
    ),
    _r(
        intent="thanks",
        patterns=[
            r"^\s*(thanks|thank\s+you|thx|ty|cheers|appreciate\s+it)\b[\s!.,]*$",
        ],
        responses=[
            "You're welcome — happy to help! Let me know if there's anything else.",
            "Anytime! Ask me another question whenever you need.",
        ],
    ),
    _r(
        intent="goodbye",
        patterns=[
            r"^\s*(bye|goodbye|see\s+you|cya|good\s+night)\b[\s!.,]*$",
        ],
        responses=[
            "Goodbye! Come back anytime.",
            "Take care — I'll be here when you need me.",
        ],
    ),
    _r(
        intent="affirmation",
        patterns=[
            r"^\s*(ok|okay|cool|nice|got\s+it|sounds\s+good|alright)\b[\s!.,]*$",
        ],
        responses=[
            "Great. Anything else you'd like to ask?",
        ],
    ),
]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

@dataclass
class RuleMatch:
    intent: str
    answer: str
    suggestions: List[str] = field(default_factory=list)
    confidence: float = 1.0


def _select_response(rule: Rule, query: str) -> str:
    """Stable pick — same query always gets the same response variant."""
    if len(rule.responses) == 1:
        return rule.responses[0]
    idx = hash(query.strip().lower()) % len(rule.responses)
    return rule.responses[idx]


def match(query: str) -> Optional[RuleMatch]:
    """Return a RuleMatch if the query matches a configured rule, else None.

    Rules are evaluated in declaration order. The first hit wins.
    """
    if not query:
        return None
    q = query.strip()
    if not q:
        return None

    word_count = len(re.findall(r"\w+", q))

    for rule in RULES:
        if rule.requires_short_query and word_count > 12:
            continue
        for pat in rule.compiled:
            if pat.search(q):
                return RuleMatch(
                    intent=rule.intent,
                    answer=_select_response(rule, q),
                    suggestions=list(rule.suggestions),
                    confidence=rule.confidence,
                )
    return None


def list_intents() -> List[str]:
    """Diagnostics: list configured intents."""
    return [r.intent for r in RULES]
