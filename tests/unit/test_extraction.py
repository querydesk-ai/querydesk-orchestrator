"""
Field extraction. Fireworks is mocked with respx, so these run offline and pin
down the parsing — which is where a model's formatting quirks would bite.
"""

import json

import httpx
import pytest
import respx

from querydesk.core.config import settings
from querydesk.services import catalog
from querydesk.services.decision import extraction
from querydesk.services.llm import fireworks as fw

CHAT_URL = f"{fw.FIREWORKS_BASE_URL}/chat/completions"


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setattr(settings, "fireworks_api_key", "test-key")


def _reply(content: str):
    return respx.post(CHAT_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    )


RESTART = catalog.get("it_application_restart")  # application, environment
UAT = catalog.get("it_uat_extension")  # environment, extend_until, reason


@pytest.mark.anyio
@respx.mock
async def test_complete_fields():
    _reply('{"application": "claims", "environment": "UAT"}')

    result = await extraction.extract_fields("restart the claims app in UAT", RESTART)

    assert result.ok and result.complete
    assert result.fields == {"application": "claims", "environment": "UAT"}


@pytest.mark.anyio
@respx.mock
async def test_null_fields_are_missing_in_required_order():
    _reply('{"environment": null, "extend_until": "11 PM", "reason": null}')

    result = await extraction.extract_fields("keep it up till 11 PM", UAT)

    assert result.fields == {"extend_until": "11 PM"}
    assert result.missing == ["environment", "reason"]
    assert not result.complete


@pytest.mark.anyio
@respx.mock
@pytest.mark.parametrize("value", ["", "  ", "N/A", "unknown", "None", "not specified"])
async def test_nullish_strings_count_as_missing(value):
    _reply(json.dumps({"application": "claims", "environment": value}))

    result = await extraction.extract_fields("restart claims", RESTART)

    assert result.missing == ["environment"]


@pytest.mark.anyio
@respx.mock
async def test_fenced_json_is_parsed():
    _reply('```json\n{"application": "claims", "environment": "UAT"}\n```')

    result = await extraction.extract_fields("restart claims in UAT", RESTART)

    assert result.ok and result.complete


@pytest.mark.anyio
@respx.mock
async def test_json_wrapped_in_prose_is_parsed():
    _reply('Here you go: {"application": "crm-web", "environment": "prod"} hope that helps')

    result = await extraction.extract_fields("bounce crm-web in prod", RESTART)

    assert result.fields == {"application": "crm-web", "environment": "prod"}


@pytest.mark.anyio
@respx.mock
async def test_unknown_keys_are_ignored():
    """Only categories.json's required fields can reach the SR."""
    _reply('{"application": "claims", "environment": "UAT", "priority": "P1"}')

    result = await extraction.extract_fields("P1: restart claims in UAT", RESTART)

    assert set(result.fields) == {"application", "environment"}


@pytest.mark.anyio
@respx.mock
async def test_non_string_values_are_stringified_and_nested_ones_dropped():
    _reply('{"application": 42, "environment": {"name": "UAT"}}')

    result = await extraction.extract_fields("restart app 42 in UAT", RESTART)

    assert result.fields == {"application": "42"}
    assert result.missing == ["environment"]


@pytest.mark.anyio
@respx.mock
@pytest.mark.parametrize("garbage", ["no json here", "{broken json", "[1, 2, 3]"])
async def test_unparseable_reply_means_all_missing_not_a_guess(garbage):
    _reply(garbage)

    result = await extraction.extract_fields("q", RESTART)

    assert not result.ok
    assert result.fields == {}
    assert result.missing == ["application", "environment"]


@pytest.mark.anyio
@respx.mock
async def test_fireworks_failure_degrades_to_all_missing():
    """A failed extraction asks the user; it must not 502 the whole message."""
    respx.post(CHAT_URL).mock(return_value=httpx.Response(500, text="boom"))

    result = await extraction.extract_fields("q", RESTART)

    assert not result.ok
    assert result.missing == ["application", "environment"]


