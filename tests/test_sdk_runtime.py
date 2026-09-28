"""Claude Agent SDK wiring: in-process MCP tools and the PreToolUse hook (no model calls)."""

import asyncio
import json

from support_agent.sdk_agent import SdkSupportRuntime, qualified


def test_options_expose_only_the_support_tools_and_register_the_hook():
    options = SdkSupportRuntime().options(model="claude-opus-5")

    assert options.tools == []
    assert options.allowed_tools == [
        qualified(n) for n in ("get_customer", "lookup_order", "process_refund", "escalate_to_human")
    ]
    (matcher,) = options.hooks["PreToolUse"]
    assert matcher.matcher == "mcp__support__get_customer|mcp__support__process_refund"
    assert options.setting_sources == []
    assert options.mcp_servers["support"]["type"] == "sdk"


def test_in_process_tool_returns_structured_errors():
    runtime = SdkSupportRuntime()
    lookup = next(t for t in _sdk_tools(runtime) if t.name == "lookup_order")

    response = asyncio.run(lookup.handler({"order_id": "ORD-1004"}))

    assert response["is_error"] is True
    assert json.loads(response["content"][0]["text"])["errorCategory"] == "transient"


def test_pre_tool_use_hook_denies_large_refund_and_escalates():
    runtime = SdkSupportRuntime()
    runtime.run_tool("get_customer", {"email": "alice@example.com"})

    output = asyncio.run(runtime.pre_tool_use_hook(
        {
            "tool_name": "mcp__support__process_refund",
            "tool_input": {"customer_id": "CUST-1001", "order_id": "ORD-1003", "amount": 1499.0,
                           "reason": "changed_mind"},
        },
        "toolu_1",
        {"signal": None},
    ))

    decision = output["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse"
    assert decision["permissionDecision"] == "deny"
    reason = json.loads(decision["permissionDecisionReason"])
    assert reason["errorCode"] == "REFUND_REQUIRES_HUMAN_APPROVAL"
    assert reason["escalation"]["ticket_id"] == "ESC-5001"
    assert runtime.backend.refunds == []
    assert runtime.backend.tickets[0]["reason_category"] == "refund_above_threshold"


def test_pre_tool_use_hook_denies_lookup_with_an_identifier_the_customer_never_gave():
    runtime = SdkSupportRuntime()
    runtime.policy.observe_customer_message("my email is alice@exmaple.com")

    output = asyncio.run(runtime.pre_tool_use_hook(
        {"tool_name": "mcp__support__get_customer", "tool_input": {"email": "alice@example.com"}},
        "toolu_3",
        {"signal": None},
    ))

    decision = output["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert json.loads(decision["permissionDecisionReason"])["errorCode"] == "IDENTIFIER_NOT_FROM_CUSTOMER"


def test_pre_tool_use_hook_allows_refund_within_policy():
    runtime = SdkSupportRuntime()
    runtime.run_tool("get_customer", {"email": "alice@example.com"})

    output = asyncio.run(runtime.pre_tool_use_hook(
        {
            "tool_name": "mcp__support__process_refund",
            "tool_input": {"customer_id": "CUST-1001", "order_id": "ORD-1001", "amount": 129.99,
                           "reason": "duplicate_charge"},
        },
        "toolu_2",
        {"signal": None},
    ))

    assert output == {}


def _sdk_tools(runtime: SdkSupportRuntime):
    from support_agent.tools import TOOL_DEFINITIONS

    return [runtime._sdk_tool(definition) for definition in TOOL_DEFINITIONS]
