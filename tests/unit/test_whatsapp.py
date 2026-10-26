"""
WhatsApp messaging: the pseudonymous user id, reply text, and both
providers' signatures, parsing and sending (Meta and Twilio mocked with respx).
"""

import hashlib
import hmac
import json

import httpx
import pytest
import respx

from querydesk.core.config import settings
from querydesk.core.errors import WhatsAppError
from querydesk.models.schemas import Decision, ResolutionType, Source
from querydesk.services.messaging import meta, text, twilio, whatsapp
from querydesk.services.pipeline import ChatResult

PHONE = "+15551234567"

# --- Identity -------------------------------------------------------------


@pytest.fixture
def id_secret(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_id_secret", "id-secret")


@pytest.mark.parametrize("raw", ["whatsapp:+15551234567", "15551234567", "+1 555-123-4567"])
def test_both_providers_numbers_normalise_to_e164(raw):
    assert whatsapp.normalize_phone(raw) == PHONE


def test_user_id_is_stable_and_hides_the_number(id_secret, monkeypatch):
    uid = whatsapp.user_id_for(PHONE)

    assert uid == whatsapp.user_id_for(PHONE)
    assert uid.startswith("wa-") and len(uid) == 19
    assert "5551234567" not in uid
    assert whatsapp.is_whatsapp_user(uid)
    assert uid != whatsapp.user_id_for("+15557654321")

    monkeypatch.setattr(settings, "whatsapp_id_secret", "another-secret")
    assert whatsapp.user_id_for(PHONE) != uid


def test_chat_page_users_are_not_whatsapp_users():
    assert not whatsapp.is_whatsapp_user("sso12345")


@pytest.mark.anyio
@pytest.mark.parametrize("provider", ["meta", "twilio"])
async def test_send_goes_through_the_configured_provider(monkeypatch, provider):
    sent = []

    async def fake(phone, body):
        sent.append((provider, phone, body))

    monkeypatch.setattr(meta, "send", fake)
    monkeypatch.setattr(twilio, "send", fake)
    monkeypatch.setattr(settings, "whatsapp_provider", provider)

    await whatsapp.send(PHONE, "hi")

    assert sent == [(provider, PHONE, "hi")]


@pytest.mark.anyio
async def test_an_unknown_provider_fails_clearly(monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_provider", "carrier-pigeon")

    with pytest.raises(WhatsAppError, match="carrier-pigeon"):
        await whatsapp.send(PHONE, "hi")


# --- Reply text -----------------------------------------------------------


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("clears after **30 minutes** [1].", "clears after *30 minutes* [1]."),
        ("this is *not* automatic", "this is _not_ automatic"),
        ("__very__ important", "*very* important"),
        ("## Steps\n- one\n- two", "*Steps*\n- one\n- two"),
        ("see [the portal](https://kb.internal/x)", "see the portal (https://kb.internal/x)"),
        ("2 * 3 * 4 stays", "2 * 3 * 4 stays"),
        ("**bold** then *italic*", "*bold* then _italic_"),
    ],
)
def test_markdown_becomes_whatsapp_formatting(markdown, expected):
    assert text.to_whatsapp(markdown) == expected


def _result(reply: str, sources=()) -> ChatResult:
    return ChatResult(
        reply=reply,
        resolution_type=ResolutionType.RAG,
        decision=Decision.ANSWER,
        sources=list(sources),
    )


def _source(title: str) -> Source:
    return Source(title=title, source_path="x.md", snippet="…", score=0.7)


def test_an_answer_lists_its_sources_numbered_like_the_citations():
    result = _result(
        "Lockouts clear after **30 minutes** [1].",
        [_source("Password reset"), _source("Account policy")],
    )

    assert text.format_reply(result) == (
        "Lockouts clear after *30 minutes* [1].\n\n"
        "_Sources:_\n[1] Password reset\n[2] Account policy"
    )


def test_a_reply_without_sources_is_just_the_reply():
    assert text.format_reply(_result("I've raised SR 1042.")) == "I've raised SR 1042."


def test_a_short_body_is_one_part():
    assert text.split_body("hello", 100) == ["hello"]


def test_a_long_body_splits_at_boundaries_within_the_limit():
    body = ("word " * 30).strip() + "\n\n" + ("more " * 30).strip()

    parts = text.split_body(body, 100)

    assert all(len(p) <= 100 for p in parts)
    assert " ".join(parts).split() == body.split()


