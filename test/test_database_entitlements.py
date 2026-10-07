import sys
import types
import os
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SUPABASE_URL", "https://example.test")
os.environ.setdefault("SUPABASE_KEY", "test-key")

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

if "supabase" not in sys.modules:
    supabaseStub = types.ModuleType("supabase")
    supabaseStub.create_client = lambda *args, **kwargs: None
    sys.modules["supabase"] = supabaseStub

if "supabase.lib.client_options" not in sys.modules:
    supabaseLibStub = types.ModuleType("supabase.lib")
    clientOptionsStub = types.ModuleType("supabase.lib.client_options")

    class _ClientOptions:
        def __init__(self, *args, **kwargs):
            pass

    clientOptionsStub.ClientOptions = _ClientOptions
    sys.modules["supabase.lib"] = supabaseLibStub
    sys.modules["supabase.lib.client_options"] = clientOptionsStub

from api.services.subscriptions.entitlementService import (
    EntitlementUnavailableError,
    SubscriptionEntitlement,
    SubscriptionEntitlementService,
    evaluateSubscriptionEntitlement,
)
from api.services.subscriptions.subscriptionFieldUtils import (
    CANONICAL_SUBSCRIPTION_SELECT,
)
from api.commons import (
    UserContext,
    requireActiveSubscription,
    requirePaidPlan,
    requireTrialOrAbove,
)


@pytest.fixture(autouse=True)
def _use_current_commons_module():
    """Keep dependency tests aligned with sibling test module reloads."""
    current = importlib.import_module("api.commons")
    globals().update(
        UserContext=current.UserContext,
        requireActiveSubscription=current.requireActiveSubscription,
        requirePaidPlan=current.requirePaidPlan,
        requireTrialOrAbove=current.requireTrialOrAbove,
    )


