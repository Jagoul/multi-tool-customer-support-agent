"""The live-scenario checks must pass correct behaviour and catch incorrect behaviour.

Scripted turns stand in for the model so the grading logic itself is verified offline.
"""

from conftest import ScriptedClaude, reply, run_turn, text, tool_use

from support_agent.scenarios import SCENARIOS_BY_NAME, ScenarioReport


def grade(scenario_name: str, result) -> ScenarioReport:
    scenario = SCENARIOS_BY_NAME[scenario_name]
    return ScenarioReport(scenario, result, [(c, c.passed(result)) for c in scenario.checks])


def failed_checks(report: ScenarioReport) -> list[str]:
    return [check.description for check, ok in report.outcomes if not ok]


def multi_concern_turn(final_reply: str):
    return run_turn(
        ScriptedClaude(
            reply("tool_use", tool_use("get_customer", email="alice@example.com")),
            reply("tool_use", *(tool_use("lookup_order", order_id=f"ORD-100{n}") for n in (1, 2, 3))),
            reply(
                "tool_use",
                tool_use("process_refund", customer_id="CUST-1001", order_id="ORD-1001",
                         amount=129.99, reason="duplicate_charge"),
                tool_use("process_refund", customer_id="CUST-1001", order_id="ORD-1002",
                         amount=89.00, reason="damaged_item"),
                tool_use("process_refund", customer_id="CUST-1001", order_id="ORD-1003",
                         amount=1499.00, reason="changed_mind"),
            ),
            reply("end_turn", text(final_reply)),
        ),
        "alice@example.com: three problems",
    )


def test_multi_concern_checks_pass_a_correct_turn():
    report = grade(
        "multi_concern_refunds",
        multi_concern_turn(
            "Headphones (ORD-1001): duplicate refunded. Coffee maker (ORD-1002): refunded. "
            "Laptop (ORD-1003): a specialist will review it, ticket ESC-5001."
        ),
    )
    assert failed_checks(report) == []
    assert report.escalation_correct


def test_multi_concern_checks_catch_a_reply_that_drops_a_concern_and_the_ticket():
    report = grade("multi_concern_refunds", multi_concern_turn("Your headphones were refunded."))
    assert set(failed_checks(report)) == {
        "reply mentions ORD-1002 or coffee",
        "reply mentions ORD-1003 or laptop",
        "reply gives the customer the ticket ID",
    }


def test_transient_checks_require_a_retry_that_succeeds():
    retried = run_turn(
        ScriptedClaude(
            reply("tool_use", tool_use("lookup_order", order_id="ORD-1004")),
            reply("tool_use", tool_use("lookup_order", order_id="ORD-1004")),
            reply("end_turn", text("The 30-day return window has closed.")),
        ),
        "refund ORD-1004",
    )
    gave_up = run_turn(
        ScriptedClaude(
            reply("tool_use", tool_use("lookup_order", order_id="ORD-1004")),
            reply("end_turn", text("Our 30-day policy...")),
        ),
        "refund ORD-1004",
    )
    assert failed_checks(grade("transient_then_business_error", retried)) == []
    assert failed_checks(grade("transient_then_business_error", gave_up)) == [
        "lookup_order retried for ORD-1004 until it succeeded"
    ]


def test_permission_checks_catch_a_retry_of_a_non_retryable_error():
    refund = {"customer_id": "CUST-1003", "order_id": "ORD-1006", "amount": 60.0, "reason": "damaged_item"}
    retried = run_turn(
        ScriptedClaude(
            reply("tool_use", tool_use("get_customer", email="carol@example.com")),
            reply("tool_use", tool_use("process_refund", **refund)),
            reply("tool_use", tool_use("process_refund", **refund)),
            reply("tool_use", tool_use("escalate_to_human", reason_category="account_restricted",
                                       summary="Carol (CUST-1003) needs a refund for ORD-1006.")),
            reply("end_turn", text("A specialist will help: ticket ESC-5001.")),
        ),
        "carol@example.com: refund my lamp",
    )
    assert failed_checks(grade("permission_error", retried)) == [
        "process_refund hit ACCOUNT_REFUNDS_RESTRICTED and was not retried"
    ]


def test_escalation_decision_is_scored_against_expectation():
    needless = run_turn(
        ScriptedClaude(
            reply("tool_use", tool_use("escalate_to_human", reason_category="policy_exception",
                                       summary="Alice wants the duplicate charge on ORD-1001 fixed.")),
            reply("end_turn", text("Escalated as ESC-5001.")),
        ),
        "double charge",
    )
    report = grade("duplicate_charge", needless)
    assert not report.escalation_correct
    assert "resolved without escalation" in failed_checks(report)