def test_a_body_with_no_spaces_is_cut_hard():
    assert text.split_body("x" * 250, 100) == ["x" * 100, "x" * 100, "x" * 50]


# --- Meta -----------------------------------------------------------------

PHONE_ID = "1234567890"
META_URL = f"{meta.GRAPH}/v23.0/{PHONE_ID}/messages"


@pytest.fixture
def meta_on(monkeypatch):
    monkeypatch.setattr(settings, "meta_access_token", "EAAtoken")
    monkeypatch.setattr(settings, "meta_phone_number_id", PHONE_ID)
    monkeypatch.setattr(settings, "meta_app_secret", "app-secret")
    monkeypatch.setattr(settings, "meta_verify_token", "verify-me")
    monkeypatch.setattr(settings, "meta_graph_version", "v23.0")


def test_meta_signature_is_hmac_sha256_of_the_raw_body(meta_on):
    body = json.dumps({"object": "whatsapp_business_account"}).encode()
    # Computed independently of meta.py.
    good = "sha256=9a219548a7618abb8c7794fbe342602df27384423f654e62961fd8999441b84e"

    assert meta.verify_signature(body, good)
    assert not meta.verify_signature(body + b" ", good)
    assert not meta.verify_signature(body, good.removeprefix("sha256="))
    assert not meta.verify_signature(body, None)


def test_meta_signature_needs_an_app_secret(meta_on, monkeypatch):
    body = b"{}"
    sig = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    monkeypatch.setattr(settings, "meta_app_secret", "")

    assert not meta.verify_signature(body, sig)


def test_meta_subscription_check(meta_on):
    assert meta.verify_subscription("subscribe", "verify-me")
    assert not meta.verify_subscription("subscribe", "wrong")
    assert not meta.verify_subscription("unsubscribe", "verify-me")
    assert not meta.verify_subscription("subscribe", None)


def _webhook(messages=(), statuses=()) -> dict:
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": PHONE_ID}}
    if messages:
        value["contacts"] = [{"profile": {"name": "Sam"}, "wa_id": "15551234567"}]
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = list(statuses)
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}],
    }


def test_meta_text_and_media_messages_are_parsed():
    payload = _webhook(
        [
            {"from": "15551234567", "id": "wamid.1", "type": "text", "text": {"body": "hi"}},
            {"from": "15551234567", "id": "wamid.2", "type": "image", "image": {"id": "m1"}},
        ]
    )

    messages, failures = meta.parse(payload)

    assert messages == [
        meta.Inbound(PHONE, "wamid.1", "hi", False),
        meta.Inbound(PHONE, "wamid.2", "", True),
    ]
    assert failures == []


def test_meta_receipts_are_ignored_and_failures_reported():
    payload = _webhook(
        statuses=[
            {"id": "wamid.9", "status": "read"},
            {
                "id": "wamid.10",
                "status": "failed",
                "errors": [{"code": 131047, "title": "Re-engagement message"}],
            },
        ]
    )

    messages, failures = meta.parse(payload)

    assert messages == []
    assert failures == ["message wamid.10 failed: 131047 Re-engagement message"]


def test_meta_parse_survives_odd_payloads():
    assert meta.parse({}) == ([], [])
    assert meta.parse({"entry": [{"changes": [{"field": "account_update", "value": {}}]}]}) == (
        [],
        [],
    )


@pytest.mark.anyio
@respx.mock
async def test_meta_send_posts_text_to_the_number_without_plus(meta_on):
    route = respx.post(META_URL).mock(return_value=httpx.Response(200, json={"messages": []}))

    await meta.send(PHONE, "a" * 4000 + " " + "b" * 200)

    assert route.call_count == 2
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer EAAtoken"
    sent = json.loads(req.content)
    assert sent["to"] == "15551234567"
    assert sent["messaging_product"] == "whatsapp" and sent["type"] == "text"


@pytest.mark.anyio
@respx.mock
@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (500, 1, True),
        (429, 4, True),
        (400, 130429, True),  # throttled
        (401, 190, False),  # expired token
        (400, 131030, False),  # not on the test number's recipient list
    ],
)
async def test_a_meta_error_says_whether_to_retry(meta_on, status, code, retryable):
    respx.post(META_URL).mock(
        return_value=httpx.Response(status, json={"error": {"code": code, "message": "x"}})
    )

    with pytest.raises(WhatsAppError) as err:
        await meta.send(PHONE, "hi")

    assert err.value.retryable is retryable


