"""
Make a follow-up searchable on its own: "and for prod?" after a question
about UAT becomes "<the UAT question>, but for prod".

Only the *search* text is rewritten (embedding, retrieval, routing and the
grounded answer). The original message stays the source for everything that
must not be reworded: intent signals, field extraction (values must be stated
by the user), the SR description and the logs. Any failure, timeout or odd
output returns the original, so this can only help or do nothing.
"""

import asyncio
import logging
import re

from querydesk.core.config import settings
from querydesk.services.llm.fireworks import FireworksError, chat_completion

logger = logging.getLogger(__name__)

# Words that lean on an earlier turn. Cheap gate: most messages never pay for the call.
_REFERENCE = re.compile(
    r"\b(it|its|that|this|those|these|them|they|same|again|previous|earlier|above|"
    r"other|instead|also|too)\b|^\s*(and|what about|how about|but)\b",
    re.IGNORECASE,
)

PROMPT = """Rewrite the user's latest message so it can be understood without the earlier chat.

Earlier chat (oldest first):
{history}

Latest message: {query}

Rules:
- Replace references like "it", "that", "the same" with what they refer to in the earlier chat.
- Keep the meaning and wording otherwise. Do not add facts, names or values not in the chat.
- If the latest message already stands on its own, repeat it unchanged.
- Reply with ONLY the rewritten message on one line."""

# A rewrite far longer than the message means the model answered or invented.
_MAX_GROWTH = 4
_MIN_EXTRA_CHARS = 200
_CONTEXT_MESSAGES = 6


def has_reference(text: str) -> bool:
    return bool(_REFERENCE.search(text))


def _transcript(turns: list[dict]) -> str:
    return "\n".join(f"{'User' if t['role'] == 'user' else 'Bot'}: {t['text']}" for t in turns)


async def standalone(query: str, turns: list[dict]) -> str:
    """The query rewritten in the light of `turns`, or `query` itself."""
    if not turns or not has_reference(query):
        return query
    prompt = PROMPT.format(history=_transcript(turns[-_CONTEXT_MESSAGES:]), query=query)
    try:
        reply = await asyncio.wait_for(
            chat_completion(messages=[{"role": "user", "content": prompt}], temperature=0.0),
            timeout=settings.history_rewrite_timeout_s,
        )
    except (FireworksError, TimeoutError) as e:
        logger.warning("Query rewrite skipped: %s", type(e).__name__)
        return query
    rewritten = " ".join(reply.split()).strip().strip('"')
    if not rewritten or len(rewritten) > len(query) * _MAX_GROWTH + _MIN_EXTRA_CHARS:
        logger.info("Query rewrite discarded (empty or too long)")
        return query
    return rewritten
