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

    result = await extraction.extract_fields("keep it up till 11", UAT)

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

    result = await extraction.extract_fields("q", RESTART)

    assert result.ok and result.complete


@pytest.mark.anyio
@respx.mock
async def test_json_wrapped_in_prose_is_parsed():
    _reply('Here you go: {"application": "crm-web", "environment": "prod"} hope that helps')

    result = await extraction.extract_fields("q", RESTART)

    assert result.fields == {"application": "crm-web", "environment": "prod"}


@pytest.mark.anyio
@respx.mock
async def test_unknown_keys_are_ignored():
    """Only categories.json's required fields can reach the SR."""
    _reply('{"application": "claims", "environment": "UAT", "priority": "P1"}')

    result = await extraction.extract_fields("q", RESTART)

    assert set(result.fields) == {"application", "environment"}


@pytest.mark.anyio
@respx.mock
async def test_non_string_values_are_stringified_and_nested_ones_dropped():
    _reply('{"application": 42, "environment": {"name": "UAT"}}')

    result = await extraction.extract_fields("q", RESTART)

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