@pytest.mark.anyio
@respx.mock
async def test_meta_unreachable_is_retryable(meta_on):
    respx.post(META_URL).mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(WhatsAppError) as err:
        await meta.send(PHONE, "hi")

    assert err.value.retryable


@pytest.mark.anyio
async def test_meta_send_without_credentials_fails_clearly():
    with pytest.raises(WhatsAppError, match="not configured"):
        await meta.send(PHONE, "hi")


# --- Twilio ---------------------------------------------------------------

SID = "AC00000000000000000000000000000000"
TWILIO_URL = f"{twilio.API}/Accounts/{SID}/Messages.json"

# Twilio's documented example, and a WhatsApp-shaped one; both signatures
# were computed with Twilio's own RequestValidator (twilio==9.3.7).
DOC_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
DOC_PARAMS = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+12349013030",
    "Digits": "1234",
    "From": "+12349013030",
    "To": "+18005551212",
}
DOC_SIGNATURE = "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
WA_URL = "https://bot.example.com/webhooks/whatsapp"
WA_PARAMS = {
    "From": "whatsapp:+15551234567",
    "To": "whatsapp:+14155238886",
    "Body": "hi there",
    "MessageSid": "SM123",
    "NumMedia": "0",
}
WA_SIGNATURE = "eony0Fe5t7zQEEd9Ktsx20OS2/U="


@pytest.fixture
def twilio_on(monkeypatch):
    monkeypatch.setattr(settings, "twilio_account_sid", SID)
    monkeypatch.setattr(settings, "twilio_auth_token", "12345")
    monkeypatch.setattr(settings, "twilio_whatsapp_from", "whatsapp:+14155238886")


@pytest.mark.parametrize(
    ("url", "params", "expected"),
    [(DOC_URL, DOC_PARAMS, DOC_SIGNATURE), (WA_URL, WA_PARAMS, WA_SIGNATURE)],
)
def test_twilio_signature_matches_twilios_validator(twilio_on, url, params, expected):
    assert twilio.compute_signature(url, params.items()) == expected
    assert twilio.verify_signature(url, params.items(), expected)


def test_twilio_parameter_order_does_not_matter(twilio_on):
    shuffled = list(reversed(WA_PARAMS.items()))
    assert twilio.verify_signature(WA_URL, shuffled, WA_SIGNATURE)


@pytest.mark.parametrize(
    ("url", "params"),
    [
        (WA_URL, {**WA_PARAMS, "Body": "hi there!"}),  # tampered body
        (WA_URL + "/", WA_PARAMS),  # another URL
        ("http://bot.example.com/webhooks/whatsapp", WA_PARAMS),  # scheme
    ],
)
def test_any_change_breaks_the_twilio_signature(twilio_on, url, params):
    assert not twilio.verify_signature(url, params.items(), WA_SIGNATURE)


def test_twilio_no_token_or_no_signature_rejects(twilio_on, monkeypatch):
    assert not twilio.verify_signature(WA_URL, WA_PARAMS.items(), None)
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    assert not twilio.verify_signature(WA_URL, WA_PARAMS.items(), WA_SIGNATURE)


@pytest.mark.anyio
@respx.mock
async def test_twilio_send_posts_one_form_per_part(twilio_on):
    route = respx.post(TWILIO_URL).mock(return_value=httpx.Response(201, json={"sid": "SM1"}))

    await twilio.send(PHONE, "a" * 1000 + " " + "b" * 1000)

    assert route.call_count == 2
    first = route.calls[0].request
    form = dict(x.split("=", 1) for x in first.content.decode().split("&"))
    assert form["From"] == "whatsapp%3A%2B14155238886"
    assert form["To"] == "whatsapp%3A%2B15551234567"
    assert first.headers["Authorization"].startswith("Basic ")


@pytest.mark.anyio
@respx.mock
@pytest.mark.parametrize(("status", "retryable"), [(500, True), (429, True), (400, False)])
async def test_a_twilio_error_says_whether_to_retry(twilio_on, status, retryable):
    respx.post(TWILIO_URL).mock(return_value=httpx.Response(status, json={"code": 21211}))

    with pytest.raises(WhatsAppError) as err:
        await twilio.send(PHONE, "hi")

    assert err.value.retryable is retryable


@pytest.mark.anyio
async def test_twilio_send_without_credentials_fails_clearly():
    with pytest.raises(WhatsAppError, match="not configured"):
        await twilio.send(PHONE, "hi")
