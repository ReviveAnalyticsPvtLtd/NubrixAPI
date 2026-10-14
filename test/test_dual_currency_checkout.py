"""Durable USD checkouts retain currency through provider orders and capture."""
from dataclasses import replace

import pytest

from test.test_manual_billing_runtime import database, sqlTransaction, read_row
from test.test_manual_checkout_http import checkout_database, request
from test.test_manual_payment_entrypoints import evidence
from test.test_customer_free_payments import StrictFakeRazorpayClient
from api.services.billing.manualPaymentService import ManualPaymentService


@pytest.mark.parametrize("mode", ["monthly_prepaid", "annual_prepaid"])
def test_usd_purchase_persists_currency(checkout_database, monkeypatch, mode):
    repository, path = checkout_database
    provider = StrictFakeRazorpayClient()
    service = ManualPaymentService.forProduction(provider, repository)
    for helper in ("_getMonthlyBasePrice", "_getAnnualBasePrice"):
        monkeypatch.setattr("api.services.billing.billingEngine." + helper,
                            lambda currency="INR": {"amount": 1739, "currency": currency, "plan_id": "unit_plan"})
    checkout = replace(request("usd-initial", mode=mode), currency="USD")
    intent = service.createCheckout(checkout)
    assert (intent.currency, provider.order.createdPayloads[-1]["currency"]) == ("USD", "USD")
    assert service.finalizeCapturedPayment(evidence(intent)).finalized
    assert read_row(path, "subscriptions")["default_currency"] == "USD"
    assert service.createCheckout(checkout).attemptId == intent.attemptId


def test_currency_change_cannot_adopt_same_key(checkout_database, monkeypatch):
    repository, _ = checkout_database
    service = ManualPaymentService.forProduction(StrictFakeRazorpayClient(), repository)
    monkeypatch.setattr("api.services.billing.billingEngine._getMonthlyBasePrice",
                        lambda currency="INR": {"amount": 1739, "currency": currency, "plan_id": "unit_plan"})
    checkout = replace(request("same-key"), currency="USD")
    service.createCheckout(checkout)
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        service.createCheckout(replace(checkout, currency="INR"))
