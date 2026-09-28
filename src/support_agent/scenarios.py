"""Live scenarios: real conversations with Claude, scored from the agent's tool trace.

Checks look at what the agent *did* (tool calls, errors, hook blocks, escalations), not only at
what it said, so a scenario passes only if the behaviour is right.
"""

from collections.abc import Callable
from dataclasses import dataclass

from support_agent.agent import AgentConfig, EventCallback, TurnResult, open_support_agent


@dataclass(frozen=True)
class Check:
    description: str
    passed: Callable[[TurnResult], bool]


@dataclass(frozen=True)
class Scenario:
    name: str
    purpose: str
    message: str
    should_escalate: bool
    checks: tuple[Check, ...]


@dataclass
class ScenarioReport:
    scenario: Scenario
    result: TurnResult
    outcomes: list[tuple[Check, bool]]

    @property
    def escalated(self) -> bool:
        return bool(self.result.escalation_tickets)

    @property
    def passed(self) -> bool:
        return all(ok for _, ok in self.outcomes)

    @property
    def escalation_correct(self) -> bool:
        return self.escalated == self.scenario.should_escalate


def refund_succeeded(order_id: str) -> Check:
    return Check(
        f"refund processed for {order_id}",
        lambda r: any(
            not c.result.is_error and c.input.get("order_id") == order_id
            for c in r.calls_to("process_refund")
        ),
    )


def no_refund_succeeded(order_id: str) -> Check:
    return Check(
        f"no refund processed for {order_id}",
        lambda r: not refund_succeeded(order_id).passed(r),
    )


def tool_error(tool_name: str, error_code: str) -> Check:
    return Check(
        f"{tool_name} returned {error_code}",
        lambda r: any(c.result.error_code == error_code for c in r.calls_to(tool_name)),
    )


def not_retried_after(tool_name: str, error_code: str) -> Check:
    def passed(r: TurnResult) -> bool:
        codes = [c.result.error_code for c in r.calls_to(tool_name)]
        return error_code in codes and codes.index(error_code) == len(codes) - 1

    return Check(f"{tool_name} hit {error_code} and was not retried", passed)


def retried_until_success(tool_name: str, order_id: str) -> Check:
    def passed(r: TurnResult) -> bool:
        attempts = [c for c in r.calls_to(tool_name) if c.input.get("order_id") == order_id]
        return len(attempts) >= 2 and not attempts[-1].result.is_error

    return Check(f"{tool_name} retried for {order_id} until it succeeded", passed)


def order_escalated(order_id: str) -> Check:
    def passed(r: TurnResult) -> bool:
        tickets = {t["ticket_id"] for t in r.escalation_tickets}
        return any(
            c.input.get("order_id") == order_id
            and (c.result.payload.get("escalation") or c.result.payload).get("ticket_id") in tickets
            for c in r.tool_calls
        )

    return Check(f"{order_id} escalated to a human", passed)


def escalated_with(reason_category: str) -> Check:
    return Check(
        f"escalated with reason {reason_category}",
        lambda r: any(
            not c.result.is_error and c.input.get("reason_category") == reason_category
            for c in r.calls_to("escalate_to_human")
        ),
    )


def escalated() -> Check:
    return Check("escalated to a human", lambda r: bool(r.escalation_tickets))


def not_escalated() -> Check:
    return Check("resolved without escalation", lambda r: not r.escalation_tickets)


def reply_mentions(*alternatives: str) -> Check:
    return Check(
        f"reply mentions {' or '.join(alternatives)}",
        lambda r: any(term.lower() in r.text.lower() for term in alternatives),
    )


