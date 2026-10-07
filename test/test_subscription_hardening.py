import hashlib
import hmac
import os
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


from utils.exceptionHandler import CustomException
from api.services.subscriptions.subscriptionService import SubscriptionService
from api.services.subscriptions.paymentValidationService import (
    calculateSubscriptionDaysLeft,
    PaymentValidationError,
    validateOrderPaymentAgainstInvoice,
)
from nubrix.components import subscriptionManager


class _FakeResponse:
    def __init__(self, data=None):
        self.data = data or []


class _NotFilter:
    def __init__(self, table):
        self.table = table

    def is_(self, field, value):
        self.table.notNullFields.add(field)
        return self.table


class _FakeTable:
    def __init__(self, name, state):
        self.name = name
        self.state = state
        self.filters = []
        self.notNullFields = set()
        self.updatePayload = None
        self.insertPayload = None
        self._limit = None
        self.not_ = _NotFilter(self)

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        self._limit = value
        return self

    def update(self, payload):
        self.updatePayload = payload
        return self

    def insert(self, payload):
        self.insertPayload = payload
        return self

    def execute(self):
        rows = list(self.state.get("rows", {}).get(self.name, []))
        for field in self.notNullFields:
            rows = [row for row in rows if row.get(field) is not None]
        for field, value in self.filters:
            rows = [row for row in rows if row.get(field) == value]
        if self.updatePayload is not None:
            self.state.setdefault("updates", []).append({
                "table": self.name,
                "filters": list(self.filters),
                "payload": dict(self.updatePayload),
            })
            for row in rows:
                row.update(self.updatePayload)
            return _FakeResponse(rows)
        if self.insertPayload is not None:
            payload = dict(self.insertPayload)
            self.state.setdefault("rows", {}).setdefault(self.name, []).append(payload)
            return _FakeResponse([payload])
        if self._limit is not None:
            rows = rows[: self._limit]
        return _FakeResponse(rows)


class _FakeClient:
    def __init__(self, rows=None):
        self.state = {"rows": rows or {}, "updates": []}

    def table(self, name):
        return _FakeTable(name, self.state)


class _FakeOrderApi:
    def __init__(self, orderById=None):
        self.orderById = orderById or {}
        self.createdPayloads = []

    def create(self, payload):
        self.createdPayloads.append(payload)
        return {"id": "order_new", "status": "created", "currency": payload.get("currency", "INR")}

    def fetch(self, orderId):
        return self.orderById.get(orderId, {"id": orderId, "status": "created", "notes": {}})

    def payments(self, _orderId):
        return {"items": []}


class _FakePaymentApi:
    def __init__(self, paymentById=None):
        self.paymentById = paymentById or {}

    def fetch(self, paymentId):
        return self.paymentById.get(paymentId, {"id": paymentId})


class _FakeTokenApi:
    def fetch(self, *_args, **_kwargs):
        return {"id": "token_1", "recurring": True, "recurring_details": {"status": "confirmed"}}


class _FakeRazorpayClient:
    def __init__(self, orderById=None, paymentById=None):
        self.order = _FakeOrderApi(orderById=orderById)
        self.payment = _FakePaymentApi(paymentById=paymentById)
        self.token = _FakeTokenApi()


