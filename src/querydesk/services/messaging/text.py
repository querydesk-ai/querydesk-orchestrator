"""
WhatsApp message text, whichever provider carries it: Markdown answers
rewritten in WhatsApp's formatting, cited sources listed, and long bodies
split to fit a provider's limit. No I/O.
"""

import re

_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__", re.DOTALL)
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
# A private-use character no answer contains, standing in for WhatsApp's bold
# marker while single-asterisk italics are rewritten.
_B = ""


def to_whatsapp(text: str) -> str:
    """
    Markdown to WhatsApp formatting: **bold** -> *bold*, *italic* -> _italic_,
    headings -> bold lines, [text](url) -> text (url). Lists and [1] citation
    markers read the same in both.
    """
    text = _HEADING.sub(lambda m: f"{_B}{m.group(1)}{_B}", text)
    text = _BOLD.sub(lambda m: f"{_B}{m.group(1) or m.group(2)}{_B}", text)
    text = _ITALIC.sub(r"_\1_", text)
    text = _LINK.sub(r"\1 (\2)", text)
    return text.replace(_B, "*")


def format_reply(result) -> str:
    """
    The WhatsApp text for a pipeline ChatResult: the reply, then the cited
    articles numbered as the answer's [n] markers are. The SR number is
    already in the reply text.
    """
    text = to_whatsapp(result.reply)
    if result.sources:
        lines = [f"[{i}] {s.title}" for i, s in enumerate(result.sources, start=1)]
        text += "\n\n_Sources:_\n" + "\n".join(lines)
    return text


def split_body(body: str, limit: int) -> list[str]:
    """Split at paragraph, then line, then word boundaries so no part exceeds `limit`."""
    body = body.strip()
    parts = []
    while len(body) > limit:
        window = body[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut <= 0:
            cut = limit
        parts.append(body[:cut].rstrip())
        body = body[cut:].lstrip()
    if body:
        parts.append(body)
    return parts