def reply_contains_ticket() -> Check:
    return Check(
        "reply gives the customer the ticket ID",
        lambda r: any(t["ticket_id"] in r.text for t in r.escalation_tickets),
    )


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="multi_concern_refunds",
        purpose="Multi-concern decomposition, parallel lookups, policy hook redirect",
        message=(
            "Hi, I'm Alice (alice@example.com). Three things: I was charged twice for my "
            "headphones (order ORD-1001), the coffee maker from ORD-1002 arrived with a cracked "
            "carafe so I'd like a refund, and I've changed my mind about the laptop in ORD-1003 "
            "and want to return it for a full refund."
        ),
        should_escalate=True,
        checks=(
            refund_succeeded("ORD-1001"),
            refund_succeeded("ORD-1002"),
            no_refund_succeeded("ORD-1003"),
            order_escalated("ORD-1003"),
            reply_mentions("ORD-1001", "headphone"),
            reply_mentions("ORD-1002", "coffee"),
            reply_mentions("ORD-1003", "laptop"),
            reply_contains_ticket(),
        ),
    ),
    Scenario(
        name="duplicate_charge",
        purpose="Straightforward first-contact resolution",
        message=(
            "Hi, it's alice@example.com. I see two charges of $129.99 for my headphones order "
            "ORD-1001. Can you fix that?"
        ),
        should_escalate=False,
        checks=(refund_succeeded("ORD-1001"), not_escalated(), reply_mentions("RF-")),
    ),
    Scenario(
        name="transient_then_business_error",
        purpose="Retry a transient error, then explain a business error",
        message=(
            "Hello, this is bob@example.com. The running shoes from order ORD-1004 don't fit. "
            "Can I get a refund?"
        ),
        should_escalate=False,
        checks=(
            tool_error("lookup_order", "ORDER_SERVICE_TIMEOUT"),
            retried_until_success("lookup_order", "ORD-1004"),
            no_refund_succeeded("ORD-1004"),
            not_escalated(),
            reply_mentions("30"),
        ),
    ),
    Scenario(
        name="validation_error",
        purpose="Validation error: ask the customer to clarify, don't guess",
        message=(
            "Hi, my email is alice@exmaple.com. My coffee maker order ORD-1002 arrived broken, "
            "please refund it."
        ),
        should_escalate=False,
        checks=(
            tool_error("get_customer", "CUSTOMER_NOT_FOUND"),
            no_refund_succeeded("ORD-1002"),
            not_escalated(),
            reply_mentions("email"),
        ),
    ),
    Scenario(
        name="permission_error",
        purpose="Permission error: don't retry, escalate",
        message=(
            "Hi, I'm Carol, carol@example.com. The desk lamp from order ORD-1006 arrived broken. "
            "Please refund me."
        ),
        should_escalate=True,
        checks=(
            not_retried_after("process_refund", "ACCOUNT_REFUNDS_RESTRICTED"),
            no_refund_succeeded("ORD-1006"),
            escalated(),
            reply_contains_ticket(),
        ),
    ),
    Scenario(
        name="explicit_human_request",
        purpose="Escalate immediately when the customer asks for a human",
        message=(
            "I've contacted you three times about my orders and I'm done with bots. Get me a "
            "human manager now. My email is alice@example.com."
        ),
        should_escalate=True,
        checks=(escalated_with("customer_requested_human"), reply_contains_ticket()),
    ),
    Scenario(
        name="multi_concern_out_of_scope",
        purpose="Multi-concern: resolve one concern, escalate the one no tool can handle",
        message=(
            "bob@example.com here. Where is my rain jacket, order ORD-1005? Also, please change "
            "the shipping address on my account to 12 Oak Street, Springfield."
        ),
        should_escalate=True,
        checks=(
            no_refund_succeeded("ORD-1005"),
            escalated_with("out_of_scope_request"),
            reply_mentions("ORD-1005", "jacket"),
            reply_contains_ticket(),
        ),
    ),
)

SCENARIOS_BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}


async def run_scenario(
    scenario: Scenario,
    config: AgentConfig | None = None,
    on_event: EventCallback | None = None,
) -> ScenarioReport:
    async with open_support_agent(config=config, on_event=on_event) as agent:
        result = await agent.send(scenario.message)
    return ScenarioReport(
        scenario, result, [(check, check.passed(result)) for check in scenario.checks]
    )
