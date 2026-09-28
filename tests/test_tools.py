"""Tool definitions, backend rules, and the structured error contract."""

import pytest

from support_agent.backend import SupportBackend
from support_agent.tools import TOOL_DEFINITIONS, TOOLS_BY_NAME, execute_tool

ERROR_KEYS = {"isError", "errorCategory", "errorCode", "isRetryable", "description"}


def run(backend: SupportBackend, name: str, **arguments):
    return execute_tool(backend, name, arguments)


class TestToolDefinitions:
    def test_exactly_the_four_support_tools(self):
        assert [d["name"] for d in TOOL_DEFINITIONS] == [
            "get_customer", "lookup_order", "process_refund", "escalate_to_human"
        ]

    @pytest.mark.parametrize("definition", TOOL_DEFINITIONS, ids=lambda d: d["name"])
    def test_description_states_when_and_when_not_to_use(self, definition):
        description = definition["description"]
        assert "Use this tool" in description
        assert "Do NOT use this tool when" in description
        assert "Errors" in description or "Returns" in description

    @pytest.mark.parametrize("definition", TOOL_DEFINITIONS, ids=lambda d: d["name"])
    def test_schema_rejects_unknown_arguments(self, definition):
        assert definition["input_schema"]["additionalProperties"] is False
        for prop in definition["input_schema"]["properties"].values():
            assert prop.get("description"), "every parameter needs a description"

    def test_similar_lookup_tools_point_to_each_other(self):
        get_customer = TOOLS_BY_NAME["get_customer"]["description"]
        lookup_order = TOOLS_BY_NAME["lookup_order"]["description"]
        assert "Use lookup_order" in get_customer
        assert "Call get_customer first" in lookup_order

    def test_refund_and_escalation_tools_point_to_each_other(self):
        assert "escalate_to_human" in TOOLS_BY_NAME["process_refund"]["description"]
        assert "resolve the concern yourself" in TOOLS_BY_NAME["escalate_to_human"]["description"]


class TestGetCustomer:
    def test_by_email_returns_order_summaries_only(self, backend):
        result = run(backend, "get_customer", email="Alice@Example.com")
        assert not result.is_error
        assert result.payload["customer_id"] == "CUST-1001"
        order = result.payload["orders"][0]
        assert set(order) == {"order_id", "placed_on", "status", "order_total"}

    def test_by_customer_id(self, backend):
        assert run(backend, "get_customer", customer_id="cust-1002").payload["name"] == "Bob Martinez"

    @pytest.mark.parametrize(
        ("arguments", "code"),
        [
            ({}, "INVALID_ARGUMENTS"),
            ({"email": "alice@example.com", "customer_id": "CUST-1001"}, "INVALID_ARGUMENTS"),
            ({"email": "not-an-email"}, "INVALID_EMAIL"),
            ({"customer_id": "1001"}, "INVALID_CUSTOMER_ID"),
            ({"email": "alice@exmaple.com"}, "CUSTOMER_NOT_FOUND"),
        ],
    )
    def test_validation_errors(self, backend, arguments, code):
        result = execute_tool(backend, "get_customer", arguments)
        assert result.is_error
        assert result.error_category == "validation"
        assert result.error_code == code


class TestLookupOrder:
    def test_returns_charges_and_refund_eligibility(self, backend):
        order = run(backend, "lookup_order", order_id="ORD-1001").payload
        assert order["amount_charged"] == 259.98
        assert order["refundable_amount"] == 259.98
        assert [c["possible_duplicate"] for c in order["charges"]] == [False, True]
        assert order["return_window"]["open"] is True

    def test_transient_failure_then_success(self, backend):
        first = run(backend, "lookup_order", order_id="ORD-1004")
        assert first.is_error
        assert first.error_category == "transient"
        assert first.payload["isRetryable"] is True

        second = run(backend, "lookup_order", order_id="ORD-1004")
        assert not second.is_error
        assert second.payload["return_window"]["open"] is False

    @pytest.mark.parametrize(
        ("order_id", "code"), [("1004", "INVALID_ORDER_ID"), ("ORD-2001", "ORDER_NOT_FOUND")]
    )
    def test_validation_errors_point_to_get_customer(self, backend, order_id, code):
        result = run(backend, "lookup_order", order_id=order_id)
        assert result.error_code == code
        assert "get_customer" in result.payload["description"]


