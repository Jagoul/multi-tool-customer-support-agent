"""Tool definitions (single source of truth) and dispatch to the backend.

Both runtimes consume these definitions: the stdio MCP server (`mcp_server.py`) and the
in-process Claude Agent SDK server (`sdk_agent.py`).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from support_agent.backend import ESCALATION_QUEUES, FIRST_RESPONSE_SLA, REFUND_REASONS, SupportBackend
from support_agent.errors import ErrorCategory, ToolError

GET_CUSTOMER_DESCRIPTION = """\
Look up and verify a customer account by email address or customer ID. Returns the customer's \
identity, account status, loyalty tier, and a SUMMARY list of their orders (order_id, placed_on, \
status, order_total). It does not return items, charges, or refund eligibility.

Use this tool when:
- You need to identify or verify who the customer is. Call it before any account-specific action: \
process_refund is rejected for a customer this tool has not returned in the current conversation.
- The customer gives an email address or customer ID but no order ID, or says "my order" without \
a number. This lists their orders so you can find the right one.
- You need the account status (for example "active" or "restricted").

Do NOT use this tool when:
- You need line items, charges, delivery dates, or refund eligibility for an order. Use \
lookup_order with the order ID instead.
- You only have the customer's name. Names are not searchable, so ask for the email on the account.

Inputs: exactly one of `email` (for example alice@example.com) or `customer_id` (format \
CUST-1234), exactly as the customer wrote it. Never pass both. Never correct a typo or reuse a \
customer_id read from an order: lookups with an identifier the customer did not write are blocked \
(IDENTIFIER_NOT_FROM_CUSTOMER).

Errors (all validation, isRetryable false): INVALID_ARGUMENTS, INVALID_EMAIL, INVALID_CUSTOMER_ID, \
CUSTOMER_NOT_FOUND. Ask the customer to confirm their details instead of guessing."""

LOOKUP_ORDER_DESCRIPTION = """\
Retrieve the full details of ONE specific order by its order ID: line items, every charge on the \
order (suspected duplicate charges are flagged with possible_duplicate), delivery status and dates, \
amount already refunded, refundable_amount, and whether the 30-day return window is still open.

Use this tool when:
- You have an order ID (format ORD-1234) and need facts about that order, either to answer a \
question or to decide whether and how much to refund.
- You are investigating a billing dispute (for example a double charge), a damaged or wrong item, \
a return request, or a "where is my order" question.
- Several orders are involved. Call it once per order, in parallel.

Do NOT use this tool when:
- You only have the customer's email, name, or customer ID. This tool cannot search by customer. \
Call get_customer first to list their order IDs.
- You need to verify the customer's identity. Order lookups do not verify identity.

Input: `order_id` in the format ORD-1234.

Errors: validation INVALID_ORDER_ID or ORDER_NOT_FOUND (isRetryable false: fix the ID or ask the \
customer). Transient ORDER_SERVICE_TIMEOUT (isRetryable true: retry the same call)."""

PROCESS_REFUND_DESCRIPTION = """\
Issue a refund to the customer's original payment method for one order. This moves money and is \
NOT idempotent: calling it twice refunds twice.

Use this tool only when ALL of these are true:
- The customer was verified with get_customer in this conversation and the order belongs to them.
- You called lookup_order for this order and its facts support the refund: a charge flagged \
possible_duplicate, a damaged or wrong item within the return window, or a change-of-mind return \
within the return window.
- The amount is no more than the order's refundable_amount.

Do NOT use this tool when:
- The request is outside policy (return window closed, order not delivered yet, account \
restricted). Explain the policy to the customer, or use escalate_to_human for an exception.
- The customer wants store credit, an exchange, or anything other than money back.

Refunds above the agent approval limit are blocked automatically and routed to a human. If a \
result says an escalation ticket was already created, do not call escalate_to_human again for \
that refund: give the customer the ticket ID.

Inputs: customer_id (CUST-1234, taken from get_customer), order_id (ORD-1234), amount (US dollars, \
positive, at most refundable_amount), reason (use duplicate_charge only for a charge flagged \
possible_duplicate).

Errors:
- validation (bad input, amount above refundable_amount, identity not verified): fix the input and \
try again.
- business (RETURN_WINDOW_EXPIRED, ALREADY_REFUNDED, ORDER_STILL_IN_TRANSIT, \
DELIVERY_CONFIRMED_BY_CARRIER, NO_DUPLICATE_CHARGE_FOUND): do not retry. Explain the policy.
- permission (ACCOUNT_REFUNDS_RESTRICTED, ORDER_NOT_OWNED_BY_CUSTOMER, \
REFUND_REQUIRES_HUMAN_APPROVAL): do not retry. Escalate, or relay the ticket if one was created."""

ESCALATE_TO_HUMAN_DESCRIPTION = """\
Hand the case to a human support specialist by opening a ticket in the right queue. The human does \
NOT see this conversation, so the summary must stand on its own.

Use this tool when:
- The customer explicitly asks for a human, manager, or supervisor. Escalate right away.
- A tool returns a permission error (for example ACCOUNT_REFUNDS_RESTRICTED) and no ticket was \
created automatically.
- The customer needs a policy exception they insist on, or a claim that needs investigation (for \
example a package marked delivered that never arrived).
- A transient error persists after 2 retries.
- The request needs an action none of your tools can perform (for example changing an address).

