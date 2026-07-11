from __future__ import annotations

from typing import List

# Per-department seed prompts. Used both for the empty-state landing
# screen and as autocomplete candidates.
DEPARTMENT_PROMPTS: dict[str, list[str]] = {
    "hr": [
        "What is the leave policy?",
        "How do I apply for parental leave?",
        "What is the notice period for resignation?",
        "How is leave encashment calculated?",
        "What benefits are included in the standard package?",
        "What is the work-from-home policy?",
        "How do I raise a grievance?",
    ],
    "finance": [
        "What is business finance?",
        "What are the three main criteria used to classify sources of funds?",
        "What are long-term sources of finance?",
        "What are retained earnings?",
        "What are preference shares?",
    ],
    "it": [
        "How do I request a new laptop?",
        "What is the VPN setup procedure?",
        "How do I reset my SSO password?",
        "What is the policy on personal device usage?",
        "How do I report a phishing email?",
    ],
    "legal": [
        "What is the objective of the Legal Compliance Framework Policy?",
        "Who does the Legal Compliance Framework Policy apply to?",
        "What document is used to compile legal obligations and responsible persons?",
        "Who is responsible for overall monitoring of the compliance system?",
    ],
    "operations": [
        "What does the term “Agency” mean in the Operational Policies?",
        "Are shareholder loans eligible for coverage?",
        "What nationality requirement applies to a natural person seeking a guarantee?",
    ],
    "security": [
        "What are the three key objectives of the Information Security Policy?",
        "What is the password policy?",
        "Can departmental emails be automatically forwarded to third-party email systems?",
        "How often must the Information Security Policy be reviewed?",
    ],
    "procurement": [
        "What is the purpose of the Corporate Procurement Policy Summary?",
        "Can a vendor receive a contract award without being registered?",
        "How long must a vendor have been offering goods or services under its business name to meet eligibility requirements?",
    ],
}


def get_seed_prompts(department_code: str, limit: int = 6) -> List[str]:
    return DEPARTMENT_PROMPTS.get(department_code.lower(), [])[:limit]


def autocomplete(department_code: str, prefix: str, limit: int = 8) -> List[str]:
    """Cheap prefix / substring match over the seed prompt list."""
    p = prefix.strip().lower()
    if not p:
        return get_seed_prompts(department_code, limit)
    pool = DEPARTMENT_PROMPTS.get(department_code.lower(), [])
    starts = [s for s in pool if s.lower().startswith(p)]
    contains = [s for s in pool if p in s.lower() and s not in starts]
    return (starts + contains)[:limit]


def derive_follow_ups(answer: str, department_code: str) -> List[str]:
    """
    Pull follow-up questions out of the model's structured JSON tail,
    or fall back to seed prompts. The model is instructed (in the
    system prompt) to emit `{"suggestions":[...]}` at the end.
    """
    import json
    import re

    m = re.search(r"```json\s*(\{.*?\})\s*```", answer, flags=re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(1))
            sug = obj.get("suggestions") or []
            return [s for s in sug if isinstance(s, str)][:4]
        except Exception:
            pass
    return get_seed_prompts(department_code, 3)