def _period_end(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _subscription(
    status: str,
    *,
    billing_mode: str = "monthly_recurring",
    plan_type: str | None = "pro",
    period_end: str | None = None,
) -> dict:
    return {
        "status": status,
        "billing_mode": billing_mode,
        "plan_type": plan_type,
        "current_period_end": period_end,
    }


def test_active_paid_subscription_has_all_paid_entitlements():
    result = evaluateSubscriptionEntitlement(
        "u1", _subscription("active", period_end=_period_end(10))
    )
    assert result.status == "active"
    assert result.planType == "pro"
    assert result.activeSubscription is True
    assert result.trialOrAbove is True
    assert result.paidPlan is True
    assert result.topupEligible is True


def test_valid_trial_is_trial_or_above_but_not_paid_or_active():
    result = evaluateSubscriptionEntitlement(
        "u1",
        _subscription(
            "trial",
            billing_mode="none",
            plan_type="free",
            period_end=_period_end(10),
        ),
    )
    assert result.trialOrAbove is True
    assert result.activeSubscription is False
    assert result.paidPlan is False
    assert result.topupEligible is False


def test_expired_or_malformed_trial_fails_closed():
    expired = evaluateSubscriptionEntitlement(
        "u1",
        _subscription(
            "trial",
            billing_mode="none",
            plan_type="free",
            period_end=_period_end(-1),
        ),
    )
    malformed = evaluateSubscriptionEntitlement(
        "u1",
        _subscription(
            "trial",
            billing_mode="none",
            plan_type="free",
            period_end="not-a-date",
        ),
    )
    assert expired.trialOrAbove is False
    assert malformed.trialOrAbove is False


def test_active_status_with_expired_period_fails_closed_before_cron_runs():
    result = evaluateSubscriptionEntitlement(
        "u1", _subscription("active", period_end=_period_end(-1))
    )
    assert result.activeSubscription is False
    assert result.paidPlan is False


def test_unexpired_cancelled_paid_plan_has_access_and_topup():
    result = evaluateSubscriptionEntitlement(
        "u1", _subscription("cancelled", period_end=_period_end(10))
    )
    assert result.activeSubscription is True
    assert result.paidPlan is True
    assert result.topupEligible is True


def test_expired_cancelled_paid_plan_has_no_access():
    result = evaluateSubscriptionEntitlement(
        "u1", _subscription("cancelled", period_end=_period_end(-1))
    )
    assert result.activeSubscription is False
    assert result.paidPlan is False


def test_inactive_statuses_fail_all_access_checks():
    for status in ("none", "past_due", "suspended", "paused", "expired"):
        result = evaluateSubscriptionEntitlement(
            "u1", _subscription(status, period_end=_period_end(10))
        )
        assert result.activeSubscription is False
        assert result.trialOrAbove is False
        assert result.paidPlan is False
        assert result.topupEligible is False


def test_missing_row_returns_explicit_no_entitlement_snapshot():
    result = evaluateSubscriptionEntitlement("u1", None)
    assert result.userId == "u1"
    assert result.status == "none"
    assert result.planType == "none"
    assert result.activeSubscription is False
    assert result.trialOrAbove is False
    assert result.paidPlan is False
    assert result.topupEligible is False


def test_missing_plan_type_is_derived_from_billing_mode():
    result = evaluateSubscriptionEntitlement(
        "u1",
        _subscription(
            "active",
            billing_mode="annual_prepaid",
            plan_type=None,
            period_end=_period_end(100),
        ),
    )
    assert result.planType == "annual"
    assert result.paidPlan is True


def test_invalid_stored_plan_type_is_derived_from_billing_mode():
    result = evaluateSubscriptionEntitlement(
        "u1",
        _subscription(
            "active",
            billing_mode="monthly_recurring",
            plan_type="unknown-tier",
            period_end=_period_end(10),
        ),
    )
    assert result.planType == "pro"


class _Response:
    def __init__(self, data):
        self.data = data


class _SubscriptionQuery:
    def __init__(
        self,
        rows=None,
        error: Exception | None = None,
        expected_user_id: str = "u1",
    ):
        self.rows = rows or []
        self.error = error
        self.expected_user_id = expected_user_id
        self.selected = None
        self.filters = []
        self.orders = []
        self.requested_limit = None

    def select(self, *args):
        self.selected = args
        return self

    def eq(self, *args):
        self.filters.append(args)
        return self

    def order(self, field, **kwargs):
        self.orders.append((field, kwargs.get("desc", False)))
        return self

    def limit(self, value):
        self.requested_limit = value
        return self

    def execute(self):
        if self.error:
            raise self.error
        assert self.selected == (CANONICAL_SUBSCRIPTION_SELECT,)
        assert ("user_id", self.expected_user_id) in self.filters
        # Canonical row selection: the entitlement service must filter on the
        # persisted is_canonical flag, never on updated_at ordering.
        assert ("is_canonical", True) in self.filters
        assert self.requested_limit == 1
        return _Response(list(self.rows)[: self.requested_limit])


class _Client:
    def __init__(self, rows=None, error=None):
        self.query = _SubscriptionQuery(rows, error)

    def table(self, name):
        assert name == "subscriptions"
        return self.query


def test_service_reads_and_evaluates_latest_canonical_row():
    row = _subscription("active", period_end=_period_end(10))
    result = SubscriptionEntitlementService(_Client([row])).get("u1")
    assert result.status == "active"
    assert result.activeSubscription is True


def test_service_requires_reviewed_canonical_flag_for_legacy_rows():
    # Legacy rows created before the expansion have is_canonical = false. The
    # service still resolves the deterministic latest row as a compatibility
    # fallback until operator-reviewed backfill promotes canonical rows.
    row = _subscription("active", period_end=_period_end(10))
    client = _Client([row])
    # First query (canonical) returns nothing; second query (fallback) rows.
    queries = []

    class _TwoPhaseClient:
        def __init__(self):
            self.phase = 0

        def table(self, name):
            assert name == "subscriptions"

            phase = self.phase
            self.phase += 1

            class _PhaseQuery(_SubscriptionQuery):
                def execute(self):
                    if phase == 0:
                        return _Response([])
                    return _Response([row])

            return _PhaseQuery([row])

    with pytest.raises(EntitlementUnavailableError):
        SubscriptionEntitlementService(_TwoPhaseClient()).get("u1")


def test_service_treats_missing_row_as_no_entitlement():
    result = SubscriptionEntitlementService(_Client([])).get("u1")
    assert result.status == "none"
    assert result.activeSubscription is False


def test_service_wraps_database_failure_without_falling_back():
    service = SubscriptionEntitlementService(
        _Client(error=RuntimeError("database unavailable"))
    )
    with pytest.raises(EntitlementUnavailableError):
        service.get("u1")


def _user() -> UserContext:
    return UserContext(userId="u1", email="u@example.test", token="legacy-token")


def _entitlement(**changes) -> SubscriptionEntitlement:
    values = {
        "userId": "u1",
        "status": "none",
        "planType": "none",
        "currentPeriodEnd": None,
        "activeSubscription": False,
        "trialOrAbove": False,
        "paidPlan": False,
        "topupEligible": False,
    }
    values.update(changes)
    return SubscriptionEntitlement(**values)


def test_original_expired_token_identity_is_allowed_after_database_upgrade():
    # The identity context intentionally has no plan fields. The string labels
    # the legacy browser token scenario without making its claims authoritative.
    user = UserContext(
        userId="u1",
        email="u@example.test",
        token="jwt-with-legacy-expired-claim",
    )
    current = _entitlement(
        status="active",
        planType="pro",
        activeSubscription=True,
        trialOrAbove=True,
        paidPlan=True,
        topupEligible=True,
    )
    with patch(
        "api.commons.subscriptionEntitlementService.get",
        return_value=current,
    ):
        assert requireActiveSubscription(user) is user


def test_original_active_token_identity_is_blocked_after_database_suspension():
    user = UserContext(
        userId="u1",
        email="u@example.test",
        token="jwt-with-legacy-active-claim",
    )
    current = _entitlement(status="suspended", planType="pro")
    with patch(
        "api.commons.subscriptionEntitlementService.get",
        return_value=current,
    ):
        with pytest.raises(HTTPException) as raised:
            requireActiveSubscription(user)
    assert raised.value.status_code == 403
    assert raised.value.detail["errorCode"] == "FEATURE_BLOCKED"


def test_active_gate_uses_current_database_entitlement():
    current = _entitlement(
        status="active",
        planType="pro",
        activeSubscription=True,
        trialOrAbove=True,
        paidPlan=True,
        topupEligible=True,
    )
    with patch(
        "api.commons.subscriptionEntitlementService.get",
        return_value=current,
    ):
        assert requireActiveSubscription(_user()).userId == "u1"


def test_active_gate_blocks_current_expired_database_state():
    with patch(
        "api.commons.subscriptionEntitlementService.get",
        return_value=_entitlement(status="expired", planType="pro"),
    ):
        with pytest.raises(HTTPException) as raised:
            requireActiveSubscription(_user())
    assert raised.value.status_code == 403
    assert raised.value.detail["errorCode"] == "FEATURE_BLOCKED"


def test_trial_gate_allows_current_trial_but_paid_gate_does_not():
    current = _entitlement(status="trial", planType="free", trialOrAbove=True)
    with patch(
        "api.commons.subscriptionEntitlementService.get",
        return_value=current,
    ):
        assert requireTrialOrAbove(_user()).userId == "u1"
        with pytest.raises(HTTPException):
            requirePaidPlan(_user())


def test_lookup_failure_returns_service_unavailable_not_upgrade_prompt():
    with patch(
        "api.commons.subscriptionEntitlementService.get",
        side_effect=EntitlementUnavailableError("unavailable"),
    ):
        with pytest.raises(HTTPException) as raised:
            requireActiveSubscription(_user())
    assert raised.value.status_code == 503
    assert raised.value.detail["errorCode"] == "ENTITLEMENT_UNAVAILABLE"
