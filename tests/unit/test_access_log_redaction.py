"""uvicorn's access line must not carry the query string (Meta's verify token)."""

import logging

from querydesk.main import _RedactQueryString


def _record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", path, "1.1", 200), None,
    )  # fmt: skip


def test_query_string_is_redacted():
    r = _record("/webhooks/whatsapp?hub.verify_token=s3cret&hub.challenge=1")

    assert _RedactQueryString().filter(r) is True
    assert "s3cret" not in r.getMessage()
    assert "/webhooks/whatsapp?[redacted]" in r.getMessage()


def test_a_path_without_a_query_is_untouched():
    r = _record("/health")

    _RedactQueryString().filter(r)

    assert "/health HTTP" in r.getMessage()
