"""The manual agentic loop, driven by scripted Claude responses against the real MCP server.

Scripting the model makes stop_reason routing, tool-result plumbing, and hook enforcement
deterministic. Model judgment (decomposition, retries, wording) is covered by the live scenarios.
"""

import asyncio
import json

from conftest import ScriptedClaude, reply, text, tool_use

from support_agent.agent import (
    FALLBACK_BETA,
    ITERATION_CAP_MESSAGE,
    REFUSAL_MESSAGE,
    AgentConfig,
    SupportAgent,
    connect_mcp_tools,
)
from support_agent.prompts import SYSTEM_PROMPT


def converse(claude: ScriptedClaude, message: str, config: AgentConfig | None = None):
    """Run one customer turn; return the result and a post-turn order lookup helper."""

    async def scenario():
        async with connect_mcp_tools() as executor:
            agent = SupportAgent(claude, executor, executor.tools, config=config)
            result = await agent.send(message)
            ord_1003 = await executor.call_tool("lookup_order", {"order_id": "ORD-1003"})
            return agent, result, ord_1003

    return asyncio.run(scenario())


def tool_results_in(message: dict) -> list[dict]:
    assert message["role"] == "user"
    return [block for block in message["content"] if block["type"] == "tool_result"]


def test_end_turn_returns_the_answer_without_running_tools():
    claude = ScriptedClaude(reply("end_turn", text("Happy to help! What's your order number?")))
    _, result, _ = converse(claude, "Hi there")

    assert result.stop_reason == "end_turn"
    assert result.text == "Happy to help! What's your order number?"
    assert result.iterations == 1
    assert result.tool_calls == []
    assert len(claude.requests) == 1


def test_request_carries_system_prompt_mcp_tools_fallbacks_and_caching():
    claude = ScriptedClaude(reply("end_turn", text("Hello")))
    converse(claude, "Hi")

    request = claude.requests[0]
    assert request["system"] == SYSTEM_PROMPT
    assert [t["name"] for t in request["tools"]] == [
        "get_customer", "lookup_order", "process_refund", "escalate_to_human"
    ]
    assert request["fallbacks"] == "default"
    assert request["betas"] == [FALLBACK_BETA]
    assert request["cache_control"] == {"type": "ephemeral"}


def test_tool_use_runs_the_tool_and_loops_until_end_turn():
    call = tool_use("get_customer", email="alice@example.com")
    claude = ScriptedClaude(
        reply("tool_use", text("Let me look you up."), call),
        reply("end_turn", text("Found your account, Alice.")),
    )
    agent, result, _ = converse(claude, "I'm alice@example.com")

    assert result.stop_reason == "end_turn"
    assert result.iterations == 2
    assert [c.name for c in result.tool_calls] == ["get_customer"]

    second_request = claude.requests[1]["messages"]
    assert second_request[1]["role"] == "assistant"
    assert second_request[1]["content"][1].type == "tool_use"
    (tool_result,) = tool_results_in(second_request[2])
    assert tool_result["tool_use_id"] == call.id
    assert tool_result["is_error"] is False
    assert json.loads(tool_result["content"])["customer_id"] == "CUST-1001"
    assert agent.messages[-1]["role"] == "assistant"


def test_parallel_tool_calls_get_all_results_in_one_user_message():
    calls = [tool_use("lookup_order", order_id=f"ORD-100{n}") for n in (1, 2, 3)]
    claude = ScriptedClaude(reply("tool_use", *calls), reply("end_turn", text("Done.")))
    _, result, _ = converse(claude, "Check ORD-1001, ORD-1002 and ORD-1003")

    results = tool_results_in(claude.requests[1]["messages"][-1])
    assert [r["tool_use_id"] for r in results] == [c.id for c in calls]
    assert len(result.tool_calls) == 3


def test_transient_error_reaches_claude_structured_and_the_retry_succeeds():
    first, retry = tool_use("lookup_order", order_id="ORD-1004"), tool_use("lookup_order", order_id="ORD-1004")
    claude = ScriptedClaude(
        reply("tool_use", first),
        reply("tool_use", retry),
        reply("end_turn", text("Your return window closed 15 days ago.")),
    )
    _, result, _ = converse(claude, "Refund ORD-1004?")

    failed = json.loads(tool_results_in(claude.requests[1]["messages"][-1])[0]["content"])
    assert failed["errorCategory"] == "transient"
    assert failed["isRetryable"] is True
    assert result.tool_calls[0].result.is_error
    assert not result.tool_calls[1].result.is_error


def test_small_refund_passes_the_hook_after_identity_is_verified():
    claude = ScriptedClaude(
        reply("tool_use", tool_use("get_customer", email="alice@example.com")),
        reply("tool_use", tool_use("process_refund", customer_id="CUST-1001", order_id="ORD-1001",
                                   amount=129.99, reason="duplicate_charge")),
        reply("end_turn", text("Refunded.")),
    )
    _, result, _ = converse(claude, "alice@example.com here, double charge on ORD-1001")

    refund = result.calls_to("process_refund")[0]
    assert not refund.blocked_by_hook
    assert refund.result.payload["refund_id"].startswith("RF-")