@pytest.mark.anyio
@respx.mock
async def test_category_without_required_fields_skips_the_llm():
    route = respx.post(CHAT_URL)

    result = await extraction.extract_fields("unlock me", catalog.get("it_password_reset"))

    assert result.complete
    assert not route.called


@pytest.mark.anyio
@respx.mock
async def test_prompt_lists_the_categorys_fields_and_forbids_guessing():
    route = _reply('{"application": null, "environment": null}')

    await extraction.extract_fields("app is frozen pls restart", RESTART)

    prompt = json.loads(route.calls.last.request.content)["messages"][0]["content"]
    assert "- application" in prompt and "- environment" in prompt
    assert "app is frozen pls restart" in prompt
    assert "Never guess" in prompt
    assert json.loads(route.calls.last.request.content)["temperature"] == 0.0


# --- Grounding (follow-ups): a value must appear in the reply -------------------


def _follow_up(reply: str):
    return extraction.extract_follow_up(
        "keep my UAT up please", reply, UAT, collected={}, missing=list(UAT.required_fields)
    )


@pytest.mark.anyio
@respx.mock
async def test_an_ungrounded_follow_up_value_is_missing_not_kept():
    """The model inferred AM/PM and invented a reason: neither is in the reply."""
    _reply('{"environment": "UAT-2", "extend_until": "11 PM", "reason": "release testing"}')

    result = await _follow_up("UAT-2 till 11")

    assert result.fields == {"environment": "UAT-2"}
    assert result.missing == ["extend_until", "reason"]


@pytest.mark.anyio
@respx.mock
@pytest.mark.parametrize(
    ("reply", "value"),
    [("till 11pm", "11 PM"), ("it's uat 3", "UAT-3"), ("UAT-2, please", "uat-2")],
)
async def test_grounding_ignores_case_spacing_and_punctuation(reply, value):
    _reply(json.dumps({"environment": value, "extend_until": None, "reason": None}))

    result = await _follow_up(reply)

    assert result.fields == {"environment": value}


@pytest.mark.anyio
@respx.mock
async def test_first_message_keeps_a_categorical_reading():
    """Not grounded: "firewall change" is a fair reading of the request (eval act-13)."""
    dns = catalog.get("it_dns_firewall_change")
    _reply(
        '{"change_type": "firewall change", "target": "pas-web", "environment": "UAT",'
        ' "reason": "vendor integration tests"}'
    )

    result = await extraction.extract_fields(
        "allow TCP 8443 inbound to pas-web in UAT for vendor integration tests", dns
    )

    assert result.complete
    assert result.fields["change_type"] == "firewall change"


# --- Follow-up extraction (slot filling) ---------------------------------------


@pytest.mark.anyio
@respx.mock
async def test_follow_up_reads_only_the_reply():
    """The original request is context: copying a sentence from it is not an answer."""
    _reply(
        '{"environment": "UAT-3", "extend_until": null, "reason": "I need my UAT extended please"}'
    )

    result = await extraction.extract_follow_up(
        "I need my UAT extended please",
        "it's UAT-3",
        UAT,
        collected={"environment": "UAT"},
        missing=["extend_until", "reason"],
    )

    assert result.fields == {"environment": "UAT-3"}  # a correction, grounded in the reply


@pytest.mark.anyio
@respx.mock
async def test_follow_up_prompt_separates_context_from_the_reply():
    route = _reply('{"environment": null, "extend_until": "11 PM", "reason": null}')

    result = await extraction.extract_follow_up(
        "keep UAT-2 up for release testing",
        "till 11 PM",
        UAT,
        collected={"environment": "UAT-2", "reason": "release testing"},
        missing=["extend_until"],
    )

    assert result.fields == {"extend_until": "11 PM"}
    prompt = json.loads(route.calls.last.request.content)["messages"][0]["content"]
    assert "context only" in prompt
    assert "Their reply: till 11 PM" in prompt
    assert "We asked them for: extend_until" in prompt
    assert '"reason": "release testing"' in prompt
