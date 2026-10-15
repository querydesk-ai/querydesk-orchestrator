"""
Jev client. JevStation is mocked with respx: these spend no credits and need
no key beyond a dummy one.
"""

import json

import httpx
import pytest
import respx

from querydesk.core.config import settings
from querydesk.core.errors import JevError, UpstreamError
from querydesk.services.llm import jev

URL = f"{settings.jev_base_url}/systemone"


@pytest.fixture(autouse=True)
def dummy_key(monkeypatch):
    monkeypatch.setattr(settings, "typesafe_api_key", "sk_test")


def _ok(answers: dict, charged: int = 3) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "code": 0,
            "message": "ok",
            "data": {
                "model": "jev-1.13.0",
                "answers": answers,
                "credits": {"charged": charged, "remaining": 40},
                "latency_ms": 3000,
            },
        },
    )


def _choice(label: str) -> dict:
    return {"type": "choice", "choice": label, "probabilities": {label: 0.9}, "confidence": 0.8}


def test_one_choice_question_per_message_in_a_single_request():
    request = jev.build_intent_request(["reset my password", "what is the payout date?"])

    assert request["state"]["messages"] == {
        "m1": "reset my password",
        "m2": "what is the payout date?",
    }
    assert list(request["questions"]) == ["m1", "m2"]
    for mid, q in request["questions"].items():
        assert q["type"] == "choice"
        assert mid in q["instructions"]
        assert set(q["criteria"]) == {"action", "status", "information", "out_of_scope"}


def test_more_messages_than_jevstation_allows_is_refused():
    with pytest.raises(ValueError):
        jev.build_intent_request(["x"] * (jev.MAX_QUESTIONS + 1))


@pytest.mark.anyio
@respx.mock
async def test_answers_come_back_in_message_order_with_auth():
    route = respx.post(URL).mock(
        return_value=_ok({"m2": _choice("information"), "m1": _choice("action")})
    )

    intents = await jev.classify_intents(["unlock my account", "how do payouts work?"])

    assert [i.label for i in intents] == ["action", "information"]
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer sk_test"
    assert json.loads(sent.content)["model"] == settings.jev_model


def test_labels_map_onto_the_rule_engine_intent():
    assert jev.JevIntent("action", {}).as_intent().is_action
    status = jev.JevIntent("status", {}).as_intent()
    assert status.is_status and not status.is_action
    for label in ("information", "out_of_scope"):
        intent = jev.JevIntent(label, {}).as_intent()
        assert not intent.is_action and not intent.is_status


@pytest.mark.anyio
@respx.mock
async def test_credits_are_reported():
    respx.post(URL).mock(return_value=_ok({"m1": _choice("action")}, charged=1))

    evaluation = await jev.evaluate(jev.build_intent_request(["unlock my account"]))

    assert evaluation.credits_charged == 1
    assert evaluation.credits_remaining == 40


@pytest.mark.anyio
@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"detail": "bad key"}),
        httpx.Response(200, json={"code": -1, "message": "boom", "data": {"answers": {}}}),
        httpx.Response(200, text="not json"),
    ],
)
async def test_failures_raise_an_upstream_error(response):
    respx.post(URL).mock(return_value=response)

    with pytest.raises(UpstreamError):
        await jev.classify_intents(["unlock my account"])


@pytest.mark.anyio
@respx.mock
async def test_a_missing_or_unknown_answer_is_an_error_not_a_guess():
    respx.post(URL).mock(return_value=_ok({"m1": {"type": "choice", "choice": "maybe"}}))

    with pytest.raises(JevError):
        await jev.classify_intents(["unlock my account", "second"])


@pytest.mark.anyio
async def test_no_key_fails_before_any_request(monkeypatch):
    monkeypatch.setattr(settings, "typesafe_api_key", "")

    with pytest.raises(JevError):
        await jev.classify_intents(["unlock my account"])
