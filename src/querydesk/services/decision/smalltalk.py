"""
Small talk: "hi", "thanks", "what can you do?". These are conversation, not a
knowledge question and not a request, so they must never reach retrieval or
the service desk (rule 5 would escalate them: a bare "hi" is not a ticket).

Rule-based and anchored at both ends, like the cancel pattern: the whole
message has to be the small talk. "hi, my password reset fails" is a real
message and is decided normally. Replies are fixed (no LLM call, no latency),
and the capabilities reply is built from the catalog, never a hardcoded list.
"""

import re
from enum import StrEnum

from querydesk.services import catalog


class Kind(StrEnum):
    GREETING = "greeting"
    HOW_ARE_YOU = "how_are_you"
    THANKS = "thanks"
    GOODBYE = "goodbye"
    ACK = "ack"
    CAPABILITIES = "capabilities"


_END = r"[\s.!,]*$"
_NAME = r"(\s+(there|team|bot|querydesk|everyone|all))?"

_PATTERNS: list[tuple[Kind, re.Pattern]] = [
    (
        Kind.HOW_ARE_YOU,
        re.compile(
            r"^\s*((hi|hello|hey)" + _NAME + r"[\s,!.]+)?"
            r"(how\s+are\s+(you|u)|how'?s\s+it\s+going|how\s+do\s+you\s+do|what'?s\s+up|sup)"
            r"( today| doing)?[\s?.!]*$",
            re.IGNORECASE,
        ),
    ),
    (
        Kind.GREETING,
        re.compile(
            r"^\s*(hi+|hello+|hey+|hiya|howdy|yo|greetings|namaste|"
            r"good\s+(morning|afternoon|evening|day))" + _NAME + _END,
            re.IGNORECASE,
        ),
    ),
    (
        Kind.THANKS,
        re.compile(
            r"^\s*(ok(ay)?[\s,]+)?(thanks|thank\s+you|thankyou|thx|ty|cheers|much\s+appreciated|"
            r"great[\s,]+thanks|thanks\s+a\s+lot|thank\s+you\s+so\s+much)" + _NAME + _END,
            re.IGNORECASE,
        ),
    ),
    (
        Kind.GOODBYE,
        re.compile(
            r"^\s*(bye+|goodbye|good\s*night|see\s+(you|ya)|cya|take\s+care|that'?s\s+all|"
            r"that\s+is\s+all)( for now| later| soon)?" + _END,
            re.IGNORECASE,
        ),
    ),
    (
        Kind.CAPABILITIES,
        re.compile(
            r"^\s*(who\s+are\s+you|what\s+are\s+you|what\s+(can|do)\s+you\s+(do|help\s+(me\s+)?with)|"
            r"how\s+can\s+you\s+help(\s+me)?|what\s+is\s+this|help|"
            r"what\s+(services|things)\s+(do|can)\s+you\s+(offer|help\s+with|handle))[\s?.!]*$",
            re.IGNORECASE,
        ),
    ),
    (
        # Acknowledgements with nothing open. While a conversation is open the
        # pipeline reads "yes"/"no" itself before it ever asks this module.
        Kind.ACK,
        re.compile(
            r"^\s*(ok(ay)?|k|kk|cool|fine|alright|got\s+it|sure|yes|yeah|yep|yup|no|nope|nah|"
            r"hmm+|oh|i\s+see|understood|noted)[\s.!,]*$",
            re.IGNORECASE,
        ),
    ),
]


def classify(text: str) -> Kind | None:
    """The kind of small talk the whole message is, or None."""
    for kind, pattern in _PATTERNS:
        if pattern.match(text):
            return kind
    return None


def _capabilities() -> str:
    names = ", ".join(c.name for c in catalog.all_categories().values())
    return (
        "I'm the QueryDesk service desk assistant. I can answer questions from the "
        "knowledge base and raise service requests for you. Right now I cover: "
        f"{names}. Ask me a question, or tell me what you need done."
    )


_FIXED = {
    Kind.GREETING: "Hi! I'm the QueryDesk assistant. Ask me a question, or tell me what you need "
    "done and I'll raise a request.",
    Kind.HOW_ARE_YOU: "Doing well, thanks for asking! What can I help you with today?",
    Kind.THANKS: "You're welcome! Anything else I can help with?",
    Kind.GOODBYE: "Goodbye! Message me any time you need the service desk.",
    Kind.ACK: "Got it. Is there anything you'd like help with?",
}


def reply(kind: Kind) -> str:
    return _capabilities() if kind is Kind.CAPABILITIES else _FIXED[kind]