class TestProcessRefund:
    def test_duplicate_charge_refund_succeeds(self, backend):
        result = run(backend, "process_refund", customer_id="CUST-1001", order_id="ORD-1001",
                     amount=129.99, reason="duplicate_charge")
        assert not result.is_error
        assert result.payload["refund_id"].startswith("RF-")
        assert result.payload["remaining_refundable"] == 129.99

    @pytest.mark.parametrize(
        ("arguments", "category", "code"),
        [
            ({"customer_id": "CUST-1002", "order_id": "ORD-1004", "amount": 120, "reason": "changed_mind"},
             "business", "RETURN_WINDOW_EXPIRED"),
            ({"customer_id": "CUST-1002", "order_id": "ORD-1005", "amount": 75, "reason": "not_received"},
             "business", "ORDER_STILL_IN_TRANSIT"),
            ({"customer_id": "CUST-1002", "order_id": "ORD-1005", "amount": 75, "reason": "changed_mind"},
             "business", "ORDER_NOT_DELIVERED"),
            ({"customer_id": "CUST-1001", "order_id": "ORD-1002", "amount": 89, "reason": "duplicate_charge"},
             "business", "NO_DUPLICATE_CHARGE_FOUND"),
            ({"customer_id": "CUST-1003", "order_id": "ORD-1006", "amount": 60, "reason": "damaged_item"},
             "permission", "ACCOUNT_REFUNDS_RESTRICTED"),
            ({"customer_id": "CUST-1002", "order_id": "ORD-1001", "amount": 10, "reason": "other"},
             "permission", "ORDER_NOT_OWNED_BY_CUSTOMER"),
            ({"customer_id": "CUST-1001", "order_id": "ORD-1002", "amount": 500, "reason": "damaged_item"},
             "validation", "AMOUNT_EXCEEDS_REFUNDABLE"),
            ({"customer_id": "CUST-1001", "order_id": "ORD-1002", "amount": -5, "reason": "damaged_item"},
             "validation", "INVALID_ARGUMENTS"),
            ({"customer_id": "CUST-1001", "order_id": "ORD-1002", "amount": 5, "reason": "because"},
             "validation", "INVALID_ARGUMENTS"),
        ],
    )
    def test_errors_are_categorized(self, backend, arguments, category, code):
        result = execute_tool(backend, "process_refund", arguments)
        assert result.is_error
        assert (result.error_category, result.error_code) == (category, code)
        assert not backend.refunds

    def test_already_refunded_is_a_business_error(self, backend):
        args = {"customer_id": "CUST-1001", "order_id": "ORD-1002", "reason": "damaged_item"}
        assert not execute_tool(backend, "process_refund", {**args, "amount": 89}).is_error
        second = execute_tool(backend, "process_refund", {**args, "amount": 1})
        assert (second.error_category, second.error_code) == ("business", "ALREADY_REFUNDED")


class TestEscalateToHuman:
    def test_opens_ticket_in_queue_for_reason(self, backend):
        result = run(backend, "escalate_to_human", reason_category="account_restricted",
                     summary="Carol Smith (CUST-1003) wants a refund but refunds are on hold.",
                     priority="high")
        assert result.payload == {
            "ticket_id": "ESC-5001",
            "status": "open",
            "queue": "trust-and-safety",
            "priority": "high",
            "expected_first_response": "within 4 hours",
        }

    def test_summary_must_stand_alone(self, backend):
        result = run(backend, "escalate_to_human", reason_category="policy_exception", summary="help")
        assert result.error_code == "SUMMARY_TOO_SHORT"


class TestErrorContract:
    @pytest.mark.parametrize(
        ("name", "arguments"),
        [
            ("lookup_order", {"order_id": "ORD-1004"}),
            ("lookup_order", {"order_id": "bad"}),
            ("process_refund", {"customer_id": "CUST-1002", "order_id": "ORD-1004", "amount": 120,
                                "reason": "changed_mind"}),
            ("process_refund", {"customer_id": "CUST-1003", "order_id": "ORD-1006", "amount": 60,
                                "reason": "damaged_item"}),
            ("no_such_tool", {}),
        ],
    )
    def test_every_error_has_the_structured_fields(self, backend, name, arguments):
        result = execute_tool(backend, name, arguments)
        assert result.is_error
        assert ERROR_KEYS <= set(result.payload)
        assert result.payload["isRetryable"] is (result.payload["errorCategory"] == "transient")
        assert len(result.payload["description"]) > 20
