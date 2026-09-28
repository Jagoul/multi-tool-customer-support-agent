"""The PreToolUse policy: refund limit with escalation redirect, and verified identity first."""

import pytest

from support_agent.policy import SupportPolicy, blocked_result
from support_agent.tools import ToolResult

REFUND = {"customer_id": "CUST-1001", "order_id": "ORD-1003", "reason": "changed_mind"}


@pytest.fixture
def policy() -> SupportPolicy:
    verified = SupportPolicy(refund_limit=500.0)
    verified.post_tool_use("get_customer", ToolResult({"customer_id": "CUST-1001"}, False))
    return verified


@pytest.mark.parametrize("tool_name", ["lookup_order", "escalate_to_human"])
def test_other_tools_are_not_intercepted(tool_name):
    assert SupportPolicy().pre_tool_use(tool_name, {"order_id": "ORD-1001"}).allowed


@pytest.mark.parametrize(
    "tool_input",
    [{"email": "Alice@Example.com"}, {"customer_id": "cust-1002"}],
)
def test_lookup_with_identifier_the_customer_wrote_is_allowed(tool_input):
    policy = SupportPolicy()
    policy.observe_customer_message("Hi, alice@example.com here. My ID is CUST-1002.")
    assert policy.pre_tool_use("get_customer", tool_input).allowed


@pytest.mark.parametrize(
    "tool_input",
    [
        {"email": "alice@example.com"},
        {"customer_id": "CUST-1001"},
    ],
    ids=["corrected-typo", "id-read-from-an-order"],
)
def test_lookup_with_identifier_the_customer_never_wrote_is_blocked(tool_input):
    policy = SupportPolicy()
    policy.observe_customer_message("My email is alice@exmaple.com, refund ORD-1002 please.")
    decision = policy.pre_tool_use("get_customer", tool_input)

    assert not decision.allowed
    assert decision.error.code == "IDENTIFIER_NOT_FROM_CUSTOMER"
    assert decision.error.category == "validation"
    assert "Never correct or guess it" in decision.error.description


def test_identifiers_from_any_earlier_customer_turn_count():
    policy = SupportPolicy()
    policy.observe_customer_message("My jacket hasn't arrived.")
    policy.observe_customer_message("Sure, it's bob@example.com")
    assert policy.pre_tool_use("get_customer", {"email": "bob@example.com"}).allowed


def test_refund_for_unverified_customer_is_blocked_without_escalation():
    decision = SupportPolicy().pre_tool_use("process_refund", {**REFUND, "amount": 50})
    assert not decision.allowed
    assert decision.error.code == "IDENTITY_NOT_VERIFIED"
    assert decision.error.category == "validation"
    assert decision.escalation_request is None


def test_failed_lookup_does_not_verify_the_customer():
    policy = SupportPolicy()
    policy.post_tool_use("get_customer", ToolResult({"isError": True}, True))
    assert not policy.verified_customers


@pytest.mark.parametrize("amount", [0.01, 129.99, 500.00])
def test_refund_at_or_below_limit_is_allowed(policy, amount):
    assert policy.pre_tool_use("process_refund", {**REFUND, "amount": amount}).allowed


def test_refund_above_limit_is_blocked_and_redirected_to_escalation(policy):
    decision = policy.pre_tool_use("process_refund", {**REFUND, "amount": 1499.00})

    assert not decision.allowed
    assert decision.error.category == "permission"
    assert decision.error.code == "REFUND_REQUIRES_HUMAN_APPROVAL"
    assert decision.error.is_retryable is False

    handoff = decision.escalation_request
    assert handoff["reason_category"] == "refund_above_threshold"
    assert handoff["customer_id"] == "CUST-1001"
    assert handoff["order_id"] == "ORD-1003"
    assert "$1,499.00" in handoff["summary"]
    assert handoff["recommended_action"]


def test_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("SUPPORT_AGENT_REFUND_LIMIT", "100")
    policy = SupportPolicy()
    policy.verified_customers.add("CUST-1001")
    assert not policy.pre_tool_use("process_refund", {**REFUND, "amount": 129.99}).allowed


def test_blocked_result_relays_the_escalation_ticket(policy):
    decision = policy.pre_tool_use("process_refund", {**REFUND, "amount": 1499.00})
    ticket = ToolResult(
        {"ticket_id": "ESC-5001", "expected_first_response": "within 4 hours"}, False
    )
    result = blocked_result(decision, ticket)

    assert result.is_error
    assert result.payload["escalation"]["ticket_id"] == "ESC-5001"
    assert "Do not call escalate_to_human again" in result.payload["description"]


def test_blocked_result_asks_the_agent_to_escalate_if_auto_escalation_failed(policy):
    decision = policy.pre_tool_use("process_refund", {**REFUND, "amount": 1499.00})
    failed = ToolResult({"isError": True, "errorCategory": "transient"}, True)
    result = blocked_result(decision, failed)
    assert "Call escalate_to_human yourself" in result.payload["description"]
