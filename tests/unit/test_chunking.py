import pytest

from querydesk.services.retrieval.chunking import chunk_text


def test_empty_and_whitespace_input_yields_no_chunks():
    assert chunk_text("", 100, 10) == []
    assert chunk_text("   \n\n  \n\n ", 100, 10) == []


def test_short_text_is_a_single_chunk():
    assert chunk_text("One short paragraph.", 100, 10) == ["One short paragraph."]


def test_paragraphs_are_packed_up_to_the_budget():
    text = "\n\n".join(["a" * 30, "b" * 30, "c" * 30])
    chunks = chunk_text(text, 70, 10)

    # 30 + 2 (separator) + 30 = 62 fits; adding the third would reach 94.
    assert len(chunks) == 2
    assert chunks[0] == f"{'a' * 30}\n\n{'b' * 30}"
    assert all(len(c) <= 70 for c in chunks)


def test_paragraphs_are_never_split_mid_paragraph_when_they_fit():
    text = "\n\n".join(["x" * 40, "y" * 40])
    chunks = chunk_text(text, 50, 5)

    assert len(chunks) == 2
    assert "x" * 40 in chunks[0]
    # The second chunk carries overlap, but the whole paragraph survives intact.
    assert "y" * 40 in chunks[1]


def test_overlap_carries_tail_context_across_the_seam():
    text = "\n\n".join(["a" * 40, "b" * 40])
    chunks = chunk_text(text, 50, 10)

    assert chunks[1].startswith("a" * 10)


def test_zero_overlap_carries_nothing():
    text = "\n\n".join(["a" * 40, "b" * 40])
    chunks = chunk_text(text, 50, 0)

    assert chunks == ["a" * 40, "b" * 40]


def test_oversized_paragraph_is_hard_split_within_budget():
    chunks = chunk_text("z" * 250, 100, 20)

    assert all(len(c) <= 100 for c in chunks)
    # Every character survives somewhere.
    assert "".join(dict.fromkeys("".join(chunks))) == "z"
    assert len(chunks) > 1


def test_oversized_paragraph_flushes_the_buffer_first():
    text = "\n\n".join(["short para", "L" * 200])
    chunks = chunk_text(text, 100, 10)

    assert chunks[0] == "short para"
    assert all(len(c) <= 100 for c in chunks)


@pytest.mark.parametrize(
    "size,overlap",
    [(0, 0), (-1, 0), (100, -1), (100, 100), (100, 150)],
)
def test_invalid_arguments_are_rejected(size, overlap):
    # overlap >= size would make packing unable to progress; fail loudly
    # rather than loop or silently misbehave.
    with pytest.raises(ValueError):
        chunk_text("some text", size, overlap)