Do NOT use this tool when:
- You can resolve the concern yourself, within policy, with the other tools. First-contact \
resolution is the goal.
- A tool result already contains an escalation ticket for this issue. Relay that ticket instead of \
creating a duplicate.
- A business error only needs explaining (for example an expired return window) and the customer \
has not asked for an exception.

Inputs: reason_category and summary are required. The summary covers who the customer is, what they \
want, the order IDs and amounts involved, and what was tried with what outcome. Also pass \
customer_id, order_id, actions_taken, recommended_action, and priority when known.

Returns: ticket_id, queue, priority, expected_first_response. Give the customer the ticket_id and \
expected response time."""

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "get_customer",
        "title": "Get customer account",
        "description": GET_CUSTOMER_DESCRIPTION,
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "Email address on the account, e.g. alice@example.com. "
                    "Omit when passing customer_id.",
                },
                "customer_id": {
                    "type": "string",
                    "description": "Customer ID in the format CUST-1234. Omit when passing email.",
                },
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "lookup_order",
        "title": "Look up order details",
        "description": LOOKUP_ORDER_DESCRIPTION,
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "Order ID in the format ORD-1234, e.g. ORD-1001.",
                },
            },
            "required": ["order_id"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "process_refund",
        "title": "Process refund",
        "description": PROCESS_REFUND_DESCRIPTION,
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "description": "Verified customer ID (CUST-1234) returned by get_customer.",
                },
                "order_id": {
                    "type": "string",
                    "description": "Order ID (ORD-1234) that the refund applies to.",
                },
                "amount": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "Refund amount in US dollars, e.g. 129.99. Must not exceed "
                    "refundable_amount from lookup_order.",
                },
                "reason": {
                    "type": "string",
                    "enum": list(REFUND_REASONS),
                    "description": "Why the refund is issued.",
                },
            },
            "required": ["customer_id", "order_id", "amount", "reason"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "escalate_to_human",
        "title": "Escalate to a human",
        "description": ESCALATE_TO_HUMAN_DESCRIPTION,
        "input_schema": {
            "type": "object",
            "properties": {
                "reason_category": {
                    "type": "string",
                    "enum": list(ESCALATION_QUEUES),
                    "description": "Why the case needs a human. Determines the queue.",
                },
                "summary": {
                    "type": "string",
                    "description": "Self-contained case summary for a human who has not seen "
                    "the conversation.",
                },
                "customer_id": {"type": "string", "description": "Customer ID, if known."},
                "order_id": {"type": "string", "description": "Primary order ID, if any."},
                "actions_taken": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "What you already did, with outcomes.",
                },
                "recommended_action": {
                    "type": "string",
                    "description": "What you recommend the human do next.",
                },
                "priority": {
                    "type": "string",
                    "enum": list(FIRST_RESPONSE_SLA),
                    "description": "Defaults to normal. Use high for money at stake or an "
                    "upset customer, urgent only for account security.",
                },
            },
            "required": ["reason_category", "summary"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
]

TOOLS_BY_NAME = {definition["name"]: definition for definition in TOOL_DEFINITIONS}
TOOL_NAMES = tuple(TOOLS_BY_NAME)

_VALIDATORS = {
    definition["name"]: Draft202012Validator(definition["input_schema"])
    for definition in TOOL_DEFINITIONS
}

_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_customer": SupportBackend.get_customer,
    "lookup_order": SupportBackend.lookup_order,
    "process_refund": SupportBackend.process_refund,
    "escalate_to_human": SupportBackend.escalate_to_human,
}


@dataclass(frozen=True)
class ToolResult:
    payload: dict[str, Any]
    is_error: bool

    @classmethod
    def from_error(cls, error: ToolError) -> "ToolResult":
        return cls(error.to_payload(), True)

    def to_text(self) -> str:
        return json.dumps(self.payload)

    @property
    def error_category(self) -> str | None:
        return self.payload.get("errorCategory") if self.is_error else None

    @property
    def error_code(self) -> str | None:
        return self.payload.get("errorCode") if self.is_error else None


def execute_tool(backend: SupportBackend, name: str, arguments: dict[str, Any]) -> ToolResult:
    if name not in TOOLS_BY_NAME:
        return ToolResult.from_error(
            ToolError(
                ErrorCategory.VALIDATION,
                "UNKNOWN_TOOL",
                f"No tool named '{name}'. Available tools: {', '.join(TOOL_NAMES)}.",
            )
        )
    try:
        _validate_arguments(name, arguments)
        return ToolResult(_HANDLERS[name](backend, **arguments), False)
    except ToolError as error:
        return ToolResult.from_error(error)


def _validate_arguments(name: str, arguments: dict[str, Any]) -> None:
    problems = [
        f"{'.'.join(map(str, error.path)) or 'input'}: {error.message}"
        for error in _VALIDATORS[name].iter_errors(arguments)
    ]
    if problems:
        raise ToolError(
            ErrorCategory.VALIDATION,
            "INVALID_ARGUMENTS",
            f"Arguments for {name} do not match its schema: {'; '.join(problems)}.",
        )
