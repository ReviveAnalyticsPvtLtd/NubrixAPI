import hashlib
import hmac
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


if "logtail" not in sys.modules:
    logtailStub = types.ModuleType("logtail")

    class _DummyLogtailHandler:
        def __init__(self, *args, **kwargs):
            pass

    logtailStub.LogtailHandler = _DummyLogtailHandler
    sys.modules["logtail"] = logtailStub

if "loguru" not in sys.modules:
    loguruStub = types.ModuleType("loguru")

    class _DummyLogger:
        def remove(self, *args, **kwargs):
            return None

        def add(self, *args, **kwargs):
            return None

        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

    loguruStub.logger = _DummyLogger()
    sys.modules["loguru"] = loguruStub

if "fastapi" not in sys.modules:
    fastapiStub = types.ModuleType("fastapi")

    class _DummyHTTPException(Exception):
        pass

    fastapiStub.HTTPException = _DummyHTTPException
    sys.modules["fastapi"] = fastapiStub

if "api.commons" not in sys.modules:
    commonsStub = types.ModuleType("api.commons")
    commonsStub.client = object()
    commonsStub.verifyToken = lambda: "token"
    commonsStub.updateProjectModifiedAt = lambda *_args, **_kwargs: None
    sys.modules["api.commons"] = commonsStub

if "razorpay" not in sys.modules:
    razorpayStub = types.ModuleType("razorpay")

    class _DummyRazorpayClient:
        def __init__(self, *args, **kwargs):
            pass

    razorpayStub.Client = _DummyRazorpayClient
    sys.modules["razorpay"] = razorpayStub

if "redis" not in sys.modules:
    redisStub = types.ModuleType("redis")

    class _DummyRedis:
        def __init__(self, *args, **kwargs):
            pass

        def set(self, *args, **kwargs):
            return True

    redisStub.Redis = _DummyRedis
    sys.modules["redis"] = redisStub

if "jose" not in sys.modules:
    joseStub = types.ModuleType("jose")

    class _DummyJwt:
        @staticmethod
        def decode(*args, **kwargs):
            return {}

    joseStub.jwt = _DummyJwt
    sys.modules["jose"] = joseStub

if "requests" not in sys.modules:
    requestsStub = types.ModuleType("requests")
    requestsStub.post = lambda *args, **kwargs: None
    sys.modules["requests"] = requestsStub

