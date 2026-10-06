import sys
import types
import unittest
from unittest.mock import patch
import json

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
    sys.modules["api.commons"] = commonsStub

if "requests" not in sys.modules:
    requestsStub = types.ModuleType("requests")
    requestsStub.post = lambda *args, **kwargs: None
    sys.modules["requests"] = requestsStub

if "supabase" not in sys.modules:
    supabaseStub = types.ModuleType("supabase")
    supabaseStub.create_client = lambda *args, **kwargs: None
    sys.modules["supabase"] = supabaseStub

if "redis" not in sys.modules:
    redisStub = types.ModuleType("redis")

    class _DummyRedis:
        def __init__(self, *args, **kwargs):
            pass

        def set(self, *args, **kwargs):
            return True

    redisStub.Redis = _DummyRedis
    sys.modules["redis"] = redisStub

if "razorpay" not in sys.modules:
    razorpayStub = types.ModuleType("razorpay")

    class _DummyRazorpayClient:
        def __init__(self, *args, **kwargs):
            pass

    razorpayStub.Client = _DummyRazorpayClient
    sys.modules["razorpay"] = razorpayStub

from api.services.webhookService import WebhookService
from nubrix.triggers.tasks.annualRenewalTask import AnnualRenewalTask
from nubrix.triggers.tasks.renewalLifecycleTask import RenewalLifecycleTask


class _FakeResponse:
    def __init__(self, data=None):
        self.data = data or []


class _FakeTable:
    def __init__(self, state):
        self.state = state
        self._data = []

    def select(self, *_args, **_kwargs):
        return self

    def update(self, payload):
        self.state["update"] = payload
        return self

    def insert(self, payload):
        self.state.setdefault("inserts", []).append(payload)
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def execute(self):
        return _FakeResponse(self._data)


class _FakeClient:
    def __init__(self):
        self.state = {}

    def table(self, _name):
        return _FakeTable(self.state)


class _FakeRedis:
    def set(self, *_args, **_kwargs):
        return True


