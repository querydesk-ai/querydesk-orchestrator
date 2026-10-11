"""
Rule-based message signals for the decision layer: is this an action request,
and is it asking about an existing request?

Why rules, not an LLM: eval v0 showed retrieval scores can't separate actions
from questions (83% of action requests clear confidence_high), and an LLM
classifier would add ~1.4 s to every message. These patterns run in
microseconds, are fully testable, and are measured on the eval set. Jev may
replace them on Day 5 behind the same DecisionEngine interface.

The key asymmetry: a message is informational only if it *reads as a question
and makes no request*. Anything else — imperatives, "pls restart",
"can you…", and problem statements like "I can't see my leads" — is an action.
Erring towards "action" is deliberate: the costly failure is answering an
action request with an article, not asking one extra question.
"""

import re
from dataclasses import dataclass

# "status of my requests", "any update on SR-1042", "what happened to my ticket".
_STATUS = re.compile(
    r"\b(status|update|updates|progress|happened to|where is|where's)\b"
    r".*\b(request|requests|sr|srs|ticket|tickets)\b"
    r"|\bsr[-\s]?\d+\b"
    r"|\b(my|open)\s+(requests|tickets|srs)\b",
    re.IGNORECASE,
)

_QUESTION_START = re.compile(
    r"^(what|whats|what's|when|where|why|who|which|how|is|are|am|do|does|did|"
    r"can i|can we|could i|should|will|would i|any)\b",
    re.IGNORECASE,
)

# Polite or explicit asks anywhere in the message. "I need" doesn't count after
# "do" ("do I need approval…?" is a question). Bare verbs like "raise a" are
# deliberately absent: "should I raise a request?" is a question; a leading
# "raise …" is caught by _IMPERATIVE_START instead.
_REQUEST_MARKER = re.compile(
    r"\b(please|pls|plz|kindly|can you|could you|can someone|could someone|"
    r"would you|i want to|i'd like|i would like)\b"
    r"|(?<!do )\b(i|we) need\b",
    re.IGNORECASE,
)

# An imperative verb as the first word: "restart the claims app", "need uat extended".
_IMPERATIVE_START = re.compile(
    r"^(restart|bounce|reset|unlock|extend|keep|add|open|allow|whitelist|change|"
    r"correct|fix|update|onboard|register|raise|create|close|remove|provision|"
    r"book|order|submit|give|grant|enable|disable|delete|need)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Intent:
    is_action: bool
    is_status: bool


def is_question(text: str) -> bool:
    t = text.strip()
    return t.endswith("?") or bool(_QUESTION_START.match(t))


def has_request(text: str) -> bool:
    t = text.strip()
    return bool(_REQUEST_MARKER.search(t) or _IMPERATIVE_START.match(t))


def classify(text: str) -> Intent:
    return Intent(
        is_action=has_request(text) or not is_question(text),
        is_status=bool(_STATUS.search(text)),
    )
