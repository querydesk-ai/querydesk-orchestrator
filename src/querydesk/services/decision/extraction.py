"""
Required-field extraction for the SR path.

One LLM call pulls the category's required fields (from categories.json, via
the catalog — never hardcoded) out of the user's message. It runs only when a
message is heading for an SR, so the answer path never pays for it.

The rule that matters: a field the message doesn't state is *missing*, never
guessed. A wrong guess on an SR (the wrong environment, the wrong policy
number) is worse than one follow-up question, so anything unparseable or a
failed call degrades to "all fields missing" -> COLLECT_DETAILS.

`extract_follow_up` is the slot-filling variant: it reads only the user's
latest reply, with the original request as context (docs/plan-day-6.md), and
every value must appear in that reply (`_grounded`). Extracting from a joined
transcript instead made the model copy the opening sentence into a field.
"""

import json
import logging
import re
from dataclasses import dataclass, field

from querydesk.services.catalog import Category
from querydesk.services.llm.fireworks import FireworksError, chat_completion

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """Extract fields from an internal service desk message.

Category: {category_name}
Message: {query}

Fields to extract:
{field_list}

Rules:
- Copy each value as written in the message (an environment name, a time, a code, a reason).
- If the message does not explicitly state a field, use null. Never guess or infer a value.
- Reply with ONLY a JSON object with exactly these keys, and nothing else."""

FOLLOW_UP_PROMPT = """A user is answering a follow-up question from an internal service desk.

Category: {category_name}
Their original request (context only, do not copy values from it): {original_query}
Already collected (context only): {collected}
We asked them for: {asked_for}

Their reply: {reply}

Fields:
{field_list}

Rules:
- Fill a field ONLY with a value written in their reply, copied as written.
- If the reply gives a new value for an already-collected field, it is a correction: include it.
- Every field the reply does not explicitly state is null. Never guess or infer a value.
- Reply with ONLY a JSON object with exactly these keys, and nothing else."""

# Values a model sometimes writes instead of a real null.
_NULLISH = {"", "null", "none", "n/a", "na", "unknown", "not specified", "not provided"}

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Extraction:
    fields: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    # False when the LLM call failed or its output couldn't be parsed.
    ok: bool = True

    @property
    def complete(self) -> bool:
        return not self.missing


def _parse(reply: str) -> dict | None:
    text = _FENCE.sub("", reply.strip())
    match = _OBJECT.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _clean(value) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return None if text.lower() in _NULLISH else text


def _field_list(category: Category) -> str:
    return "\n".join(f"- {name}" for name in category.required_fields)


def _key(text: str) -> str:
    """Lowercase letters and digits only: "UAT-3" -> "uat3", "till 11 PM" -> "till11pm"."""
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _grounded(value: str, source: str) -> bool:
    """
    A value must appear in the reply it was extracted from (ignoring case,
    spacing and punctuation), so a sentence copied from the context, or a
    value carried over from earlier turns, counts as missing.
    """
    key = _key(value)
    return bool(key) and key in _key(source)


async def _extract(prompt: str, category: Category, ground_in: str | None = None) -> Extraction:
    """
    One extraction call. With `ground_in`, every value must appear in that
    text. Follow-ups use it; the first message doesn't: a categorical value
    there ("allow TCP 8443 inbound" -> change_type "firewall change") is a
    fair reading, and the eval measured that path without grounding.
    """
    required = category.required_fields
    try:
        reply = await chat_completion(
            messages=[{"role": "user", "content": prompt}], temperature=0.0
        )
    except FireworksError as e:
        logger.warning("Field extraction failed for %s: %s", category.id, e)
        return Extraction(missing=list(required), ok=False)

    data = _parse(reply)
    if data is None:
        logger.warning("Unparseable extraction for %s: %r", category.id, reply[:200])
        return Extraction(missing=list(required), ok=False)

    fields = {}
    for name in required:  # unknown keys in the reply are ignored
        value = _clean(data.get(name))
        if value is None:
            continue
        if ground_in is not None and not _grounded(value, ground_in):
            logger.info("Dropped ungrounded %s=%r for %s", name, value, category.id)
            continue
        fields[name] = value
    return Extraction(fields=fields, missing=[n for n in required if n not in fields])


async def extract_fields(query: str, category: Category) -> Extraction:
    if not category.required_fields:
        # Nothing to collect (e.g. password reset): complete without an LLM call.
        return Extraction()

    prompt = EXTRACTION_PROMPT.format(
        category_name=category.name,
        query=query,
        field_list=_field_list(category),
    )
    return await _extract(prompt, category)


async def extract_follow_up(
    original_query: str,
    reply: str,
    category: Category,
    collected: dict[str, str],
    missing: list[str],
) -> Extraction:
    """
    Fields stated in the user's reply to "I just need the …" (slot filling).

    Values come from the reply only — the original request and what was
    already collected are context, never a source — and are grounded against
    the reply. A field the reply restates (a correction, "it's UAT-3") is
    returned too; the caller decides how to merge. `missing` on the result is
    relative to the reply alone.
    """
    prompt = FOLLOW_UP_PROMPT.format(
        category_name=category.name,
        original_query=original_query,
        collected=json.dumps(collected) if collected else "nothing yet",
        asked_for=", ".join(missing),
        reply=reply,
        field_list=_field_list(category),
    )
    return await _extract(prompt, category, ground_in=reply)
