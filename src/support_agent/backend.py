"""In-memory stand-in for the order, billing, and ticketing systems.

Money is stored in integer cents to avoid float rounding; the API surface speaks dollars.
Dates are seeded relative to `today` so return-window rules behave the same on any run date.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from support_agent.errors import ErrorCategory, ToolError

RETURN_WINDOW_DAYS = 30

CUSTOMER_ID_RE = re.compile(r"^CUST-\d{4}$")
ORDER_ID_RE = re.compile(r"^ORD-\d{4}$")

REFUND_REASONS = (
    "duplicate_charge",
    "damaged_item",
    "wrong_item",
    "not_received",
    "changed_mind",
    "other",
)
WINDOW_BOUND_REASONS = {"damaged_item", "wrong_item", "changed_mind", "other"}

ESCALATION_QUEUES = {
    "refund_above_threshold": "billing-tier2",
    "account_restricted": "trust-and-safety",
    "policy_exception": "support-tier2",
    "customer_requested_human": "support-tier2",
    "unresolved_technical_error": "support-tier2",
    "out_of_scope_request": "support-tier2",
}
FIRST_RESPONSE_SLA = {
    "urgent": "within 1 hour",
    "high": "within 4 hours",
    "normal": "within 24 hours",
    "low": "within 48 hours",
}

DEFAULT_TRANSIENT_FAILURES = {"ORD-1004": 1}


def _dollars(cents: int) -> float:
    return round(cents / 100, 2)


@dataclass
class Customer:
    customer_id: str
    name: str
    email: str
    tier: str
    account_status: str
    order_ids: list[str]
    refund_hold: bool = False


@dataclass
class Charge:
    charge_id: str
    amount_cents: int
    charged_on: date
    possible_duplicate: bool = False


@dataclass
class OrderItem:
    name: str
    quantity: int
    unit_price_cents: int


@dataclass
class Order:
    order_id: str
    customer_id: str
    status: str
    placed_on: date
    items: list[OrderItem]
    charges: list[Charge]
    delivered_on: date | None = None
    estimated_delivery: date | None = None
    refunded_cents: int = 0

    @property
    def total_cents(self) -> int:
        return sum(item.quantity * item.unit_price_cents for item in self.items)

    @property
    def charged_cents(self) -> int:
        return sum(charge.amount_cents for charge in self.charges)

    @property
    def refundable_cents(self) -> int:
        return self.charged_cents - self.refunded_cents


@dataclass
class SupportBackend:
    today: date = field(default_factory=date.today)
    transient_failures: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_TRANSIENT_FAILURES)
    )
    refunds: list[dict[str, Any]] = field(default_factory=list)
    tickets: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.customers, self.orders = _seed(self.today)

    def get_customer(
        self, customer_id: str | None = None, email: str | None = None
    ) -> dict[str, Any]:
        if (customer_id is None) == (email is None):
            raise ToolError(
                ErrorCategory.VALIDATION,
                "INVALID_ARGUMENTS",
                "Provide exactly one of `email` or `customer_id`, not both and not neither.",
            )
        if customer_id is not None:
            customer_id = customer_id.strip().upper()
            if not CUSTOMER_ID_RE.match(customer_id):
                raise ToolError(
                    ErrorCategory.VALIDATION,
                    "INVALID_CUSTOMER_ID",
                    f"'{customer_id}' is not a valid customer ID. Customer IDs look like CUST-1234.",
                )
            customer = self.customers.get(customer_id)
        else:
            email = email.strip().lower()
            if "@" not in email:
                raise ToolError(
                    ErrorCategory.VALIDATION,
                    "INVALID_EMAIL",
                    f"'{email}' is not a valid email address.",
                )
            customer = next((c for c in self.customers.values() if c.email == email), None)

        if customer is None:
            raise ToolError(
                ErrorCategory.VALIDATION,
                "CUSTOMER_NOT_FOUND",
                "No customer matches that identifier. Ask the customer to confirm the email "
                "address or customer ID on their account.",
            )

        return {
            "customer_id": customer.customer_id,
            "name": customer.name,
            "email": customer.email,
            "tier": customer.tier,
            "account_status": customer.account_status,
            "orders": [
                {
                    "order_id": order.order_id,
                    "placed_on": order.placed_on.isoformat(),
                    "status": order.status,
                    "order_total": _dollars(order.total_cents),
                }
                for order in (self.orders[oid] for oid in customer.order_ids)
            ],
        }

    def lookup_order(self, order_id: str) -> dict[str, Any]:
        order = self._require_order(order_id)

        if self.transient_failures.get(order.order_id, 0) > 0:
            self.transient_failures[order.order_id] -= 1
            raise ToolError(
                ErrorCategory.TRANSIENT,
                "ORDER_SERVICE_TIMEOUT",
                f"The order service timed out while fetching {order.order_id}. This is "
                "temporary. Retry the same call.",
                {"retry_after_seconds": 1},
            )

        days_since_delivery = (
            (self.today - order.delivered_on).days if order.delivered_on else None
        )
        window_closes = (
            order.delivered_on + timedelta(days=RETURN_WINDOW_DAYS) if order.delivered_on else None
        )
        return {
            "order_id": order.order_id,
            "customer_id": order.customer_id,
            "status": order.status,
            "placed_on": order.placed_on.isoformat(),
            "delivered_on": order.delivered_on.isoformat() if order.delivered_on else None,
            "estimated_delivery": (
                order.estimated_delivery.isoformat() if order.estimated_delivery else None
            ),
            "items": [
                {
                    "name": item.name,
                    "quantity": item.quantity,
                    "unit_price": _dollars(item.unit_price_cents),
                }
                for item in order.items
            ],
            "order_total": _dollars(order.total_cents),
            "charges": [
                {
                    "charge_id": charge.charge_id,
                    "amount": _dollars(charge.amount_cents),
                    "charged_on": charge.charged_on.isoformat(),
                    "possible_duplicate": charge.possible_duplicate,
                }
                for charge in order.charges
            ],
            "amount_charged": _dollars(order.charged_cents),
            "amount_refunded": _dollars(order.refunded_cents),
            "refundable_amount": _dollars(order.refundable_cents),
            "days_since_delivery": days_since_delivery,
            "return_window": {
                "days": RETURN_WINDOW_DAYS,
                "closes_on": window_closes.isoformat() if window_closes else None,
                "open": window_closes is not None and self.today <= window_closes,
            },
        }

    def process_refund(
        self, customer_id: str, order_id: str, amount: float, reason: str
    ) -> dict[str, Any]:
        order = self._require_order(order_id)
        customer_id = customer_id.strip().upper()
        customer = self.customers.get(customer_id)
        if customer is None:
            raise ToolError(
                ErrorCategory.VALIDATION,
                "CUSTOMER_NOT_FOUND",
                f"No customer with ID '{customer_id}'. Use the customer_id returned by get_customer.",
            )
        if reason not in REFUND_REASONS:
            raise ToolError(
                ErrorCategory.VALIDATION,
                "INVALID_REASON",
                f"'{reason}' is not a valid refund reason. Use one of: {', '.join(REFUND_REASONS)}.",
            )
        amount_cents = round(float(amount) * 100)
        if amount_cents <= 0:
            raise ToolError(
                ErrorCategory.VALIDATION,
                "INVALID_AMOUNT",
                "Refund amount must be a positive number of US dollars.",
            )

        if order.customer_id != customer.customer_id:
            raise ToolError(
                ErrorCategory.PERMISSION,
                "ORDER_NOT_OWNED_BY_CUSTOMER",
                f"{order.order_id} does not belong to {customer.customer_id}. Agents may only act "
                "on orders owned by the verified customer.",
            )
        if customer.refund_hold:
            raise ToolError(
                ErrorCategory.PERMISSION,
                "ACCOUNT_REFUNDS_RESTRICTED",
                f"Refunds on {customer.customer_id} are on hold pending a trust-and-safety review. "
                "Agents are not permitted to issue refunds on this account. Escalate to a human.",
                {"hold": "trust_and_safety_review"},
            )
        if order.refundable_cents <= 0:
            raise ToolError(
                ErrorCategory.BUSINESS,
                "ALREADY_REFUNDED",
                f"{order.order_id} has already been fully refunded.",
            )
        if amount_cents > order.refundable_cents:
            raise ToolError(
                ErrorCategory.VALIDATION,
                "AMOUNT_EXCEEDS_REFUNDABLE",
                f"Requested ${_dollars(amount_cents):.2f} but only "
                f"${_dollars(order.refundable_cents):.2f} is refundable on {order.order_id}.",
                {"refundable_amount": _dollars(order.refundable_cents)},
            )

        if reason == "duplicate_charge" and not any(c.possible_duplicate for c in order.charges):
            raise ToolError(
                ErrorCategory.BUSINESS,
                "NO_DUPLICATE_CHARGE_FOUND",
                f"Billing shows no duplicate charge on {order.order_id}.",
            )
        if reason == "not_received":
            if order.status == "in_transit":
                raise ToolError(
                    ErrorCategory.BUSINESS,
                    "ORDER_STILL_IN_TRANSIT",
                    f"{order.order_id} is still in transit (estimated delivery "
                    f"{order.estimated_delivery}). It can't be refunded as not received yet.",
                )
            raise ToolError(
                ErrorCategory.BUSINESS,
                "DELIVERY_CONFIRMED_BY_CARRIER",
                f"The carrier confirmed delivery of {order.order_id} on {order.delivered_on}. A "
                "missing-package claim needs a human carrier investigation.",
            )
        if reason in WINDOW_BOUND_REASONS:
            if order.delivered_on is None:
                raise ToolError(
                    ErrorCategory.BUSINESS,
                    "ORDER_NOT_DELIVERED",
                    f"{order.order_id} has not been delivered yet, so it can't be returned.",
                )
            closes_on = order.delivered_on + timedelta(days=RETURN_WINDOW_DAYS)
            if self.today > closes_on:
                raise ToolError(
                    ErrorCategory.BUSINESS,
                    "RETURN_WINDOW_EXPIRED",
                    f"{order.order_id} was delivered on {order.delivered_on.isoformat()}. The "
                    f"{RETURN_WINDOW_DAYS}-day return window closed on {closes_on.isoformat()}.",
                    {"return_window_days": RETURN_WINDOW_DAYS, "closed_on": closes_on.isoformat()},
                )

        order.refunded_cents += amount_cents
        refund = {
            "refund_id": f"RF-{7001 + len(self.refunds)}",
            "order_id": order.order_id,
            "customer_id": customer.customer_id,
            "amount": _dollars(amount_cents),
            "reason": reason,
            "status": "processed",
            "settlement": "3-5 business days to the original payment method",
            "remaining_refundable": _dollars(order.refundable_cents),
        }
        self.refunds.append(refund)
        return refund

    def escalate_to_human(
        self,
        reason_category: str,
        summary: str,
        customer_id: str | None = None,
        order_id: str | None = None,
        actions_taken: list[str] | None = None,
        recommended_action: str | None = None,
        priority: str = "normal",
    ) -> dict[str, Any]:
        if reason_category not in ESCALATION_QUEUES:
            raise ToolError(
                ErrorCategory.VALIDATION,
                "INVALID_REASON_CATEGORY",
                f"'{reason_category}' is not a valid reason_category. Use one of: "
                f"{', '.join(ESCALATION_QUEUES)}.",
            )
        if priority not in FIRST_RESPONSE_SLA:
            raise ToolError(
                ErrorCategory.VALIDATION,
                "INVALID_PRIORITY",
                f"'{priority}' is not a valid priority. Use one of: {', '.join(FIRST_RESPONSE_SLA)}.",
            )
        if len(summary.strip()) < 20:
            raise ToolError(
                ErrorCategory.VALIDATION,
                "SUMMARY_TOO_SHORT",
                "The summary must let a human act without reading the transcript: include who "
                "the customer is, what they asked for, and what has been tried.",
            )

        ticket = {
            "ticket_id": f"ESC-{5001 + len(self.tickets)}",
            "status": "open",
            "queue": ESCALATION_QUEUES[reason_category],
            "priority": priority,
            "expected_first_response": FIRST_RESPONSE_SLA[priority],
            "reason_category": reason_category,
            "customer_id": customer_id,
            "order_id": order_id,
            "summary": summary.strip(),
            "actions_taken": actions_taken or [],
            "recommended_action": recommended_action,
        }
        self.tickets.append(ticket)
        return {
            key: ticket[key]
            for key in ("ticket_id", "status", "queue", "priority", "expected_first_response")
        }

    def _require_order(self, order_id: str) -> Order:
        normalized = order_id.strip().upper()
        if not ORDER_ID_RE.match(normalized):
            raise ToolError(
                ErrorCategory.VALIDATION,
                "INVALID_ORDER_ID",
                f"'{order_id}' is not a valid order ID. Order IDs look like ORD-1234. If you only "
                "have the customer's email or name, call get_customer to list their orders.",
            )
        order = self.orders.get(normalized)
        if order is None:
            raise ToolError(
                ErrorCategory.VALIDATION,
                "ORDER_NOT_FOUND",
                f"No order with ID {normalized}. Ask the customer to double-check the order "
                "number, or call get_customer to list the orders on their account.",
            )
        return order


def _seed(today: date) -> tuple[dict[str, Customer], dict[str, Order]]:
    def ago(days: int) -> date:
        return today - timedelta(days=days)

    customers = [
        Customer("CUST-1001", "Alice Nguyen", "alice@example.com", "gold", "active",
                 ["ORD-1001", "ORD-1002", "ORD-1003"]),
        Customer("CUST-1002", "Bob Martinez", "bob@example.com", "standard", "active",
                 ["ORD-1004", "ORD-1005"]),
        Customer("CUST-1003", "Carol Smith", "carol@example.com", "standard", "active",
                 ["ORD-1006"], refund_hold=True),
    ]
    orders = [
        Order("ORD-1001", "CUST-1001", "delivered", ago(9),
              [OrderItem("Wireless Headphones", 1, 12999)],
              [Charge("CH-9001", 12999, ago(9)), Charge("CH-9002", 12999, ago(9), True)],
              delivered_on=ago(5)),
        Order("ORD-1002", "CUST-1001", "delivered", ago(16),
              [OrderItem("Coffee Maker", 1, 8900)],
              [Charge("CH-9003", 8900, ago(16))],
              delivered_on=ago(12)),
        Order("ORD-1003", "CUST-1001", "delivered", ago(14),
              [OrderItem("14-inch Laptop", 1, 149900)],
              [Charge("CH-9004", 149900, ago(14))],
              delivered_on=ago(10)),
        Order("ORD-1004", "CUST-1002", "delivered", ago(50),
              [OrderItem("Running Shoes", 1, 12000)],
              [Charge("CH-9005", 12000, ago(50))],
              delivered_on=ago(45)),
        Order("ORD-1005", "CUST-1002", "in_transit", ago(3),
              [OrderItem("Rain Jacket", 1, 7500)],
              [Charge("CH-9006", 7500, ago(3))],
              estimated_delivery=today + timedelta(days=2)),
        Order("ORD-1006", "CUST-1003", "delivered", ago(7),
              [OrderItem("Desk Lamp", 1, 6000)],
              [Charge("CH-9007", 6000, ago(7))],
              delivered_on=ago(3)),
    ]
    return (
        {c.customer_id: c for c in customers},
        {o.order_id: o for o in orders},
    )
