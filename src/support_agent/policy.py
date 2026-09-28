"""Business rules enforced in code, before a tool runs.

The model is never told the refund limit. The rule lives here so it holds no matter what the
model decides, and both runtimes (manual loop and Claude Agent SDK) call the same policy.
"""

import os
from dataclasses import dataclass, field
from typing import Any

from support_agent.errors import ErrorCategory, ToolError
from support_agent.tools import ToolResult

DEFAULT_REFUND_LIMIT = 500.00


@dataclass(frozen=True)
class HookDecision:
    allowed: bool
    error: ToolError | None = None
    escalation_request: dict[str, Any] | None = None

    @classmethod
    def allow(cls) -> "HookDecision":
        return cls(allowed=True)


@dataclass
class SupportPolicy:
    refund_limit: float = field(
        default_factory=lambda: float(
            os.environ.get("SUPPORT_AGENT_REFUND_LIMIT", DEFAULT_REFUND_LIMIT)
        )
    )
    verified_customers: set[str] = field(default_factory=set)
    customer_text: str = ""

    def observe_customer_message(self, text: str) -> None:
        """Record what the customer wrote; account lookups must use identifiers from it."""
        self.customer_text += "\n" + text.lower()

    def pre_tool_use(self, tool_name: str, tool_input: dict[str, Any]) -> HookDecision:
        if tool_name == "get_customer":
            return self._check_identifiers_came_from_customer(tool_input)
        if tool_name != "process_refund":
            return HookDecision.allow()

        customer_id = str(tool_input.get("customer_id", "")).strip().upper()
        if customer_id not in self.verified_customers:
            return HookDecision(
                allowed=False,
                error=ToolError(
                    ErrorCategory.VALIDATION,
                    "IDENTITY_NOT_VERIFIED",
                    f"Refund blocked: customer '{customer_id}' has not been verified in this "
                    "conversation. Call get_customer first, then retry the refund with the "
                    "customer_id it returns.",
                ),
            )

        amount = float(tool_input.get("amount", 0))
        if amount > self.refund_limit:
            order_id = tool_input.get("order_id")
            reason = tool_input.get("reason", "unspecified")
            return HookDecision(
                allowed=False,
                error=ToolError(
                    ErrorCategory.PERMISSION,
                    "REFUND_REQUIRES_HUMAN_APPROVAL",
                    f"Refund of ${amount:,.2f} for {order_id} was NOT processed: it exceeds the "
                    f"${self.refund_limit:,.2f} limit agents may approve.",
                    {"requested_amount": amount, "approval_limit": self.refund_limit},
                ),
                escalation_request={
                    "reason_category": "refund_above_threshold",
                    "customer_id": customer_id,
                    "order_id": order_id,
                    "priority": "high",
                    "summary": (
                        f"Customer {customer_id} requested a ${amount:,.2f} refund for "
                        f"{order_id} (reason: {reason}). The amount exceeds the "
                        f"${self.refund_limit:,.2f} agent approval limit, so the refund was "
                        "blocked before execution and needs human approval."
                    ),
                    "actions_taken": [
                        "Verified the customer with get_customer",
                        f"Attempted process_refund for ${amount:,.2f}, blocked by policy hook",
                    ],
                    "recommended_action": (
                        f"Review the order and approve or deny a ${amount:,.2f} refund "
                        f"for {order_id}."
                    ),
                },
            )

        return HookDecision.allow()

    def _check_identifiers_came_from_customer(self, tool_input: dict[str, Any]) -> HookDecision:
        """Block account lookups with identifiers the customer never gave.

        Stops the agent from "fixing" a mistyped email, or reusing a customer_id read from an
        order, and then acting on an account the person hasn't shown they own.
        """
        for field_name in ("email", "customer_id"):
            identifier = str(tool_input.get(field_name) or "").strip()
            if identifier and identifier.lower() not in self.customer_text:
                return HookDecision(
                    allowed=False,
                    error=ToolError(
                        ErrorCategory.VALIDATION,
                        "IDENTIFIER_NOT_FROM_CUSTOMER",
                        f"Lookup blocked: the {field_name} '{identifier}' does not appear in "
                        "anything the customer wrote. Look up accounts only with an email or "
                        "customer ID the customer gave you, exactly as given. If it was not "
                        "found, ask the customer to confirm it. Never correct or guess it.",
                    ),
                )
        return HookDecision.allow()

    def post_tool_use(self, tool_name: str, result: ToolResult) -> None:
        if tool_name == "get_customer" and not result.is_error:
            self.verified_customers.add(result.payload["customer_id"])


def blocked_result(decision: HookDecision, escalation: ToolResult | None = None) -> ToolResult:
    """The tool_result Claude receives in place of a blocked call."""
    payload = decision.error.to_payload()
    if escalation is not None:
        payload["escalation"] = escalation.payload
        if escalation.is_error:
            payload["description"] += (
                " Automatic escalation FAILED (see `escalation`). Call escalate_to_human yourself."
            )
        else:
            payload["description"] += (
                f" The case was escalated to a human automatically: ticket "
                f"{escalation.payload['ticket_id']}, first response "
                f"{escalation.payload['expected_first_response']}. Do not call escalate_to_human "
                "again for this refund. Give the customer the ticket ID and expected response time."
            )
    return ToolResult(payload, True)
