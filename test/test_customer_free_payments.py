"""Customer-free payment tests.

A strict fake Razorpay client raises if any Customer API call, token
enrollment, or recurring parameter is attempted, and records every outgoing
order payload so the tests can assert no customer_id/recurring fields are
sent. Two users share the same contact to prove contact is not identity.
"""

class ForbiddenCallError(AssertionError):
    pass


class _FakeOrderAPI:
    def __init__(self, provider):
        self.provider = provider
        self.orders = {}
        self.createdPayloads = []

    def create(self, payload, **options):
        self.provider.forbiddenFieldCheck(payload, context="order.create")
        self.createdPayloads.append(payload)
        orderId = f"order_{len(self.createdPayloads)}"
        order = {"id": orderId, "status": "created", "amount": payload["amount"], "currency": payload["currency"]}
        self.orders[orderId] = order
        return order

    def fetch(self, orderId, **options):
        return self.orders.get(orderId)

    def payments(self, orderId, params=None, **options):
        items = list(self.provider.paymentsByOrder.get(orderId, []))
        if params:
            items = items[params['skip']:params['skip'] + params['count']]
        return {"items": items}


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
