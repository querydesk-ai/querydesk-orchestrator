"""
The only module that talks to Jev (TypeSafe's decision model, via JevStation).

Jev answers typed questions about a `state`. QueryDesk asks one `choice`
question per message — action / status / information / out_of_scope — which
covers the PRD's "is this an action request?" and "is it about an existing
request?" in a single question.

JevStation bills per *evaluation* (one request), not per question, and allows
up to 12 questions per request, so classify_intents() packs many messages into
one request: each message is a numbered entry in the state and gets its own
question. That's an eval-time optimisation; a live /chat call would classify
one message. Raises JevError on any failure.
"""

from dataclasses import dataclass, field

import httpx

from querydesk.core.config import settings
from querydesk.core.errors import JevError
from querydesk.services.decision.signals import Intent

# JevStation's per-evaluation question limit.
MAX_QUESTIONS = 12

INTENT_CRITERIA = {
    "action": (
        "The user asks the service desk to DO something for them: reset, unlock, restart, "
        "extend, grant access, change, onboard, process, fix."
    ),
    "status": (
        "The user asks about the status or progress of a request or ticket they have "
        "already raised."
    ),
    "information": (
        "The user asks a question to learn how something works, a policy, a timeline or "
        "the steps to follow — no action is requested."
    ),
    "out_of_scope": (
        "Not an IT or insurance-operations service desk matter (e.g. HR, payroll, "
        "personal or general-knowledge questions), or too vague to act on."
    ),
}


@dataclass
class JevIntent:
    label: str
    probabilities: dict[str, float]
    confidence: float | None = None

    def as_intent(self) -> Intent:
        """The rule engine's signal shape, so Jev can stand in for signals.classify()."""
        return Intent(is_action=self.label == "action", is_status=self.label == "status")


@dataclass
class Evaluation:
    answers: dict[str, dict]
    credits_charged: int | None = None
    credits_remaining: int | None = None
    latency_ms: float | None = None
    raw: dict = field(default_factory=dict)


def _auth_headers() -> dict:
    if not settings.typesafe_api_key:
        raise JevError("TYPESAFE_API_KEY is not set — it is the JevStation API key.")
    return {"Authorization": f"Bearer {settings.typesafe_api_key}"}


def build_intent_request(messages: list[str]) -> dict:
    """One state holding every message, one choice question per message."""
    if not 1 <= len(messages) <= MAX_QUESTIONS:
        raise ValueError(f"classify 1..{MAX_QUESTIONS} messages per request, got {len(messages)}")
    ids = [f"m{i}" for i in range(1, len(messages) + 1)]
    return {
        "model": settings.jev_model,
        "state": {
            "context": "Messages sent to the internal IT and operations service desk of an "
            "insurance company. Each is from a different user and must be judged on its own.",
            "messages": dict(zip(ids, messages, strict=True)),
        },
        "questions": {
            mid: {
                "type": "choice",
                "instructions": f"Classify ONLY message {mid} in state.messages, ignoring "
                "every other message. What kind of message is it?",
                "criteria": INTENT_CRITERIA,
            }
            for mid in ids
        },
    }


async def evaluate(request: dict) -> Evaluation:
    """POST one evaluation. Every call may spend credits — callers decide when."""
    async with httpx.AsyncClient(timeout=settings.jev_timeout_s) as client:
        try:
            resp = await client.post(
                f"{settings.jev_base_url}/systemone", json=request, headers=_auth_headers()
            )
        except httpx.HTTPError as e:
            raise JevError(f"Jev request failed: {e}") from e

    if resp.status_code != 200:
        raise JevError(f"Jev returned {resp.status_code}: {resp.text[:300]}")
    try:
        body = resp.json()
        data = body["data"]
        answers = data["answers"]
    except (ValueError, KeyError, TypeError) as e:
        raise JevError(f"Unexpected Jev response: {resp.text[:300]}") from e
    if body.get("code", 0) != 0:
        raise JevError(f"Jev error {body.get('code')}: {body.get('message')}")

    credits = data.get("credits") or {}
    return Evaluation(
        answers=answers,
        credits_charged=credits.get("charged"),
        credits_remaining=credits.get("remaining"),
        latency_ms=data.get("latency_ms"),
        raw=body,
    )


def parse_intents(request: dict, evaluation: Evaluation) -> list[JevIntent]:
    """Answers back in message order; a missing or malformed answer is a JevError."""
    intents = []
    for mid in request["questions"]:
        answer = evaluation.answers.get(mid)
        if not answer or answer.get("choice") not in INTENT_CRITERIA:
            raise JevError(f"Jev gave no usable answer for {mid}: {answer!r}")
        intents.append(
            JevIntent(
                label=answer["choice"],
                probabilities=answer.get("probabilities") or {},
                confidence=answer.get("confidence"),
            )
        )
    return intents


async def classify_intents(messages: list[str]) -> list[JevIntent]:
    request = build_intent_request(messages)
    return parse_intents(request, await evaluate(request))
