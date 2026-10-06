import hashlib
import hmac
import asyncio
import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# --- import-time stubs (match existing test suite pattern) ---
os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("REDIS_PASSWORD", "")
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_key")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test-rzp-secret")

for name in ("logtail", "loguru", "redis"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
if not hasattr(sys.modules["logtail"], "LogtailHandler"):
    sys.modules["logtail"].LogtailHandler = lambda *a, **k: None
if not hasattr(sys.modules["loguru"], "logger"):
    class _L:
        def __getattr__(self, _):
            return lambda *a, **k: None
    sys.modules["loguru"].logger = _L()
if not hasattr(sys.modules["redis"], "Redis"):
    sys.modules["redis"].Redis = lambda *a, **k: None
if not hasattr(sys.modules["redis"], "ConnectionPool"):
    sys.modules["redis"].ConnectionPool = type("ConnectionPool", (), {})
supabaseStub = sys.modules.setdefault("supabase", types.ModuleType("supabase"))
if not hasattr(supabaseStub, "create_client"):
    supabaseStub.create_client = lambda *a, **k: None
sys.modules.setdefault("supabase.lib", types.ModuleType("supabase.lib"))
optsMod = sys.modules.setdefault("supabase.lib.client_options",
                                 types.ModuleType("supabase.lib.client_options"))
if not hasattr(optsMod, "ClientOptions"):
    optsMod.ClientOptions = lambda *a, **k: None
joseStub = sys.modules.setdefault("jose", types.ModuleType("jose"))
if not hasattr(joseStub, "jwt"):
    joseStub.jwt = types.SimpleNamespace(decode=lambda *a, **k: {})
if not hasattr(joseStub, "JWTError"):
    joseStub.JWTError = type("JWTError", (Exception,), {})
razorpayStub = sys.modules.setdefault("razorpay", types.ModuleType("razorpay"))
if not hasattr(razorpayStub, "Client"):
    razorpayStub.Client = lambda *a, **k: MagicMock()


class TestTopupPricingSnapshot(unittest.TestCase):
    def test_snapshot_prices_the_pack_and_freezes_its_tokens(self):
        from api.services.billing.billingEngine import computeTopupSnapshot
        snap = computeTopupSnapshot("medium", "monthly_recurring")
        self.assertEqual(snap.billing_reason, "add_on")
        self.assertEqual(snap.amount_before_tax, 199900)
        self.assertEqual(snap.domain_count, 1)
        self.assertEqual(snap.currency, "INR")
        self.assertEqual(snap.pricing_reference_snapshot_json["packId"], "medium")
        self.assertEqual(snap.pricing_reference_snapshot_json["tokens"], 5000000)
        self.assertEqual(snap.pricing_reference_snapshot_json["source"],
                         "config_credits_json")
        self.assertTrue(snap.pricing_version.startswith("topup_v4.0.0_medium_"))

    def test_total_includes_tax(self):
        from api.services.billing.billingEngine import computeTopupSnapshot
        snap = computeTopupSnapshot("small", "annual_prepaid")
        self.assertEqual(snap.total_amount, snap.amount_before_tax + snap.tax.tax_amount)
        self.assertGreaterEqual(snap.total_amount, snap.amount_before_tax)

    def test_unknown_pack_raises_before_any_pricing_work(self):
        from api.services.billing.billingEngine import computeTopupSnapshot
        with self.assertRaises(ValueError) as ctx:
            computeTopupSnapshot("enormous", "monthly_recurring")
        self.assertIn("enormous", str(ctx.exception))


class TestTopupEligibilityAndOrder(unittest.TestCase):
    def _service(self):
        from api.services.credits.topupService import TopupService
        svc = TopupService()
        svc.client = MagicMock()
        svc.razorpayClient = MagicMock()
        from api.services.billing.manualBillingContracts import CheckoutIntent
        from datetime import datetime,timezone,timedelta
        def reserve(request):
            from api.services.billing.billingEngine import computeTopupSnapshot
            snap = computeTopupSnapshot(request.payload['packId'], request.billingMode)
            payload = {'invoiceId': 'inv_1', 'lifecycleId': 'test-life',
                'packId': request.payload['packId'], 'tokens': snap.pricing_reference_snapshot_json['tokens']}
            repository.intent = CheckoutIntent('test-attempt', 'inv_1', request.userId, 'test-life', request.purpose,
                request.billingMode, 'selection-hash', snap.currency, 'created', 1, snap.total_amount,
                datetime.now(timezone.utc)+timedelta(minutes=30), None, payload)
            return repository.intent
        repository=MagicMock()
        repository.reserveCheckout.side_effect=reserve
        repository.claimProviderOrderCreation.return_value=True
        from dataclasses import replace
        repository.bindProviderOrder.side_effect = lambda attempt, order: replace(repository.intent, razorpayOrderId=order['id'])
        svc.reservationRepository = repository
        patcher=patch('api.services.billing.manualBillingRepository.getManualBillingRepository',return_value=repository)
        patcher.start()
        self.addCleanup(patcher.stop)
        return svc

    def _sub(self, status="active", planType="pro"):
        from datetime import datetime, timedelta, timezone
        start = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        end = (datetime.now(timezone.utc) + timedelta(days=20)).isoformat()
        return {"id": "sub_1", "status": status, "plan_type": planType,
                "user_id":"u1","billing_state":{"manualBilling":{"lifecycleId":"test-life"}},
                "billing_mode": "monthly_prepaid",
                "current_period_start": start, "current_period_end": end}

    def test_active_pro_is_eligible(self):
        svc = self._service()
        self.assertTrue(svc._isTopupEligible(self._sub()))

    def test_active_annual_is_eligible(self):
        svc = self._service()
        self.assertTrue(svc._isTopupEligible(self._sub(planType="annual")))

    def test_trial_is_not_eligible(self):
        svc = self._service()
        self.assertFalse(svc._isTopupEligible(self._sub(status="trial", planType="free")))

    def test_cancelled_pro_with_valid_paid_time_remains_eligible(self):
        # A cancelled monthly subscription whose already-paid coverage
        # remains valid can still purchase top-ups (design §9.5).
        svc = self._service()
        self.assertTrue(svc._isTopupEligible(self._sub(status="cancelled")))

    def test_expired_period_is_not_eligible_even_with_stored_topups(self):
        from datetime import datetime, timedelta, timezone
        svc = self._service()
        expired = self._sub()
        expired["current_period_start"] = (
            datetime.now(timezone.utc) - timedelta(days=40)
        ).isoformat()
        expired["current_period_end"] = (
            datetime.now(timezone.utc) - timedelta(days=10)
        ).isoformat()
        self.assertFalse(svc._isTopupEligible(expired))

    def test_missing_subscription_is_not_eligible(self):
        svc = self._service()
        self.assertFalse(svc._isTopupEligible(None))

    def test_list_packs_returns_credits_and_tax_inclusive_totals(self):
        svc = self._service()
        with patch.object(svc, "_decodeToken", return_value=("u1", "a@b.c")), \
             patch.object(svc, "_subscription", return_value=self._sub()):
            result = svc.listPacks(token="t")
        self.assertTrue(result["eligible"])
        medium = next(p for p in result["packs"] if p["packId"] == "medium")
        self.assertEqual(medium["tokens"], 5000000)
        self.assertEqual(medium["credits"], 500.0)
        self.assertEqual(medium["amountBeforeTax"], 199900)
        self.assertGreaterEqual(medium["totalAmount"], 199900)

    def test_list_packs_reports_ineligible_without_raising(self):
        svc = self._service()
        with patch.object(svc, "_decodeToken", return_value=("u1", "a@b.c")), \
             patch.object(svc, "_subscription", return_value=self._sub(status="trial",
                                                                      planType="free")):
            result = svc.listPacks(token="t")
        self.assertFalse(result["eligible"])
        self.assertEqual(len(result["packs"]), 3)

    def test_order_rejects_an_ineligible_user(self):
        svc = self._service()
        with patch.object(svc, "_decodeToken", return_value=("u1", "a@b.c")), \
             patch.object(svc, "_subscription", return_value=self._sub(status="trial",
                                                                      planType="free")):
            with self.assertRaises(Exception) as ctx:
                svc.createTopupOrder("medium", token="t")
        self.assertEqual(ctx.exception.errorCode, "TOPUP_NOT_ELIGIBLE")

    def test_order_rejects_an_unknown_pack_before_calling_razorpay(self):
        svc = self._service()
        with patch.object(svc, "_decodeToken", return_value=("u1", "a@b.c")), \
             patch.object(svc, "_subscription", return_value=self._sub()):
            with self.assertRaises(Exception) as ctx:
                svc.createTopupOrder("enormous", token="t")
        self.assertEqual(ctx.exception.errorCode, "TOPUP_PACK_UNKNOWN")
        svc.razorpayClient.order.create.assert_not_called()

    def test_order_creates_a_frozen_invoice_and_razorpay_order(self):
        svc = self._service()
        svc.razorpayClient.order.create.return_value = {
            "id": "order_abc", "currency": "INR", "amount": 235882}
        with patch.object(svc, "_decodeToken", return_value=("u1", "a@b.c")), \
             patch.object(svc, "_subscription", return_value=self._sub()), \
             patch.object(svc, "_identity", return_value={
                 "email": "a@b.c", "name": "A", "contact": "9", "customerId": "cust_1"}), \
             patch.object(svc, "_createInvoice", return_value={"id": "inv_1"}) as mkInv, \
             patch.object(svc, "_attachOrder") as attach, \
             patch.object(svc, "_audit"):
            result = svc.createTopupOrder("medium", token="t")

        self.assertEqual(result["orderId"], "order_abc")
        self.assertEqual(result["packId"], "medium")
        self.assertEqual(result["tokens"], 5000000)
        self.assertEqual(result["credits"], 500.0)
        self.assertEqual(result["invoiceId"], "inv_1")
        mkInv.assert_not_called()
        svc.reservationRepository.reserveCheckout.assert_called_once()
        attach.assert_not_called()

        notes = svc.razorpayClient.order.create.call_args[0][0]["notes"]
        self.assertEqual(notes["type"], "credit_topup")
        self.assertEqual(notes["userId"], "u1")
        self.assertEqual(notes["packId"], "medium")
        self.assertEqual(notes["invoiceId"], "inv_1")

    def test_order_amount_is_the_tax_inclusive_total(self):
        svc = self._service()
        svc.razorpayClient.order.create.return_value = {
            "id": "order_abc", "currency": "INR", "amount": 0}
        with patch.object(svc, "_decodeToken", return_value=("u1", "a@b.c")), \
             patch.object(svc, "_subscription", return_value=self._sub()), \
             patch.object(svc, "_identity", return_value={
                 "email": "a@b.c", "name": "A", "contact": "9", "customerId": "cust_1"}), \
             patch.object(svc, "_createInvoice", return_value={"id": "inv_1"}), \
             patch.object(svc, "_attachOrder"), patch.object(svc, "_audit"):
            svc.createTopupOrder("medium", token="t")
        sent = svc.razorpayClient.order.create.call_args[0][0]
        self.assertGreaterEqual(sent["amount"], 199900)


class TestTopupVerification(unittest.TestCase):
    def _service(self):
        from api.services.credits.topupService import TopupService
        svc = TopupService()
        svc.client = MagicMock()
        svc.razorpayClient = MagicMock()
        return svc

    @staticmethod
    def _signature(orderId, paymentId):
        return hmac.new(os.environ["RAZORPAY_KEY_SECRET"].encode(),
                        f"{orderId}|{paymentId}".encode(), hashlib.sha256).hexdigest()

    def _payload(self, orderId="order_abc", paymentId="pay_abc", signature=None):
        return {"razorpayOrderId": orderId, "razorpayPaymentId": paymentId,
                "razorpaySignature": signature or self._signature(orderId, paymentId)}

    def _order(self, userId="u1", packId="medium"):
        return {"id": "order_abc", "notes": {"userId": userId, "type": "credit_topup",
                                             "packId": packId, "invoiceId": "inv_1"}}

    def test_valid_payment_grants_tokens(self):
        svc = self._service()
        svc.razorpayClient.order.fetch.return_value = self._order()
        with patch.object(svc, "_decodeToken", return_value=("u1", "a@b.c")), \
             patch("api.services.credits.creditService.creditService.grantTopupTokens",
                   return_value={"granted": True, "tokens": 5000000}) as grant, \
             patch.object(svc, "_audit"):
            result = svc.verifyTopupPayment(self._payload(), token="t")
        self.assertEqual(result, {"granted": True, "tokens": 5000000, "credits": 500.0})
        grant.assert_called_once_with("u1", "order_abc", "pay_abc")

    def test_tampered_signature_is_rejected_before_any_grant(self):
        svc = self._service()
        with patch.object(svc, "_decodeToken", return_value=("u1", "a@b.c")), \
             patch("api.services.credits.creditService.creditService.grantTopupTokens") as grant:
            with self.assertRaises(Exception) as ctx:
                svc.verifyTopupPayment(self._payload(signature="deadbeef"), token="t")
        self.assertIn("Invalid Razorpay signature", str(ctx.exception))
        grant.assert_not_called()

    def test_order_belonging_to_another_user_is_rejected(self):
        svc = self._service()
        svc.razorpayClient.order.fetch.return_value = self._order(userId="someone_else")
        with patch.object(svc, "_decodeToken", return_value=("u1", "a@b.c")), \
             patch("api.services.credits.creditService.creditService.grantTopupTokens") as grant:
            with self.assertRaises(Exception) as ctx:
                svc.verifyTopupPayment(self._payload(), token="t")
        self.assertIn("mismatch", str(ctx.exception).lower())
        grant.assert_not_called()

    def test_non_topup_order_is_rejected(self):
        svc = self._service()
        order = self._order()
        order["notes"]["type"] = "domain_upgrade_proration"
        svc.razorpayClient.order.fetch.return_value = order
        with patch.object(svc, "_decodeToken", return_value=("u1", "a@b.c")), \
             patch("api.services.credits.creditService.creditService.grantTopupTokens") as grant:
            with self.assertRaises(Exception):
                svc.verifyTopupPayment(self._payload(), token="t")
        grant.assert_not_called()

    def test_missing_fields_are_rejected(self):
        svc = self._service()
        with patch.object(svc, "_decodeToken", return_value=("u1", "a@b.c")):
            with self.assertRaises(Exception) as ctx:
                svc.verifyTopupPayment({"razorpayOrderId": "order_abc"}, token="t")
        self.assertIn("Missing Razorpay verification fields", str(ctx.exception))

    def test_webhook_already_granted_reports_gracefully(self):
        svc = self._service()
        svc.razorpayClient.order.fetch.return_value = self._order()
        with patch.object(svc, "_decodeToken", return_value=("u1", "a@b.c")), \
             patch("api.services.credits.creditService.creditService.grantTopupTokens",
                   return_value={"granted": False, "tokens": 0}), \
             patch.object(svc, "_audit"):
            result = svc.verifyTopupPayment(self._payload(), token="t")
        self.assertEqual(result, {"granted": False, "tokens": 0, "credits": 0.0})


class TestTopupErrorCodeMapping(unittest.TestCase):
    def test_eligibility_failure_maps_to_403(self):
        from api.routers.credits import _topupStatusCode
        self.assertEqual(_topupStatusCode("TOPUP_NOT_ELIGIBLE: ..."), 403)

    def test_unknown_pack_maps_to_400(self):
        from api.routers.credits import _topupStatusCode
        self.assertEqual(_topupStatusCode("TOPUP_PACK_UNKNOWN: ..."), 400)

    def test_anything_else_maps_to_500(self):
        from api.routers.credits import _topupStatusCode
        self.assertEqual(_topupStatusCode("Razorpay exploded"), 500)

    def test_error_code_is_extracted_for_the_client(self):
        from api.routers.credits import _topupErrorCode
        self.assertEqual(_topupErrorCode("TOPUP_NOT_ELIGIBLE: ..."), "TOPUP_NOT_ELIGIBLE")
        self.assertEqual(_topupErrorCode("Razorpay exploded"), "TOPUP_FAILED")


class TestCreditReadContracts(unittest.TestCase):
    @staticmethod
    def _snapshot():
        return {
            "planTier": "pro",
            "monthlyTokenQuota": 10_000_000,
            "usedTokens": 2_500_000,
            "monthlyRemainingTokens": 7_500_000,
            "topupTokens": 3_500_000,
            "topupTotalTokens": 5_000_000,
            "topupUsedTokens": 1_500_000,
            "remainingTokens": 11_000_000,
            "monthlyCredits": 1000.0,
            "usedCredits": 250.0,
            "topupCredits": 350.0,
            "remainingCredits": 1100.0,
            "usagePercentage": 25.0,
            "periodStart": "2026-08-01T00:00:00+00:00",
            "periodEnd": "2026-09-01T00:00:00+00:00",
            "lastResetAt": "2026-08-01T00:00:00+00:00",
            "initialized": True,
        }

    def test_balance_returns_only_nested_public_buckets(self):
        from api.routers.credits import getCreditBalance

        user = types.SimpleNamespace(userId="u1")
        with patch(
            "api.services.credits.creditService.creditService.getBalanceSnapshot",
            return_value=self._snapshot(),
        ):
            response = asyncio.run(getCreditBalance(user=user))

        data = json.loads(response.body)["data"]
        self.assertEqual(
            set(data),
            {
                "planTier", "monthlyTokens", "topupTokens", "monthlyCredits",
                "topupCredits", "periodStart", "periodEnd", "lastResetAt",
                "initialized",
            },
        )
        self.assertEqual(data["monthlyCredits"]["percentageUsed"], 25.0)
        self.assertEqual(data["topupCredits"], {"total": 500.0, "used": 150.0})

    def test_usage_reuses_nested_balance_and_keeps_breakdown(self):
        from api.routers.credits import getCreditUsage

        user = types.SimpleNamespace(userId="u1")
        breakdown = {
            "langfuseAvailable": True,
            "byOperation": [{"operation": "reporting_query", "tokens": 1200}],
            "byModel": [{"model": "test-model", "tokens": 1200}],
        }
        with patch(
            "api.services.credits.creditService.creditService.getBalanceSnapshot",
            return_value=self._snapshot(),
        ), patch(
            "api.services.credits.langfuseUsageService.getUsageBreakdown",
            return_value=breakdown,
        ):
            response = asyncio.run(getCreditUsage(user=user))

        data = json.loads(response.body)["data"]
        self.assertEqual(data["monthlyTokens"], {"total": 10_000_000, "used": 2_500_000})
        self.assertEqual(data["topupTokens"], {"total": 5_000_000, "used": 1_500_000})
        self.assertEqual(data["monthlyCredits"]["percentageUsed"], 25.0)
        self.assertNotIn("percentageUsed", data["topupCredits"])
        self.assertNotIn("remainingCredits", data)
        self.assertNotIn("remainingTokens", data)
        self.assertTrue(data["langfuseAvailable"])
        self.assertEqual(data["breakdown"]["byOperation"], breakdown["byOperation"])


class TestTopupWebhooks(unittest.TestCase):
    def _service(self):
        from api.services.webhookService import WebhookService
        svc = WebhookService()
        svc.client = MagicMock()
        return svc

    @staticmethod
    def _captured(paymentType, userId="u1"):
        return {"payload": {"payment": {"entity": {
            "id": "pay_abc", "order_id": "order_abc", "amount": 235882,
            "currency": "INR",
            "notes": {"userId": userId, "type": paymentType,
                      "packId": "medium", "invoiceId": "inv_1"}}}}}

    @staticmethod
    def _refund():
        return {"payload": {"refund": {"entity": {
            "id": "rfnd_1", "payment_id": "pay_abc", "amount": 99950,
            "currency": "INR"}}}}

    @staticmethod
    def _razorpay(paymentNotes=None, fetchRaises=False):
        """WebhookService has no razorpayClient of its own — it borrows
        subscriptionService's, the same way _handlePaymentCaptured does."""
        rzp = MagicMock()
        if fetchRaises:
            rzp.payment.fetch.side_effect = Exception("razorpay down")
        else:
            rzp.payment.fetch.return_value = {"id": "pay_abc", "notes": paymentNotes}
        return patch("api.services.subscriptions.subscriptionService."
                     "subscriptionService.razorpayClient", rzp)

    def test_captured_topup_payment_grants_tokens(self):
        svc = self._service()
        with patch("api.services.credits.creditService.creditService.grantTopupTokens",
                   return_value={"granted": True, "tokens": 5000000}) as grant, \
             patch.object(svc, "_auditLog"):
            svc._handlePaymentCaptured(self._captured("credit_topup"))
        grant.assert_called_once_with("u1", "order_abc", "pay_abc")

    def test_captured_subscription_payment_never_grants_tokens(self):
        svc = self._service()
        with patch("api.services.credits.creditService.creditService.grantTopupTokens") as grant, \
             patch.object(svc, "_auditLog"):
            svc._handlePaymentCaptured(self._captured("some_other_type"))
        grant.assert_not_called()

    def test_topup_capture_without_userid_does_not_raise(self):
        svc = self._service()
        event = self._captured("credit_topup")
        del event["payload"]["payment"]["entity"]["notes"]["userId"]
        with patch("api.services.credits.creditService.creditService.grantTopupTokens") as grant, \
             patch.object(svc, "_auditLog"):
            svc._handlePaymentCaptured(event)
        grant.assert_not_called()

    def test_refund_of_a_topup_payment_claws_back(self):
        svc = self._service()
        with self._razorpay({"userId": "u1", "type": "credit_topup"}), \
             patch("api.services.credits.creditService.creditService.clawbackTopupTokens",
                   return_value={"clawed": True, "tokens": 2500000}) as claw, \
             patch.object(svc, "_auditLog"):
            svc._handleRefundProcessed(self._refund())
        claw.assert_called_once_with("u1", "rfnd_1", "pay_abc", 99950)

    def test_refund_of_a_subscription_payment_moves_no_tokens(self):
        svc = self._service()
        with self._razorpay({"userId": "u1", "type": "annual_renewal"}), \
             patch("api.services.credits.creditService.creditService.clawbackTopupTokens") as claw, \
             patch.object(svc, "_auditLog"):
            svc._handleRefundProcessed(self._refund())
        claw.assert_not_called()

    def test_refund_still_audits_when_the_payment_fetch_fails(self):
        svc = self._service()
        with self._razorpay(fetchRaises=True), \
             patch("api.services.credits.creditService.creditService.clawbackTopupTokens") as claw, \
             patch.object(svc, "_auditLog") as audit:
            svc._handleRefundProcessed(self._refund())
        claw.assert_not_called()
        audit.assert_called()


class TestStuckPurchaseSweep(unittest.TestCase):
    def _invoices(self, rows):
        client = MagicMock()
        chain = client.table.return_value.select.return_value
        chain.eq.return_value.in_.return_value.lt.return_value.execute.return_value = \
            MagicMock(data=rows)
        return client

    def test_paid_order_is_granted(self):
        from nubrix.triggers.tasks.creditReconciliationTask import sweepStuckTopupPurchases

        rows = [{"id": "inv_1", "userId": "u1", "razorpay_order_id": "order_abc"}]
        razorpay = MagicMock()
        razorpay.order.fetch.return_value = {"id": "order_abc", "status": "paid",
                                             "notes": {"userId": "u1"}}
        razorpay.order.payments.return_value = {"items": [{"id": "pay_abc",
                                                           "status": "captured"}]}
        with patch("api.commons.client", self._invoices(rows)), \
             patch("api.services.subscriptions.subscriptionService."
                   "subscriptionService.razorpayClient", razorpay), \
             patch("api.services.credits.creditService.creditService.grantTopupTokens",
                   return_value={"granted": True, "tokens": 5000000}) as grant:
            result = sweepStuckTopupPurchases()
        grant.assert_called_once_with("u1", "order_abc", "pay_abc")
        self.assertEqual(result, {"checked": 1, "granted": 1})

    def test_unpaid_order_is_left_alone(self):
        from nubrix.triggers.tasks.creditReconciliationTask import sweepStuckTopupPurchases

        rows = [{"id": "inv_1", "userId": "u1", "razorpay_order_id": "order_abc"}]
        razorpay = MagicMock()
        razorpay.order.fetch.return_value = {"id": "order_abc", "status": "created"}
        with patch("api.commons.client", self._invoices(rows)), \
             patch("api.services.subscriptions.subscriptionService."
                   "subscriptionService.razorpayClient", razorpay), \
             patch("api.services.credits.creditService.creditService.grantTopupTokens") as grant:
            result = sweepStuckTopupPurchases()
        grant.assert_not_called()
        self.assertEqual(result, {"checked": 1, "granted": 0})

    def test_no_stuck_invoices_is_a_clean_noop(self):
        from nubrix.triggers.tasks.creditReconciliationTask import sweepStuckTopupPurchases

        with patch("api.commons.client", self._invoices([])), \
             patch("api.services.credits.creditService.creditService.grantTopupTokens") as grant:
            self.assertEqual(sweepStuckTopupPurchases(), {"checked": 0, "granted": 0})
        grant.assert_not_called()


if __name__ == "__main__":
    unittest.main()