def test_hook_blocks_refund_above_limit_and_auto_escalates():
    claude = ScriptedClaude(
        reply("tool_use", tool_use("get_customer", email="alice@example.com")),
        reply("tool_use", tool_use("process_refund", customer_id="CUST-1001", order_id="ORD-1003",
                                   amount=1499.00, reason="changed_mind")),
        reply("end_turn", text("A specialist will review your laptop refund.")),
    )
    _, result, ord_1003_after = converse(claude, "alice@example.com: refund my laptop ORD-1003")

    refund = result.calls_to("process_refund")[0]
    assert refund.blocked_by_hook
    assert refund.result.payload["errorCategory"] == "permission"
    assert refund.result.payload["errorCode"] == "REFUND_REQUIRES_HUMAN_APPROVAL"
    assert refund.result.payload["escalation"]["queue"] == "billing-tier2"
    assert [t["ticket_id"] for t in result.escalation_tickets] == ["ESC-5001"]

    sent_to_claude = json.loads(tool_results_in(claude.requests[2]["messages"][-1])[0]["content"])
    assert sent_to_claude["escalation"]["ticket_id"] == "ESC-5001"
    assert ord_1003_after.payload["amount_refunded"] == 0, "the refund must never reach the backend"


def test_hook_blocks_refund_before_identity_is_verified():
    claude = ScriptedClaude(
        reply("tool_use", tool_use("process_refund", customer_id="CUST-1001", order_id="ORD-1001",
                                   amount=129.99, reason="duplicate_charge")),
        reply("end_turn", text("Could you confirm your email?")),
    )
    _, result, _ = converse(claude, "Refund ORD-1001")

    refund = result.tool_calls[0]
    assert refund.blocked_by_hook
    assert refund.result.payload["errorCode"] == "IDENTITY_NOT_VERIFIED"
    assert result.escalation_tickets == []


def test_agent_cannot_refund_an_account_it_found_by_correcting_the_customers_typo():
    """Replays a live-run failure: Claude "fixed" alice@exmaple.com and refunded Alice's order."""
    claude = ScriptedClaude(
        reply("tool_use", tool_use("get_customer", email="alice@exmaple.com"),
              tool_use("lookup_order", order_id="ORD-1002")),
        reply("tool_use", tool_use("get_customer", email="alice@example.com")),
        reply("tool_use", tool_use("process_refund", customer_id="CUST-1001", order_id="ORD-1002",
                                   amount=89.00, reason="damaged_item")),
        reply("end_turn", text("Could you confirm the email on your account?")),
    )
    _, result, _ = converse(
        claude, "Hi, my email is alice@exmaple.com. My coffee maker order ORD-1002 arrived broken."
    )

    typo, _, corrected, refund = result.tool_calls
    assert typo.result.error_code == "CUSTOMER_NOT_FOUND"
    assert corrected.blocked_by_hook
    assert corrected.result.error_code == "IDENTIFIER_NOT_FROM_CUSTOMER"
    assert refund.blocked_by_hook
    assert refund.result.error_code == "IDENTITY_NOT_VERIFIED"


def test_multi_concern_turn_resolves_two_and_escalates_one():
    lookups = [tool_use("lookup_order", order_id=f"ORD-100{n}") for n in (1, 2, 3)]
    refunds = [
        tool_use("process_refund", customer_id="CUST-1001", order_id="ORD-1001", amount=129.99,
                 reason="duplicate_charge"),
        tool_use("process_refund", customer_id="CUST-1001", order_id="ORD-1002", amount=89.00,
                 reason="damaged_item"),
        tool_use("process_refund", customer_id="CUST-1001", order_id="ORD-1003", amount=1499.00,
                 reason="changed_mind"),
    ]
    claude = ScriptedClaude(
        reply("tool_use", tool_use("get_customer", email="alice@example.com")),
        reply("tool_use", *lookups),
        reply("tool_use", *refunds),
        reply("end_turn", text("1) Refunded. 2) Refunded. 3) Escalated as ESC-5001.")),
    )
    _, result, _ = converse(claude, "alice@example.com: three problems...")

    outcomes = {c.input["order_id"]: c for c in result.calls_to("process_refund")}
    assert not outcomes["ORD-1001"].result.is_error
    assert not outcomes["ORD-1002"].result.is_error
    assert outcomes["ORD-1003"].blocked_by_hook
    assert len(result.escalation_tickets) == 1
    assert len(tool_results_in(claude.requests[3]["messages"][-1])) == 3
    assert result.iterations == 4


def test_max_tokens_never_executes_a_truncated_tool_call():
    truncated = tool_use("process_refund", customer_id="CUST-1001")
    claude = ScriptedClaude(reply("max_tokens", text("Processing your refund"), truncated))
    agent, result, _ = converse(claude, "Refund please")

    assert result.stop_reason == "max_tokens"
    assert result.tool_calls == []
    (closing,) = tool_results_in(agent.messages[-1])
    assert closing["tool_use_id"] == truncated.id
    assert closing["is_error"] is True


def test_refusal_returns_a_safe_reply():
    claude = ScriptedClaude(reply("refusal"))
    agent, result, _ = converse(claude, "...")

    assert result.stop_reason == "refusal"
    assert result.text == REFUSAL_MESSAGE
    assert agent.messages[-1]["role"] == "user", "an empty assistant turn must not enter history"


def test_iteration_cap_stops_a_runaway_loop():
    claude = ScriptedClaude(
        *(reply("tool_use", tool_use("lookup_order", order_id="ORD-1001")) for _ in range(3))
    )
    _, result, _ = converse(claude, "Loop forever", AgentConfig(max_iterations=3))

    assert result.stop_reason == "max_iterations"
    assert result.text == ITERATION_CAP_MESSAGE
    assert len(claude.requests) == 3
