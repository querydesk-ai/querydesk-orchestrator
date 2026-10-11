"""
Action / status signals. A table of phrasings independent of the eval set, so
the patterns aren't only ever tested against rows written alongside them.
"""

import pytest

from querydesk.eval.schema import Kind, load_eval_set
from querydesk.services.decision.signals import classify, has_request, is_question

ACTIONS = [
    # Imperatives
    "restart the claims app in UAT",
    "Bounce agent-portal in prod",
    "extend UAT-1 to 10 pm",
    "unlock my account",
    "whitelist 203.0.113.7 for the vendor in UAT",
    "need the uat env kept up tonight",
    # Polite asks, including question-shaped ones
    "please reset my password",
    "pls restart crm-web",
    "kindly onboard the new agent",
    "can you restart pas-web in UAT?",
    "Could you add a node to doc-render?",
    "can someone unlock my account?",
    "would you extend UAT-2 till midnight?",
    "I need a firewall port opened",
    "we need another server in pas-web",
    "I want to raise a commission dispute",
    "I'd like to change the nominee on policy 123",
    # Problem statements: not questions, so actions
    "I can't see my leads",
    "my policy still hasn't been issued",
    "the claims app is hung in UAT",
    "commission for August is missing for AG10234",
]

QUESTIONS = [
    "how long does a lockout last?",
    "what does PWD-1407 mean",
    "when is commission credited",
    "why do leads disappear after 3 days?",
    "is UAT data lost at shutdown",
    "does a rolling restart cause downtime",
    "do I need approval to restart in prod?",
    "do we need approval for a DNS change?",
    "can I restart my own service in UAT",
    "should I raise a request for a missing lead?",
    "which clusters have a node limit?",
    "any downtime during a rolling restart?",
    "the restart policy for production?",  # no question word, but ends in "?"
    # Polite information asks: they open like a request but want an answer.
    "I would like to know how commission is calculated",
    "I'd like to understand the UAT shutdown schedule",
    "I want to know why leads disappear",
    "can you tell me when commission is credited",
    "could you please explain the restart policy",
    "tell me how a password lockout works",
]

# A polite information ask that also asks for something is still an action.
MIXED = [
    "I'd like to know when commission is credited, and please raise a dispute for August",
    "can you tell me the policy and kindly reset my password",
]

STATUS = [
    "what's the status of my request?",
    "any update on SR-1042",
    "status of my requests",
    "where is my ticket for the UAT extension",
    "what happened to my request from yesterday?",
    "show my open requests",
    "sr 2201 progress?",
    "update on SR-1042?",
    "can you check the status of the request I raised yesterday",
    "what's the progress on SR-77",
]

NOT_STATUS = [
    "update the nominee on policy 40012345",
    "the commission statement is missing a policy",
    "restart the claims app in UAT",
    "what does PAS-4412 mean?",
    # An action on an existing request is not a status question.
    "Please close SR-1042, the issue is fixed",
    "close my open requests",
    "Can you update the ticket with the new policy number",
    # Informational questions that merely mention requests or statuses.
    "Where is the request form for VPN access?",
    "What does the status Pending Approval mean on a request?",
    "what is the status of a request after approval?",
]


@pytest.mark.parametrize("text", ACTIONS)
def test_actions_are_actions(text):
    assert classify(text).is_action


@pytest.mark.parametrize("text", QUESTIONS)
def test_questions_are_not_actions(text):
    assert not classify(text).is_action


@pytest.mark.parametrize("text", MIXED)
def test_an_info_ask_with_a_request_in_it_is_an_action(text):
    assert classify(text).is_action


@pytest.mark.parametrize("text", STATUS)
def test_status_questions_are_detected(text):
    assert classify(text).is_status


@pytest.mark.parametrize("text", NOT_STATUS)
def test_non_status_messages_are_not_status(text):
    assert not classify(text).is_status


def test_do_i_need_is_a_question_not_a_request():
    """'I need' is a request marker, except after 'do' — the trap in 'do I need approval?'."""
    assert not has_request("do I need approval to restart in prod?")
    assert has_request("I need approval to restart in prod")


def test_question_detection_handles_whitespace():
    assert is_question("  when is commission credited?  ")


def test_signals_agree_with_every_eval_label():
    """Every action row reads as an action; no informational row does."""
    for row in load_eval_set():
        intent = classify(row.query)
        if row.kind is Kind.ACTION:
            assert intent.is_action, row.id
        if row.kind is Kind.INFORMATIONAL:
            assert not intent.is_action, row.id
