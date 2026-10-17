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
# Each branch needs a real status phrasing: a bare SR number ("close SR-1042")
# or "request" next to "status" ("what does the status Pending mean on a
# request?") is not enough, because rule 1 beats every other rule.
_SR_REF = r"(request|requests|ticket|tickets|srs?|sr[-\s]?\d+)"
_STATUS = re.compile(
    rf"\b(status|progress)\s+(of|on|for)\s+(my|our|the|this|that|these|those)\b.*\b{_SR_REF}\b"
    rf"|\b(update|updates|news)\s+on\b.*\b{_SR_REF}\b"
    rf"|\b(happened to|where is|where's)\s+(my|our)\b.*\b{_SR_REF}\b"
    r"|\b(my|open)\s+(requests|tickets|srs)\b"
    r"|\bsr[-\s]?\d+\b.*\b(status|progress|update|updates)\b"
    r"|\b(status|progress|update|updates)\b.*\bsr[-\s]?\d+\b",
    re.IGNORECASE,
)
# "update on SR-1042?" starts with an imperative verb but is a status question.
_UPDATE_ON = re.compile(r"^updates?\s+on\b", re.IGNORECASE)

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

# A polite way to ask a question: "I'd like to know…", "can you tell me…",
# "explain…". The opening reads as a request but asks for information, so it
# makes the message a question and its words don't count as request markers.
# Any other marker later in the message ("…and please reset it") still does.
_INFO_ASK = re.compile(
    r"^(i would like to|i'd like to|i want to|i wanna|i am trying to|i'm trying to)\s+"
    r"(know|understand|learn|find out)\b"
    r"|^(can|could|would) you\s+(please\s+)?(tell|explain|let me know|help me understand)\b"
    r"|^(tell me|explain)\b",
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
    return t.endswith("?") or bool(_QUESTION_START.match(t) or _INFO_ASK.match(t))


def has_request(text: str) -> bool:
    t = text.strip()
    rest = _INFO_ASK.sub("", t, count=1)
    return bool(_REQUEST_MARKER.search(rest) or _IMPERATIVE_START.match(t))


def is_status(text: str) -> bool:
    t = text.strip()
    # An instruction about a request ("close my open requests", "update the
    # ticket…") is an action on it, not a question about where it is.
    if _IMPERATIVE_START.match(t) and not _UPDATE_ON.match(t):
        return False
    return bool(_STATUS.search(t))


# "SR-1042", "sr 1042", "SR1042": the number of one SR the message names.
_SR_NUMBER = re.compile(r"\bsr[-\s]?(\d+)\b", re.IGNORECASE)


def sr_number(text: str) -> str | None:
    """The first SR number in the message, if it names one."""
    m = _SR_NUMBER.search(text)
    return m.group(1) if m else None


def classify(text: str) -> Intent:
    return Intent(
        is_action=has_request(text) or not is_question(text),
        is_status=is_status(text),
    )
