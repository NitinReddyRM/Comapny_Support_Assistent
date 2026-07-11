from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Tuple

# ---------- Patterns ----------

PROMPT_INJECTION_PATTERNS = [
    r"\bignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompt)",
    r"\bdisregard\s+(previous|prior|above)\s+(instructions|rules)",
    r"\bforget\s+(everything|your\s+rules)",
    r"\bact\s+as\s+(an?\s+)?(admin|root|developer|jailbroken|dan)",
    r"\bsystem\s+prompt",
    r"\breveal\s+(your|the)\s+(prompt|instructions|system)",
    r"\boverride\s+(your|the)\s+(rules|guardrails|policies)",
    r"\bdeveloper\s+mode",
    r"\bdo\s+anything\s+now\b",
    r"\bbypass\s+(your\s+)?(restrictions|filters|safety)",
]

SQL_INJECTION_PATTERNS = [
    r"(\bunion\b.+\bselect\b)",
    r"(';\s*drop\s+table)",
    r"(--\s*$)",
    r"(\bor\s+1\s*=\s*1\b)",
    r"(\bxp_cmdshell\b)",
]

# Match common credential / financial / national-id leak attempts.
#
# IMPORTANT: order matters. Longer / more-specific patterns must come
# FIRST so they win against the shorter MOBILE_NUMBER pattern (which
# would otherwise greedily eat the first 10 digits of a 16-digit card).
PII_PATTERNS = [
    # Credentials must be first — they're the highest-impact leak.
    (r"(?i)(api[_-]?key|secret[_-]?key|password|passwd|bearer\s+\S+)\s*[:=]\s*\S+", "CREDENTIAL"),
    # 13–19 digit card numbers (optionally space/dash separated).
    (r"\b(?:\d[ -]?){12,18}\d\b", "CARD_NUMBER"),
    # SSN (US) — fixed shape, very low false-positive.
    (r"\b\d{3}-\d{2}-\d{4}\b", "SSN"),
    # IBAN — country code + 2 check digits + 10–30 alphanumerics.
    (r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b", "IBAN"),
    # 10-digit phone with optional country code / separators. Anchored
    # to digit boundaries so it doesn't eat into card sequences.
    (r"(?<!\d)(?:\+?\d{1,3}[ -]?)?\d{10}(?!\d)", "MOBILE_NUMBER"),
    # Email.
    (r"\b[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "EMAIL"),
]

CROSS_DEPT_PATTERNS = [
    r"\bshow\s+(me\s+)?(all\s+)?(salaries|salary|payroll)\b",
    r"\baccess\s+(the\s+)?admin\s+(panel|data)",
    r"\bother\s+departments?\s+data",
    r"\breveal\s+(passwords?|tokens?|keys?)",
]

TOXICITY_WORDS = {
    "kill", "die", "racist", "nazi", "terrorist", "bomb", "rape",
    # We keep this list short — Bedrock Guardrails handles the long tail.
}

# Output-side: detect actual system-prompt leakage (not just any mention).
SYSTEM_PROMPT_LEAK_PATTERNS = [
    r"\b(my|the|here'?s?\s+the)\s+system\s+prompt\s+(is|says|begins|reads)\s*:",
    r"\bmy\s+instructions\s+(are|say|read)\s*:",
    r"#\s*ROLE\s*&?\s*(SCOPE|STRICT\s+SCOPE)",      # leak of the section header from system_prompt.py
    r"#\s*ANTI-HALLUCINATION\s+RULES",
    r"#\s*SECURITY\s*(\(non-negotiable\))?",
]


@dataclass
class GuardrailResult:
    allowed: bool = True
    reasons: List[str] = field(default_factory=list)
    redacted_text: str = ""

    def block(self, reason: str) -> "GuardrailResult":
        self.allowed = False
        self.reasons.append(reason)
        return self


def _any_match(text: str, patterns: List[str]) -> Tuple[bool, str | None]:
    for p in patterns:
        if re.search(p, text, flags=re.IGNORECASE | re.MULTILINE):
            return True, p
    return False, None


def check_input(query: str) -> GuardrailResult:
    """
    Validate a user query against input guardrails.

    Returns a GuardrailResult; if .allowed is False, the caller must
    refuse the request and log to guardrail.log.
    """
    result = GuardrailResult(redacted_text=query)

    if not query or not query.strip():
        return result.block("Empty query")

    if len(query) > 4000:
        return result.block("Query too long (>4000 chars)")

    matched, pat = _any_match(query, PROMPT_INJECTION_PATTERNS)
    if matched:
        return result.block(f"Prompt injection / jailbreak detected ({pat})")

    matched, pat = _any_match(query, SQL_INJECTION_PATTERNS)
    if matched:
        return result.block(f"SQL injection pattern ({pat})")

    matched, pat = _any_match(query, CROSS_DEPT_PATTERNS)
    if matched:
        return result.block(f"Disallowed cross-department / privileged request ({pat})")

    lowered = query.lower()
    for w in TOXICITY_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", lowered):
            return result.block(f"Toxic content ({w})")

    # PII detection — redact but do not block (let user proceed if they
    # accidentally included an email).
    redacted = query
    for pattern, label in PII_PATTERNS:
        redacted = re.sub(pattern, f"[REDACTED_{label}]", redacted, flags=re.IGNORECASE)
    result.redacted_text = redacted

    return result


def check_output(answer: str, department_code: str) -> GuardrailResult:
    """
    Validate model output before returning to the user.

    - Strip credential-style leaks.
    - Flag if the answer references a *different* department's restricted data.
    - Block on actual system-prompt disclosure (not just casual mention).
    """
    result = GuardrailResult(redacted_text=answer)

    if not answer:
        return result

    # System-prompt disclosure — require an actual leak phrase, not just
    # the words "system" and "prompt" co-occurring.
    matched, pat = _any_match(answer, SYSTEM_PROMPT_LEAK_PATTERNS)
    if matched:
        return result.block(f"System-prompt disclosure detected ({pat})")

    # Credential redaction — only triggers when the *full* credential
    # shape is matched, so "please change your password" no longer
    # false-positives.
    cred_re = re.compile(
        r"(?i)(password|passwd|api[_-]?key|secret[_-]?key|bearer\s+\S+)\s*[:=]\s*\S+"
    )
    if cred_re.search(result.redacted_text):
        result.redacted_text = cred_re.sub(r"\1=[REDACTED]", result.redacted_text)
        result.reasons.append("Credential-like content redacted")

    # Detect mention of another known department's restricted data.
    known_depts = {"hr", "finance", "legal", "it", "operations", "security", "procurement"}
    lowered = result.redacted_text.lower()
    dept_lc = (department_code or "").lower()
    for d in known_depts - {dept_lc}:
        if re.search(rf"\b{re.escape(d)}\s+(salary|salaries|payroll|policies?)\b", lowered):
            return result.block(f"Output referenced restricted {d.upper()} data")

    return result
