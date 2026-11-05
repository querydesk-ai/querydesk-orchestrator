import pytest

from querydesk.services import catalog
from querydesk.services.decision import smalltalk
from querydesk.services.decision.smalltalk import Kind


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("hi", Kind.GREETING),
        ("Hello!", Kind.GREETING),
        ("hey there", Kind.GREETING),
        ("good morning", Kind.GREETING),
        ("how are you?", Kind.HOW_ARE_YOU),
        ("hi, how are you", Kind.HOW_ARE_YOU),
        ("thanks", Kind.THANKS),
        ("Thank you!", Kind.THANKS),
        ("ok thanks", Kind.THANKS),
        ("bye", Kind.GOODBYE),
        ("that's all", Kind.GOODBYE),
        ("what can you do?", Kind.CAPABILITIES),
        ("who are you", Kind.CAPABILITIES),
        ("help", Kind.CAPABILITIES),
        ("ok", Kind.ACK),
        ("hmm", Kind.ACK),
        ("yes", Kind.ACK),
        ("no", Kind.ACK),
    ],
)
def test_small_talk_is_recognised(text, kind):
    assert smalltalk.classify(text) is kind


@pytest.mark.parametrize(
    "text",
    [
        "hi, my password reset is not working",
        "hello can you restart the claims app in UAT",
        "thanks, but I also need UAT extended",
        "help me reset my password",
        "how do I reset my password?",
        "what can you do about a delayed policy issuance?",
        "ok restart it in prod",
        "no, make it UAT-3",
        "does a rolling restart cause downtime?",
        "what is the capital of Peru?",
        "",
    ],
)
def test_a_real_message_is_never_small_talk(text):
    assert smalltalk.classify(text) is None


def test_every_kind_has_a_reply():
    for kind in Kind:
        assert smalltalk.reply(kind).strip()


def test_capabilities_reply_lists_the_catalog_not_a_hardcoded_list():
    reply = smalltalk.reply(Kind.CAPABILITIES)

    for category in catalog.all_categories().values():
        assert category.name in reply