if "supabase" not in sys.modules:
    supabaseStub = types.ModuleType("supabase")
    supabaseStub.create_client = lambda *args, **kwargs: None
    sys.modules["supabase"] = supabaseStub


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

    @patch("api.services.subscriptions.subscriptionService.jwt.decode", return_value={"userId": "u1", "email": "u@example.test"})
    def test_create_subscription_rejects_cancelled_but_unexpired_subscription(self, _mockDecode):
        service = SubscriptionService()
        service.client = _FakeClient({"Users": [{"userId": "u1"}]})
        service._getCanonicalSubscription = lambda **_kwargs: _subscription(status="cancelled")
        service._resolveCheckoutIdentity = lambda *_args: {
            "email": "u@example.test",
            "name": "User",
            "contact": "+919999999999",
        }
        service._getOrCreateRazorpayCustomer = lambda *_args: "cust_1"
        service._syncRazorpayCustomerIdentity = lambda *_args, **_kwargs: False
        service._auditLog = lambda *_args, **_kwargs: None
        service.razorpayClient = _FakeRazorpayClient()

        with self.assertRaises(CustomException) as raised:
            service.createSubscription(
                domains=["banking"],
                contact="+919999999999",
                billingMode="monthly_recurring",
                token="token",
            )

        self.assertEqual(raised.exception.statusCode, 409)

    @patch("api.services.subscriptions.subscriptionService.jwt.decode", return_value={"userId": "u1", "email": "u@example.test"})
    @patch("api.services.subscriptions.subscriptionService.computeInvoiceSnapshot")
    def test_create_subscription_allows_cancelled_after_period_end(self, mockSnapshot, _mockDecode):
        pastEnd = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        estimatedStart = datetime.now(timezone.utc).isoformat()
        estimatedEnd = (datetime.now(timezone.utc) + timedelta(days=31)).isoformat()
        mockSnapshot.return_value = types.SimpleNamespace(
            total_amount=1180,
            currency="INR",
            pricing_version="pricing_v1",
            pricing_reference_snapshot_json={"source": "unit_test"},
            tax=types.SimpleNamespace(
                tax_rule_version="tax_v1",
                tax_amount=180,
                to_dict=lambda: {"tax_amount": 180},
            ),
            amount_before_tax=1000,
            period_start=estimatedStart,
            period_end=estimatedEnd,
        )
        service = SubscriptionService()
        service.client = _FakeClient({"Users": [{"userId": "u1"}]})
        service._getCanonicalSubscription = lambda **_kwargs: _subscription(
            status="cancelled",
            periodEnd=pastEnd,
        )
        service._resolveCheckoutIdentity = lambda *_args: {
            "email": "u@example.test",
            "name": "User",
            "contact": "+919999999999",
        }
        service._createFrozenInvoiceFromSnapshot = lambda **_kwargs: {"id": "inv_new"}
        service._attachOrderToInvoice = lambda **_kwargs: None
        service._auditLog = lambda *_args, **_kwargs: None
        service.razorpayClient = _FakeRazorpayClient()

        result = service.createSubscription(
            domains=["banking"],
            contact="+919999999999",
            billingMode="monthly_recurring",
            token="token",
        )

        self.assertEqual(result["orderId"], "order_new")

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
    def test_verify_initial_subscription_rejects_amount_mismatch(self, _mockDecode):
        orderId = "order_initial_1"
        paymentId = "pay_initial_1"
        service = SubscriptionService()
        service.client = _FakeClient({
            "subscriptions": [_subscription()],
            "Invoices": [{
                "id": "inv_initial_1",
                "userId": "u1",
                "subscription_id": "sub_1",
                "billing_reason": "initial_purchase",
                "status": "payment_pending",
                "total_amount": 1180,
                "amount": 1180,
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
                        "type": "initial_subscription",
                        "userId": "u1",
                        "domains": "banking",
                        "billingMode": "monthly_recurring",
                        "invoiceId": "inv_initial_1",
                    },
                }
            },
            paymentById={
                paymentId: {
                    "id": paymentId,
                    "status": "captured",
                    "amount": 1179,
                    "currency": "INR",
                    "order_id": orderId,
                    "customer_id": "cust_1",
                    "token_id": "token_1",
                    "captured_at": 1700000000,
                }
            },
        )

        with self.assertRaises(CustomException) as raised:
            service.verifySubscription(
                payload={
                    "razorpayOrderId": orderId,
                    "razorpayPaymentId": paymentId,
                    "razorpaySignature": _signature(orderId, paymentId),
                },
                token="token",
            )
        self.assertEqual(raised.exception.statusCode, 400)

    @patch("api.services.subscriptions.subscriptionService.jwt.decode", return_value={"userId": "u1"})
    def test_verify_initial_annual_subscription_activates_without_token_when_invoice_matches(self, _mockDecode):
        orderId = "order_initial_annual"
        paymentId = "pay_initial_annual"
        service = SubscriptionService()
        service.client = _FakeClient({
            "subscriptions": [_subscription(status="trial", billingMode="none")],
            "Invoices": [{
                "id": "inv_initial_annual",
                "userId": "u1",
                "subscription_id": "sub_1",
                "billing_reason": "initial_purchase",
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
                        "type": "initial_subscription",
                        "userId": "u1",
                        "domains": "banking",
                        "billingMode": "annual_prepaid",
                        "invoiceId": "inv_initial_annual",
                    },
                }
            },
            paymentById={
                paymentId: {
                    "id": paymentId,
                    "status": "captured",
                    "amount": 12000,
                    "currency": "INR",
                    "order_id": orderId,
                    "customer_id": "cust_1",
                    "captured_at": 1700000000,
                }
            },
        )
        service._auditLog = lambda *_args, **_kwargs: None
        service._reissueTokenWithUpdatedClaims = lambda *_args, **_kwargs: "new-token"

        service.verifySubscription(
            payload={
                "razorpayOrderId": orderId,
                "razorpayPaymentId": paymentId,
                "razorpaySignature": _signature(orderId, paymentId),
            },
            token="token",
        )

        subscriptionUpdates = [
            update["payload"]
            for update in service.client.state["updates"]
            if update["table"] == "subscriptions"
        ]
        self.assertTrue(any(update.get("billing_mode") == "annual_prepaid" for update in subscriptionUpdates))
        self.assertTrue(any(update.get("razorpay_token_id") is None for update in subscriptionUpdates))

    @patch("api.services.subscriptions.subscriptionService.jwt.decode", return_value={"userId": "u1", "email": "u@example.test"})
    def test_annual_renewal_session_rejects_invoice_for_different_subscription(self, _mockDecode):
        service = SubscriptionService()
        service.client = _FakeClient({
            "Invoices": [{
                "id": "inv_renewal_1",
                "userId": "u1",
                "subscription_id": "sub_old",
                "billing_reason": "renewal",
                "status": "payment_pending",
                "total_amount": 12000,
                "amount": 12000,
                "currency": "INR",
            }],
            "subscriptions": [_subscription(billingMode="annual_prepaid")],
        })
        service.razorpayClient = _FakeRazorpayClient()

        with self.assertRaises(CustomException):
            service.createAnnualRenewalPaymentSession("inv_renewal_1", "token")

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
    def test_annual_renewal_verify_preserves_invoice_metadata_snapshot(self, _mockDecode):
        orderId = "order_renewal_2"
        paymentId = "pay_renewal_2"
        service = SubscriptionService()
        service.client = _FakeClient({
            "Users": [{"userId": "u1"}],
            "subscriptions": [_subscription(billingMode="annual_prepaid")],
            "Invoices": [{
                "id": "inv_renewal_2",
                "userId": "u1",
                "subscription_id": "sub_1",
                "billing_reason": "renewal",
                "status": "payment_pending",
                "total_amount": 12000,
                "amount": 12000,
                "currency": "INR",
                "razorpay_order_id": orderId,
                "metadata_json": {
                    "currentDomains": ["banking", "manufacturing"],
                    "renewalDomains": ["banking"],
                    "entitlementChangeEffectiveAt": "2026-05-26T00:00:00+00:00",
                },
            }],
        })
        service.razorpayClient = _FakeRazorpayClient(
            orderById={
                orderId: {
                    "id": orderId,
                    "status": "attempted",
                    "customer_id": "cust_1",
                    "notes": {
                        "type": "annual_renewal",
                        "userId": "u1",
                        "invoiceId": "inv_renewal_2",
                    },
                }
            },
            paymentById={
                paymentId: {
                    "id": paymentId,
                    "status": "authorized",
                    "amount": 12000,
                    "currency": "INR",
                    "order_id": orderId,
                    "customer_id": "cust_1",
                    "captured_at": None,
                }
            },
        )
        service._auditLog = lambda *_args, **_kwargs: None

        service.verifyAnnualRenewalPayment(
            payload={
                "invoiceId": "inv_renewal_2",
                "razorpayOrderId": orderId,
                "razorpayPaymentId": paymentId,
                "razorpaySignature": _signature(orderId, paymentId),
            },
            token="token",
        )

        invoiceUpdate = next(
            update["payload"]
            for update in service.client.state["updates"]
            if update["table"] == "Invoices"
        )
        metadata = invoiceUpdate["metadata_json"]
        self.assertEqual(metadata["currentDomains"], ["banking", "manufacturing"])
        self.assertEqual(metadata["renewalDomains"], ["banking"])
        self.assertEqual(metadata["entitlementChangeEffectiveAt"], "2026-05-26T00:00:00+00:00")
        self.assertTrue(metadata["verified"])

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
        self.assertEqual(invoiceUpdate["status"], "expired")
        self.assertNotIn("razorpay" + "InvoiceId", invoiceUpdate)
        self.assertNotIn("razorpay_" + "payment_" + "link_id", invoiceUpdate)
        self.assertNotIn("short" + "Url", invoiceUpdate)
        self.assertEqual(invoiceUpdate["metadata_json"]["currentDomains"], ["banking", "manufacturing"])
        self.assertTrue(invoiceUpdate["metadata_json"]["repricingRequired"])
        self.assertEqual(invoiceUpdate["metadata_json"]["repricingReason"], "pending_removals_changed")


if __name__ == "__main__":
    unittest.main()
