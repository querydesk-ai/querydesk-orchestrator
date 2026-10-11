"""
Required-field extraction for the SR path.

One LLM call pulls the category's required fields (from categories.json, via
the catalog — never hardcoded) out of the user's message. It runs only when a
message is heading for an SR, so the answer path never pays for it.

The rule that matters: a field the message doesn't state is *missing*, never
guessed. A wrong guess on an SR (the wrong environment, the wrong policy
number) is worse than one follow-up question, so anything unparseable or a
failed call degrades to "all fields missing" -> COLLECT_DETAILS.
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


async def extract_fields(query: str, category: Category) -> Extraction:
    required = category.required_fields
    if not required:
        # Nothing to collect (e.g. password reset): complete without an LLM call.
        return Extraction()

    prompt = EXTRACTION_PROMPT.format(
        category_name=category.name,
        query=query,
        field_list="\n".join(f"- {name}" for name in required),
    )
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
        if value is not None:
            fields[name] = value
    return Extraction(fields=fields, missing=[n for n in required if n not in fields])
