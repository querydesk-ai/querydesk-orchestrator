"""
Knowledge-base text chunking. Deliberately dependency-free and pure: no
langchain, no tokeniser, no I/O — so it's fast to test and its behaviour is
fully determined by its arguments.

Chunks are sized in characters rather than tokens. For prose KB articles the
ratio is stable enough that a character budget keeps chunks comfortably inside
the embedding model's window, and it avoids pulling in a tokeniser just for
ingestion.
"""


def _split_oversized(paragraph: str, size: int, overlap: int) -> list[str]:
    """Hard-split a single paragraph that is itself longer than `size`."""
    step = max(1, size - overlap)
    return [paragraph[i : i + size] for i in range(0, len(paragraph), step)]


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    """
    Split `text` into chunks of at most `size` characters, carrying `overlap`
    characters of trailing context into each subsequent chunk.

    Paragraph-aware: blank-line-separated paragraphs are packed together up to
    the budget and never split mid-paragraph unless a single paragraph exceeds
    it. Keeping paragraphs intact matters for retrieval — a chunk cut mid
    sentence embeds poorly and reads badly when cited back to the user.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if overlap < 0:
        raise ValueError("overlap must not be negative")
    if overlap >= size:
        # Otherwise the overlap carry would never shrink and packing can't progress.
        raise ValueError("overlap must be smaller than size")

    paragraphs = [p.strip() for p in text.split("\n\n")]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) > size:
            # Flush what's buffered, then hard-split the long paragraph.
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_oversized(paragraph, size, overlap))
            continue

        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= size:
            current = candidate
        else:
            chunks.append(current)
            # Carry the tail of the finished chunk so context spans the seam.
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n\n{paragraph}" if tail else paragraph

    if current:
        chunks.append(current)

    return chunks