class _RenewalTaskTable:
    def __init__(self, name, rowsByTable):
        self.name = name
        self.rowsByTable = rowsByTable
        self.filters = []
        self._limit = None

    @property
    def not_(self):
        return self

    def select(self, *_args, **_kwargs):
        return self

    def in_(self, field, values):
        self.filters.append(("in", field, values))
        return self

    def eq(self, field, value):
        self.filters.append(("eq", field, value))
        return self

    def is_(self, *_args, **_kwargs):
        return self

    def lte(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        self._limit = value
        return self

    def execute(self):
        rows = list(self.rowsByTable.get(self.name, []))
        for operation, field, value in self.filters:
            if operation == "eq":
                rows = [row for row in rows if row.get(field) == value]
            elif operation == "in":
                rows = [row for row in rows if row.get(field) in value]
        if self._limit is not None:
            rows = rows[: self._limit]
        return _FakeResponse(rows)


class _RenewalTaskClient:
    def __init__(self, rowsByTable):
        self.rowsByTable = rowsByTable

    def table(self, name):
        return _RenewalTaskTable(name, self.rowsByTable)


class RenewalEmailFlowTests(unittest.TestCase):
    def _serviceWithInvoice(self, invoice):
        service = WebhookService()
        service.client = _FakeClient()
        service._findInvoiceById = lambda _invoiceId: invoice
        service._sendRenewalNotification = lambda **kwargs: service.client.state.setdefault(
            "notification", kwargs
        )
        service._auditLog = lambda *args, **kwargs: None
        return service

    def test_webhook_dispatch_excludes_removed_provider_events(self):
        from api.services import webhookService as webhookModule

        self.assertNotIn("invoice" + ".paid", webhookModule.EVENT_HANDLERS)
        self.assertNotIn("invoice" + ".expired", webhookModule.EVENT_HANDLERS)
        self.assertNotIn("payment" + "_link.paid", webhookModule.EVENT_HANDLERS)
        self.assertNotIn("payment" + "_link.expired", webhookModule.EVENT_HANDLERS)
        self.assertFalse(hasattr(WebhookService, "_handleInvoice" + "Paid"))
        self.assertFalse(hasattr(WebhookService, "_handleInvoice" + "Expired"))
        self.assertFalse(hasattr(WebhookService, "_handlePaymentLink" + "Paid"))
        self.assertFalse(hasattr(WebhookService, "_handlePaymentLink" + "Expired"))

    def test_failed_invoice_update_preserves_metadata_snapshot(self):
        service = self._serviceWithInvoice({
            "id": "inv_internal",
            "status": "payment_pending",
            "userId": "u1",
            "metadata_json": {
                "currentDomains": ["banking", "manufacturing"],
                "renewalDomains": ["banking"],
            },
        })

        service._markInvoiceFailed("inv_internal", errorCode="BAD", errorDescription="failure")

        metadata = service.client.state["update"]["metadata_json"]
        self.assertEqual(metadata["currentDomains"], ["banking", "manufacturing"])
        self.assertEqual(metadata["renewalDomains"], ["banking"])
        self.assertEqual(metadata["error_code"], "BAD")

    def test_bounded_resend_allows_failed_attempts_until_limit(self):
        failedLogs = [
            {"metadata": {"deliveryStatus": "DELIVERY_FAILED"}},
            {"metadata": {"deliveryStatus": "DELIVERY_FAILED"}},
        ]
        self.assertTrue(WebhookService._shouldSendRenewalEmail(failedLogs, maxAttempts=3))
        self.assertFalse(WebhookService._shouldSendRenewalEmail(failedLogs, maxAttempts=2))
        self.assertFalse(
            WebhookService._shouldSendRenewalEmail(
                [{"metadata": {"deliveryStatus": "SENT"}}],
                maxAttempts=3,
            )
        )

    def test_reminder_email_reports_delivery_failed_on_provider_error(self):
        task = RenewalLifecycleTask.__new__(RenewalLifecycleTask)

        class _ProviderFailure:
            status_code = 500

        with patch(
            "nubrix.triggers.tasks.renewalLifecycleTask.requests.post",
            return_value=_ProviderFailure(),
        ), patch.dict("os.environ", {"RENEWAL_REMINDER_EMAIL_URL": "https://example.test"}):
            status = task._sendReminderEmail(
                user={"email": "user@example.test", "fullName": "User"},
                invoice={"id": "inv_1", "total_amount": 1000, "currency": "INR"},
                template="renewal_reminder_t1",
            )

        self.assertEqual(status, "DELIVERY_FAILED")

    @patch.dict(
        "os.environ",
        {
            "DASHBOARD_BASE_URL": "https://app.example.test",
            "RENEWAL_REMINDER_EMAIL_URL": "https://email.example.test",
            "SUPABASE_KEY": "service-key",
        },
    )
    def test_reminder_email_uses_dashboard_renewal_url_not_short_url(self):
        task = RenewalLifecycleTask.__new__(RenewalLifecycleTask)
        capturedPayload = {}

        class _ProviderOk:
            status_code = 200

        def _capturePost(*_args, **kwargs):
            body = kwargs.get("json")
            if body is None and kwargs.get("data"):
                body = json.loads(kwargs["data"])
            capturedPayload.update(body or {})
            return _ProviderOk()

        with patch(
            "nubrix.triggers.tasks.renewalLifecycleTask.requests.post",
            side_effect=_capturePost,
        ):
            status = task._sendReminderEmail(
                user={"email": "user@example.test", "fullName": "User"},
                invoice={
                    "id": "inv_reminder",
                    "total_amount": 1000,
                    "currency": "INR",
                },
                template="renewal_reminder_t1",
            )

        self.assertEqual(status, "SENT")
        self.assertEqual(
            capturedPayload["paymentUrl"],
            "https://app.example.test/settings/billing-details?renewalInvoiceId=inv_reminder",
        )

    @patch.dict(
        "os.environ",
        {
            "DASHBOARD_BASE_URL": "https://app.example.test",
            "RENEWAL_REMINDER_EMAIL_URL": "https://email.example.test",
            "SUPABASE_KEY": "service-key",
        },
    )
    def test_t7_email_uses_dashboard_renewal_url_not_short_url(self):
        task = AnnualRenewalTask.__new__(AnnualRenewalTask)
        task.redisClient = _FakeRedis()
        task.client = _FakeClient()
        capturedPayload = {}

        class _ProviderOk:
            status_code = 200

        def _capturePost(*_args, **kwargs):
            body = kwargs.get("json")
            if body is None and kwargs.get("data"):
                body = json.loads(kwargs["data"])
            capturedPayload.update(body or {})
            return _ProviderOk()

        with patch(
            "nubrix.triggers.tasks.annualRenewalTask.requests.post",
            side_effect=_capturePost,
        ):
            task._sendT7Email(
                user={"userId": "u1", "email": "user@example.test", "fullName": "User"},
                artifact={
                    "id": "inv_t7",
                    "total_amount": 1000,
                    "currency": "INR",
                    "due_date": "2026-06-01T00:00:00+00:00",
                },
                subscription={"subscribed_experts": ["banking"]},
            )

        self.assertEqual(
            capturedPayload["paymentUrl"],
            "https://app.example.test/settings/billing-details?renewalInvoiceId=inv_t7",
        )

    @patch.dict("os.environ", {"DASHBOARD_BASE_URL": "https://app.example.test"})
    def test_t7_sweep_prepares_dashboard_invoice_without_creating_hosted_artifact(self):
        task = AnnualRenewalTask.__new__(AnnualRenewalTask)
        invoice = {
            "id": "inv_t7",
            "userId": "u1",
            "subscription_id": "sub_1",
            "billing_reason": "renewal",
            "status": "upcoming",
            "due_date": "2026-06-01T00:00:00+00:00",
            "period_start": "2026-06-01T00:00:00+00:00",
            "period_end": "2027-06-01T00:00:00+00:00",
            "total_amount": 1000,
            "currency": "INR",
            "metadata_json": {},
        }
        user = {"userId": "u1", "email": "user@example.test", "fullName": "User"}
        subscription = {
            "id": "sub_1",
            "user_id": "u1",
            "is_canonical": True,
            "billing_mode": "annual_prepaid",
            "subscribed_experts": ["banking"],
        }
        task.client = _RenewalTaskClient({
            "Invoices": [invoice],
            "Users": [user],
            "subscriptions": [subscription],
        })
        sentEmails = []
        task._sendT7Email = lambda emailUser, artifact, emailSubscription: sentEmails.append(
            {
                "user": emailUser,
                "artifact": artifact,
                "subscription": emailSubscription,
            }
        )
        preparedInvoice = {
            **invoice,
            "status": "payment_pending",
            "payment_flow": "razorpay_order_checkout",
            "metadata_json": {
                "dashboardRenewalUrl": "https://app.example.test/settings/billing-details?renewalInvoiceId=inv_t7",
            },
        }

        with patch(
            "nubrix.triggers.tasks.annualRenewalTask.prepareDashboardRenewalInvoice",
            create=True,
            return_value=preparedInvoice,
        ) as mockPrepare:
            result = task._sweepT7()

        self.assertEqual(result, {"created": 1, "skipped": 0, "errors": 0})
        mockPrepare.assert_called_once_with(invoice)
        self.assertEqual(sentEmails[0]["artifact"]["payment_flow"], "razorpay_order_checkout")


if __name__ == "__main__":
    unittest.main()
