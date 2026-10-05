"""API contract tests for generic manual renewal routes and models."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.models import (  # noqa: E402
    CreateRenewalSessionRequest,
    CreateSubscriptionRequest,
    PrepareRenewalInvoiceRequest,
    VerifyRenewalPaymentRequest,
)
from api.routers.subscriptions import router  # noqa: E402


def _routeNames():
    return {route.name for route in router.routes}


def test_request_models_exist_with_expected_fields():
    prepare = PrepareRenewalInvoiceRequest()
    assert prepare is not None
    session = CreateRenewalSessionRequest(invoiceId="inv-1")
    assert session.invoiceId == "inv-1"
    verify = VerifyRenewalPaymentRequest(
        invoiceId="inv-1",
        razorpayOrderId="order_1",
        razorpayPaymentId="pay_1",
        razorpaySignature="sig",
    )
    assert verify.invoiceId == "inv-1"


def test_create_subscription_request_defaults_to_manual_monthly():
    request = CreateSubscriptionRequest(domains=["banking"], contact="+919999")
    assert request.billingMode == "monthly_prepaid"


def test_renewal_routes_are_registered():
    names = _routeNames()
    assert "prepareRenewalInvoice" in names
    assert "createRenewalPaymentSession" in names
    assert "verifyRenewalPayment" in names


def test_annual_wrappers_still_registered():
    names = _routeNames()
    assert "createAnnualRenewalPaymentSession" in names
    assert "verifyAnnualRenewalPayment" in names
    assert "createSubscription" in names
    assert "verifySubscription" in names