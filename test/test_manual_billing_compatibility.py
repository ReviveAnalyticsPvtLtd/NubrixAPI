"""Compatibility tests: annual parity, contracted schema selectors, erasure."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.subscriptions.subscriptionFieldUtils import (  # noqa: E402
    CANONICAL_SUBSCRIPTION_SELECT,
    buildChurnResetPayload,
    mapBillingModeToPlanType,
)


def test_canonical_select_tolerates_contracted_schema():
    # The canonical select must keep working with the expansion columns and
    # still reference only columns that exist after the contract migration
    # drops razorpay_customer_id/token/anchor/recurring_failures.
    lowered = CANONICAL_SUBSCRIPTION_SELECT.lower()
    for dropped in (
        "razorpay_customer_id",
        "razorpay_token_id",
        "subscription_anchor_day",
        "recurring_failures",
    ):
        assert dropped not in lowered, (
            f"canonical select still references contracted column {dropped}"
        )
    for required in (
        "is_canonical",
        "renewal_opt_out",
        "billing_state",
        "pending_removals",
        "pending_additions",
    ):
        assert required in lowered


def test_monthly_prepaid_maps_to_pro():
    assert mapBillingModeToPlanType("monthly_prepaid", "active") == "pro"


def test_expired_monthly_maps_to_none_not_pro():
    assert mapBillingModeToPlanType("monthly_prepaid", "expired") == "none"


def test_legacy_monthly_recurring_still_maps_to_pro_for_history():
    assert mapBillingModeToPlanType("monthly_recurring", "active") == "pro"


def test_annual_and_trial_mappings_unchanged():
    assert mapBillingModeToPlanType("annual_prepaid", "active") == "annual"
    assert mapBillingModeToPlanType("none", "trial") == "free"
    assert mapBillingModeToPlanType("none", "expired") == "free"
    assert mapBillingModeToPlanType("none", "none") == "none"


def test_churn_reset_clears_active_entitlements_but_keeps_billing_mode():
    subscription = {
        "id": "sub-1",
        "status": "active",
        "billing_mode": "monthly_prepaid",
        "current_period_start": "2026-09-20T10:00:00+00:00",
        "current_period_end": "2026-10-20T10:00:00+00:00",
        "renewal_due_at": "2026-10-20T10:00:00+00:00",
        "subscribed_experts": ["banking"],
        "pending_removals": ["telecom"],
        "billing_state": {"manualBilling": {"lifecycleId": "lc-1"}},
    }
    payload = buildChurnResetPayload(subscription, "unpaid_expiry")
    assert payload is not None
    assert payload["current_period_start"] is None
    assert payload["current_period_end"] is None
    assert payload["pending_removals"] == []
    assert payload["pending_additions"] == []
    # subscription row keeps its name/identity and mode history
    assert "billing_mode" not in payload  # unchanged, not reset


def test_churn_reset_archives_previous_experts_in_snapshot():
    subscription = {
        "id": "sub-1",
        "status": "active",
        "billing_mode": "monthly_prepaid",
        "current_period_start": "2026-09-20T10:00:00+00:00",
        "current_period_end": "2026-10-20T10:00:00+00:00",
        "subscribed_experts": ["banking", "telecom"],
        "billing_state": {},
    }
    payload = buildChurnResetPayload(subscription, "unpaid_expiry")
    snapshot = payload["billing_state"]["churn_snapshot"]
    assert snapshot["previous_subscribed_experts"] == ["banking", "telecom"]


# --- annual early renewal coverage adapter ------------------------------------


def test_annual_early_renewal_preserves_current_coverage():
    from api.services.subscriptions.paymentValidationService import isAccessActive

    # Annual early renewal rewrites current_period_start to the previous
    # expiry (future-dated) while the earlier term is still usable. The
    # mode-aware coverage adapter must preserve that existing paid access.
    now = datetime(2026, 10, 10, 10, tzinfo=timezone.utc)
    annualEarlyRenewed = {
        "status": "active",
        "billing_mode": "annual_prepaid",
        "plan_type": "annual",
        "current_period_start": "2026-11-04T10:00:00+00:00",  # future-dated
        "current_period_end": "2027-11-04T10:00:00+00:00",
    }
    # The start is after now; coverage evaluation must not deny the earlier
    # paid term through the adapter path.
    from api.services.billing.monthlyCoverageService import MonthlyCoverageService

    service = MonthlyCoverageService(now=lambda: now)
    coverage = service.evaluateAnnualPaidCoverage(
        userId="u1",
        subscription=annualEarlyRenewed,
        historicalIntervals=[
            {
                "start": "2025-11-04T10:00:00+00:00",
                "end": "2026-11-04T10:00:00+00:00",
            }
        ],
        now=now,
    )
    assert coverage["paid"] is True


def test_annual_expired_history_does_not_grant_access():
    from datetime import datetime, timezone
    from api.services.billing.monthlyCoverageService import MonthlyCoverageService

    now = datetime(2026, 12, 1, 10, tzinfo=timezone.utc)
    service = MonthlyCoverageService(now=lambda: now)
    coverage = service.evaluateAnnualPaidCoverage(
        userId="u1",
        subscription={
            "status": "active",
            "billing_mode": "annual_prepaid",
            "current_period_start": "2025-11-04T10:00:00+00:00",
            "current_period_end": "2026-11-04T10:00:00+00:00",
        },
        historicalIntervals=[
            {"start": "2025-11-04T10:00:00+00:00", "end": "2026-11-04T10:00:00+00:00"}
        ],
        now=now,
    )
    assert coverage["paid"] is False


# --- erasure guard ------------------------------------------------------------


def test_erasure_repository_has_no_runtime_customer_cleanup():
    source = Path("api/services/userErasureRepository.py").read_text(
        encoding="utf-8"
    )
    lowered = source.lower()
    assert "customer.create" not in lowered
    assert "customer.fetch" not in lowered
    assert "customer.edit" not in lowered
    # erasure guards themselves must remain
    assert "erasure" in lowered


def test_authentication_seeding_is_customer_free_and_canonical():
    source = Path("api/services/authenticationService.py").read_text(
        encoding="utf-8"
    )
    lowered = source.lower()
    assert "monthly_recurring" not in lowered, (
        "new-user seeding must not reference the retired recurring mode"
    )
    for dropped in (
        "razorpay_customer_id",
        "razorpay_token_id",
        "subscription_anchor_day",
        "recurring_failures",
    ):
        assert dropped not in lowered, f"seeding writes contracted column {dropped}"
    # fresh shells are canonical rows
    assert "is_canonical" in lowered
    assert "ensurecanonicalsubscription" in lowered
    repository = Path("api/services/billing/manualBillingRepository.py").read_text(encoding="utf-8")
    assert "renewal_opt_out" in repository
