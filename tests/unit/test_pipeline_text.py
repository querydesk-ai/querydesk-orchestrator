"""Reply wording helpers in the pipeline."""

from querydesk.services.pipeline import _join, _readable


def test_fields_read_naturally():
    assert _readable("extend_until") == "extend until"
    assert _join(["application"]) == "application"
    assert _join(["application", "environment"]) == "application and environment"
    assert _join(["environment", "extend until", "reason"]) == (
        "environment, extend until and reason"
    )