def _signature(orderId, paymentId):
    return hmac.new(
        os.environ["RAZORPAY_KEY_SECRET"].encode(),
        f"{orderId}|{paymentId}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _subscription(status="active", billingMode="monthly_recurring", periodEnd=None):
    now = datetime.now(timezone.utc)
    return {
        "id": "sub_1",
        "user_id": "u1",
                "is_canonical": True,
        "billing_mode": billingMode,
        "status": status,
        "current_period_start": (now - timedelta(days=10)).isoformat(),
        "current_period_end": periodEnd or (now + timedelta(days=10)).isoformat(),
        "renewal_due_at": periodEnd or (now + timedelta(days=10)).isoformat(),
        "payment_collection_mode": "silent_token",
        "subscribed_experts": ["banking"],
        "domain_count": 1,
        "pending_removals": [],
        "pending_additions": [],
        "billing_state": {"state": "KA"},
        "razorpay_customer_id": "cust_1",
        "razorpay_token_id": "token_1",
        "subscription_anchor_day": 1,
        "recurring_failures": 0,
    }


class SubscriptionHardeningTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("SECRET_KEY", "unit_secret")
        os.environ.setdefault("RAZORPAY_KEY_SECRET", "unit_rzp_secret")
        os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_key")
        os.environ.setdefault("FREE_TRIAL_EXPIRY_WARNING_EMAIL_URL", "https://example.test/warn")
        os.environ.setdefault("SUPABASE_URL", "http://localhost")
        os.environ.setdefault("SUPABASE_KEY", "test-key")

    def test_timezone_helper_returns_aware_utc_values(self):
        from api.services.subscriptions.paymentValidationService import utcNow, utcFromTimestamp

        now = utcNow()
        fromTs = utcFromTimestamp(1700000000)

        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(now.utcoffset(), timedelta(0))
        self.assertIsNotNone(fromTs.tzinfo)
        self.assertEqual(fromTs.utcoffset(), timedelta(0))

    def test_subscription_days_left_helper_handles_timezone_aware_values(self):
        now = datetime(2026, 5, 20, 10, 30, tzinfo=timezone.utc)

        self.assertEqual(
            calculateSubscriptionDaysLeft("2026-06-26T00:00:00+00:00", now=now),
            37,
        )
        self.assertEqual(
            calculateSubscriptionDaysLeft("2026-05-19T23:59:00+00:00", now=now),
            0,
        )
        self.assertEqual(calculateSubscriptionDaysLeft(None, now=now), 0)
        self.assertEqual(calculateSubscriptionDaysLeft("not-a-date", now=now), 0)

    def test_cancelled_monthly_subscription_expires_after_period_end(self):
        pastEnd = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        fakeClient = _FakeClient({
            "subscriptions": [_subscription(status="cancelled", periodEnd=pastEnd)],
            "Users": [{"userId": "u1"}],
        })
        with patch.object(subscriptionManager, "create_client", return_value=fakeClient):
            subscriptionManager.recalculateSubscriptionDays()

        self.assertEqual(fakeClient.state["updates"][0]["payload"]["status"], "expired")

    def test_cancelled_annual_subscription_expires_after_period_end(self):
        pastEnd = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        fakeClient = _FakeClient({
            "subscriptions": [_subscription(
                status="cancelled",
                billingMode="annual_prepaid",
                periodEnd=pastEnd,
            )],
            "Users": [{"userId": "u1"}],
        })
        with patch.object(subscriptionManager, "create_client", return_value=fakeClient):
            subscriptionManager.recalculateSubscriptionDays()

        self.assertEqual(fakeClient.state["updates"][0]["payload"]["status"], "expired")

    def test_subscription_manager_refreshes_lifecycle_snapshot(self):
        futureEnd = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        fakeClient = _FakeClient({
            "subscriptions": [_subscription(status="active", periodEnd=futureEnd)],
            "Users": [{"userId": "u1"}],
        })

        with patch.object(subscriptionManager, "create_client", return_value=fakeClient):
            subscriptionManager.recalculateSubscriptionDays()

        update = next(
            item for item in fakeClient.state["updates"]
            if item["table"] == "subscriptions" and "billing_state" in item["payload"]
        )
        billingState = update["payload"]["billing_state"]

        self.assertEqual(billingState["state"], "KA")
        self.assertEqual(
            billingState["lifecycle_snapshot"]["subscription_days_left"],
            5,
        )
        self.assertEqual(
            billingState["lifecycle_snapshot"]["current_period_end"],
            futureEnd,
        )

    def test_subscription_manager_outbox_mode_enqueues_without_sending_http(self):
        periodEnd = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        fakeClient = _FakeClient({
            "subscriptions": [
                _subscription(status="trial", billingMode="none", periodEnd=periodEnd)
            ],
            "Users": [{"userId": "u1"}],
        })
        fakeService = types.SimpleNamespace(
            enqueueEligible=lambda *_args, **_kwargs: (
                {"id": "delivery-1", "status": "PENDING"},
                True,
            )
        )

        with patch.dict(os.environ, {"TRIAL_EXPIRY_EMAIL_MODE": "outbox"}), \
                patch.object(subscriptionManager, "create_client", return_value=fakeClient), \
                patch.object(
                    subscriptionManager,
                    "getTrialExpiryNotificationService",
                    return_value=fakeService,
                ), \
                patch.object(subscriptionManager, "_sendSubscriptionWarningMail") as send:
            summary = subscriptionManager.recalculateSubscriptionDays()

        self.assertEqual(summary["enqueued"], 1)
        self.assertEqual(summary["errors"], 0)
        send.assert_not_called()

    def test_subscription_manager_legacy_mode_sends_without_enqueueing(self):
        periodEnd = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        fakeClient = _FakeClient({
            "subscriptions": [
                _subscription(status="trial", billingMode="none", periodEnd=periodEnd)
            ],
            "Users": [{
                "userId": "u1",
                "email": "user@example.test",
                "fullName": "Test User",
            }],
        })

        with patch.dict(os.environ, {"TRIAL_EXPIRY_EMAIL_MODE": "legacy"}), \
                patch.object(subscriptionManager, "create_client", return_value=fakeClient), \
                patch.object(
                    subscriptionManager,
                    "getTrialExpiryNotificationService",
                ) as serviceFactory, \
                patch.object(subscriptionManager, "_sendSubscriptionWarningMail") as send:
            summary = subscriptionManager.recalculateSubscriptionDays()

        self.assertEqual(summary["enqueued"], 0)
        serviceFactory.assert_not_called()
        send.assert_called_once()

    def test_subscription_manager_disabled_mode_neither_enqueues_nor_sends(self):
        periodEnd = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        fakeClient = _FakeClient({
            "subscriptions": [
                _subscription(status="trial", billingMode="none", periodEnd=periodEnd)
            ],
            "Users": [{"userId": "u1"}],
        })

        with patch.dict(os.environ, {"TRIAL_EXPIRY_EMAIL_MODE": "disabled"}), \
                patch.object(subscriptionManager, "create_client", return_value=fakeClient), \
                patch.object(
                    subscriptionManager,
                    "getTrialExpiryNotificationService",
                ) as serviceFactory, \
                patch.object(subscriptionManager, "_sendSubscriptionWarningMail") as send:
            summary = subscriptionManager.recalculateSubscriptionDays()

        self.assertEqual(summary["enqueued"], 0)
        serviceFactory.assert_not_called()
        send.assert_not_called()



    def test_payment_validation_rejects_currency_order_and_uncaptured_status(self):
        invoice = {
            "id": "inv_1",
            "userId": "u1",
            "billing_reason": "initial_purchase",
            "status": "payment_pending",
            "total_amount": 1180,
            "currency": "INR",
            "razorpay_order_id": "order_1",
        }
        order = {
            "id": "order_1",
            "customer_id": "cust_1",
            "notes": {"type": "initial_subscription", "userId": "u1", "invoiceId": "inv_1"},
        }

        with self.assertRaises(PaymentValidationError):
            validateOrderPaymentAgainstInvoice(
                order=order,
                payment={
                    "id": "pay_1",
                    "status": "captured",
                    "amount": 1180,
                    "currency": "USD",
                    "order_id": "order_1",
                    "customer_id": "cust_1",
                },
                invoice=invoice,
                expectedType="initial_subscription",
                expectedUserId="u1",
                expectedCustomerId="cust_1",
                requestOrderId="order_1",
                requireCaptured=True,
            )

        with self.assertRaises(PaymentValidationError):
            validateOrderPaymentAgainstInvoice(
                order=order,
                payment={
                    "id": "pay_1",
                    "status": "captured",
                    "amount": 1180,
                    "currency": "INR",
                    "order_id": "order_other",
                    "customer_id": "cust_1",
                },
                invoice=invoice,
                expectedType="initial_subscription",
                expectedUserId="u1",
                expectedCustomerId="cust_1",
                requestOrderId="order_1",
                requireCaptured=True,
            )

        with self.assertRaises(PaymentValidationError):
            validateOrderPaymentAgainstInvoice(
                order=order,
                payment={
                    "id": "pay_1",
                    "status": "authorized",
                    "amount": 1180,
                    "currency": "INR",
                    "order_id": "order_1",
                    "customer_id": "cust_1",
                },
                invoice=invoice,
                expectedType="initial_subscription",
                expectedUserId="u1",
                expectedCustomerId="cust_1",
                requestOrderId="order_1",
                requireCaptured=True,
            )




    @patch("api.services.subscriptions.subscriptionService.jwt.decode", return_value={"userId": "u1"})
    def test_annual_renewal_verify_rejects_invoice_for_different_subscription(self, _mockDecode):
        orderId = "order_renewal_1"
        paymentId = "pay_renewal_1"
        service = SubscriptionService()
        service.client = _FakeClient({
            "Users": [{"userId": "u1"}],
            "subscriptions": [_subscription(billingMode="annual_prepaid")],
            "Invoices": [{
                "id": "inv_renewal_1",
                "userId": "u1",
                "subscription_id": "sub_old",
                "billing_reason": "renewal",
                "status": "payment_pending",
                "total_amount": 12000,
                "amount": 12000,
                "currency": "INR",
                "razorpay_order_id": orderId,
            }],
        })
        service.razorpayClient = _FakeRazorpayClient(
            orderById={
                orderId: {
                    "id": orderId,
                    "status": "paid",
                    "customer_id": "cust_1",
                    "notes": {
                        "type": "annual_renewal",
                        "userId": "u1",
                        "invoiceId": "inv_renewal_1",
                    },
                }
            },
            paymentById={
                paymentId: {
                    "id": paymentId,
                    "amount": 12000,
                    "currency": "INR",
                    "order_id": orderId,
                    "customer_id": "cust_1",
                }
            },
        )

        with self.assertRaises(CustomException):
            service.verifyAnnualRenewalPayment(
                payload={
                    "invoiceId": "inv_renewal_1",
                    "razorpayOrderId": orderId,
                    "razorpayPaymentId": paymentId,
                    "razorpaySignature": _signature(orderId, paymentId),
                },
                token="token",
            )


    @patch("api.services.subscriptions.subscriptionService.jwt.decode", return_value={"userId": "u1"})
    def test_remove_domain_marks_payable_renewal_invoice_for_repricing(self, _mockDecode):
        service = SubscriptionService()
        subscription = _subscription(billingMode="annual_prepaid")
        subscription.update({
            "subscribed_experts": ["banking", "manufacturing"],
            "domain_count": 2,
            "pending_removals": [],
        })
        service.client = _FakeClient({
            "Users": [{"userId": "u1"}],
            "subscriptions": [subscription],
            "Invoices": [{
                "id": "inv_renewal_existing",
                "subscription_id": "sub_1",
                "billing_reason": "renewal",
                "status": "payment_pending",
                "metadata_json": {"currentDomains": ["banking", "manufacturing"]},
            }],
        })
        service._getCanonicalSubscription = lambda **_kwargs: subscription
        service._reconcilePendingAdditions = lambda value: value
        service._auditLog = lambda *_args, **_kwargs: None

        service.removeDomain(["manufacturing"], "token")

        invoiceUpdate = next(
            update["payload"]
            for update in service.client.state["updates"]
            if update["table"] == "Invoices"
        )
        self.assertEqual(invoiceUpdate["status"], "EXPIRED")
        self.assertNotIn("razorpay" + "InvoiceId", invoiceUpdate)
        self.assertNotIn("razorpay_" + "payment_" + "link_id", invoiceUpdate)
        self.assertNotIn("short" + "Url", invoiceUpdate)
        self.assertEqual(invoiceUpdate["metadata_json"]["currentDomains"], ["banking", "manufacturing"])
        self.assertTrue(invoiceUpdate["metadata_json"]["repricingRequired"])
        self.assertEqual(invoiceUpdate["metadata_json"]["repricingReason"], "pending_removals_changed")


if __name__ == "__main__":
    unittest.main()
