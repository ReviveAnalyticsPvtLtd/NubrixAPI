"""Customer-free payment tests.

A strict fake Razorpay client raises if any Customer API call, token
enrollment, or recurring parameter is attempted, and records every outgoing
order payload so the tests can assert no customer_id/recurring fields are
sent. Two users share the same contact to prove contact is not identity.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.billing.manualPaymentService import (  # noqa: E402
    ManualPaymentService, _OperationStore,
)


class ForbiddenCallError(AssertionError):
    pass


class _FakeOrderAPI:
    def __init__(self, provider):
        self.provider = provider
        self.orders = {}
        self.createdPayloads = []

    def create(self, payload):
        self.provider.forbiddenFieldCheck(payload, context="order.create")
        self.createdPayloads.append(payload)
        orderId = f"order_{len(self.createdPayloads)}"
        order = {"id": orderId, "status": "created", "amount": payload["amount"], "currency": payload["currency"]}
        self.orders[orderId] = order
        return order

    def fetch(self, orderId):
        return self.orders.get(orderId)

    def payments(self, orderId):
        return {"items": list(self.provider.paymentsByOrder.get(orderId, []))}


class _FakePaymentAPI:
    def __init__(self, provider):
        self.provider = provider
        self.payments = {}

    def fetch(self, paymentId):
        return self.provider.payments.get(paymentId)

    def refund(self, paymentId, params=None):
        raise ForbiddenCallError(
            "payment.refund must go through the staff refund service, not "
            "the customer-free checkout path"
        )


class _FakeCustomerAPI:
    def __init__(self, provider):
        self.provider = provider

    def create(self, payload):
        raise ForbiddenCallError(f"customer.create called: {payload}")

    def fetch(self, customerId):
        raise ForbiddenCallError(f"customer.fetch called: {customerId}")

    def edit(self, customerId, updates):
        raise ForbiddenCallError(f"customer.edit called: {customerId} {updates}")


class _FakeTokenAPI:
    def __init__(self, provider):
        self.provider = provider

    def fetch(self, customerId, tokenId):
        raise ForbiddenCallError("token.fetch called in customer-free path")


class StrictFakeRazorpayClient:
    """Rejects every Customer/token/recurring usage and records payloads."""

    def __init__(self):
        self.paymentsByOrder = {}
        self.payments = {}
        self.forbiddenCalls = []
        self.order = _FakeOrderAPI(self)
        self.payment = _FakePaymentAPI(self)
        self.customer = _FakeCustomerAPI(self)
        self.token = _FakeTokenAPI(self)

    def forbiddenFieldCheck(self, payload, context):
        def field_names(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield str(key).lower()
                    yield from field_names(item)
            elif isinstance(value, list):
                for item in value:
                    yield from field_names(item)
        fields = set(field_names(payload))
        for forbidden in ("customer_id", "token", "recurring"):
            if forbidden in fields:
                self.forbiddenCalls.append((context, forbidden))
                raise ForbiddenCallError(
                    f"{context} contains forbidden field '{forbidden}': {payload}"
                )

    def addCapturedPayment(self, orderId, paymentId, amount, currency="INR", capturedAt=1760000000):
        self.payments[paymentId] = {
            "id": paymentId,
            "order_id": orderId,
            "amount": amount,
            "currency": currency,
            "status": "captured",
            "captured_at": capturedAt,
            "notes": {},
        }
        self.paymentsByOrder.setdefault(orderId, []).append(self.payments[paymentId])


class _FakeSupabaseClient:
    def __init__(self):
        self.rows = {
            "subscriptions": [],
            "Invoices": [],
            "Users": [],
            "billing_events": [],
        }
        self.inserts = []
        self.updates = []

    def table(self, name):
        client = self

        class _Table:
            def select(self, *_):
                return self

            def eq(self, *_):
                return self

            def order(self, *_a, **_k):
                return self

            def limit(self, _):
                return self

            def execute(self):
                class _R:
                    data = client.rows.get(name, [])
                return _R()

            def insert(self, payload):
                client.inserts.append((name, payload))

                class _R:
                    data = [payload]
                return _R()

            def update(self, payload):
                client.updates.append((name, payload))

                class _R:
                    data = [payload]
                return _R()

        return _Table()


_NOW = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)


def _service(provider=None, supabase=None):
    service = ManualPaymentService(
        razorpayClient=provider or StrictFakeRazorpayClient(),
        supabaseClient=supabase or _FakeSupabaseClient(),
        now=lambda: _NOW,
        store=_OperationStore(),
    )
    return service


def _payloadHashStub(payload):
    import hashlib
    import json

    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode()).hexdigest()


def test_create_checkout_never_calls_customer_apis():
    provider = StrictFakeRazorpayClient()
    service = _service(provider)
    intent = service.createCheckout(
        userId="user-a",
        purpose="initial_purchase",
        payload={
            "domains": ["banking"],
            "billingMode": "monthly_prepaid",
            "amount": 118000,
            "currency": "INR",
            "contact": "+919999999999",
        },
        requestKey="req-1",
    )
    assert intent.razorpayOrderId
    assert provider.order.createdPayloads, "order.create must have been called"
    for payload in provider.order.createdPayloads:
        serialized = str(payload).lower()
        assert "customer_id" not in serialized
        assert "'token'" not in serialized.lower()
        assert "recurring" not in serialized
        assert "max_amount" not in serialized


def test_two_users_sharing_one_contact_both_get_independent_orders():
    provider = StrictFakeRazorpayClient()
    service = _service(provider)
    intentA = service.createCheckout(
        userId="user-a",
        purpose="initial_purchase",
        payload={
            "domains": ["banking"],
            "billingMode": "monthly_prepaid",
            "amount": 118000,
            "currency": "INR",
            "contact": "+919999999999",
        },
        requestKey="req-a",
    )
    intentB = service.createCheckout(
        userId="user-b",
        purpose="initial_purchase",
        payload={
            "domains": ["banking"],
            "billingMode": "monthly_prepaid",
            "amount": 118000,
            "currency": "INR",
            "contact": "+919999999999",
        },
        requestKey="req-b",
    )
    assert intentA.razorpayOrderId != intentB.razorpayOrderId
    assert len(provider.order.createdPayloads) == 2


def test_same_request_key_same_payload_reuses_intent_without_second_order():
    provider = StrictFakeRazorpayClient()
    service = _service(provider)
    payload = {
        "domains": ["banking"],
        "billingMode": "monthly_prepaid",
        "amount": 118000,
        "currency": "INR",
    }
    first = service.createCheckout(
        userId="user-a", purpose="initial_purchase",
        payload=payload, requestKey="req-1",
    )
    second = service.createCheckout(
        userId="user-a", purpose="initial_purchase",
        payload=payload, requestKey="req-1",
    )
    assert first.attemptId == second.attemptId
    assert first.razorpayOrderId == second.razorpayOrderId
    assert len(provider.order.createdPayloads) == 1


def test_same_request_key_changed_payload_conflicts():
    provider = StrictFakeRazorpayClient()
    service = _service(provider)
    service.createCheckout(
        userId="user-a",
        purpose="initial_purchase",
        payload={
            "domains": ["banking"],
            "billingMode": "monthly_prepaid",
            "amount": 118000,
            "currency": "INR",
        },
        requestKey="req-1",
    )
    with pytest.raises(Exception):
        service.createCheckout(
            userId="user-a",
            purpose="initial_purchase",
            payload={
                "domains": ["banking", "telecom"],
                "billingMode": "monthly_prepaid",
                "amount": 236000,
                "currency": "INR",
            },
            requestKey="req-1",
        )


def test_attempt_expires_at_bounded_for_initial_purchase():
    provider = StrictFakeRazorpayClient()
    service = _service(provider)
    intent = service.createCheckout(
        userId="user-a",
        purpose="initial_purchase",
        payload={
            "domains": ["banking"],
            "billingMode": "monthly_prepaid",
            "amount": 118000,
            "currency": "INR",
        },
        requestKey="req-1",
    )
    assert intent.expiresAt is not None
    # configurable TTL default 1800s
    assert (intent.expiresAt - _NOW).total_seconds() <= 1800


def test_no_customer_call_for_any_purpose():
    provider = StrictFakeRazorpayClient()
    service = _service(provider)
    for purpose in ("renewal", "expert_addition", "topup"):
        service.createCheckout(
            userId="user-a",
            purpose=purpose,
            payload={
                "domains": ["banking"],
                "billingMode": "monthly_prepaid",
                "amount": 118000,
                "currency": "INR",
                "contact": "+919999999999",
            },
            requestKey=f"req-{purpose}",
        )
    assert provider.forbiddenCalls == []
    for payload in provider.order.createdPayloads:
        assert "customer_id" not in str(payload).lower()
