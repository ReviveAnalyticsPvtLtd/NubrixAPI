import os
import sys
import types
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from api.commons import requireCredits
from api.services.subscriptions.entitlementService import (
    EntitlementUnavailableError,
    SubscriptionEntitlement,
)


class _FakeUser:
    def __init__(self):
        self.userId = "u1"
        self.email = "u@example.test"
        self.token = "session-token"


def _entitlement(topupEligible=False):
    return SubscriptionEntitlement(
        userId="u1",
        status="active" if topupEligible else "trial",
        planType="pro" if topupEligible else "free",
        currentPeriodEnd="2026-09-01T00:00:00+00:00",
        activeSubscription=topupEligible,
        trialOrAbove=True,
        paidPlan=topupEligible,
        topupEligible=topupEligible,
    )


class TestRequireCreditsMonthly(unittest.TestCase):
    def _run(self, remaining, operationType="reporting_query", entitlement=None):
        dep = requireCredits(operationType)  # reporting_query minimum = 5000 tokens
        snapshot = {"remainingTokens": remaining, "periodEnd": "2026-08-01T00:00:00+00:00"}
        with patch("api.services.credits.creditService.creditService.getBalanceSnapshot",
                   return_value=snapshot), \
             patch("api.services.credits.creditService.creditService.getRemainingTokens",
                   return_value=remaining), \
             patch("api.commons.subscriptionEntitlementService.get",
                   return_value=entitlement or _entitlement(False)):
            return dep(user=_FakeUser())

    def test_allows_when_at_minimum(self):
        self.assertEqual(self._run(5000).userId, "u1")

    def test_allows_when_well_above_minimum(self):
        self.assertEqual(self._run(10000000).userId, "u1")

    def test_blocks_below_minimum_with_monthly_error(self):
        with self.assertRaises(HTTPException) as ctx:
            self._run(4999)
        self.assertEqual(ctx.exception.status_code, 402)
        self.assertEqual(ctx.exception.detail["errorCode"], "MONTHLY_QUOTA_EXHAUSTED")
        self.assertEqual(ctx.exception.detail["required"], 5000)
        self.assertEqual(ctx.exception.detail["remaining"], 4999)
        self.assertEqual(ctx.exception.detail["resetAt"], "2026-08-01T00:00:00+00:00")

    def test_blocks_at_zero(self):
        with self.assertRaises(HTTPException) as ctx:
            self._run(0)
        self.assertEqual(ctx.exception.detail["errorCode"], "MONTHLY_QUOTA_EXHAUSTED")

    def test_uses_per_operation_minimum(self):
        # transformation_message requires 10000 tokens, so 6000 passes reporting
        # but fails transformations.
        self.assertEqual(self._run(6000).userId, "u1")
        with self.assertRaises(HTTPException) as ctx:
            self._run(6000, operationType="transformation_message")
        self.assertEqual(ctx.exception.detail["required"], 10000)

    def test_unreadable_balance_is_retryable_503_not_fail_open(self):
        # -1 means the balance cannot be safely reconstructed: unavailable,
        # never fail-open paid access.
        with self.assertRaises(HTTPException) as ctx:
            self._run(-1)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail["errorCode"], "CREDIT_STATE_UNAVAILABLE")

    def test_expired_entitlement_is_blocked_before_quota_check(self):
        # Eligibility first: an expired subscription gets the feature-access
        # error even with a large stored balance.
        expired = SubscriptionEntitlement(
            userId="u1",
            status="expired",
            planType="none",
            currentPeriodEnd=None,
            activeSubscription=False,
            trialOrAbove=False,
            paidPlan=False,
            topupEligible=False,
        )
        dep = requireCredits("reporting_query")
        with patch("api.services.credits.creditService.creditService.getRemainingTokens",
                   return_value=10_000_000) as mockRemaining, \
             patch("api.commons.subscriptionEntitlementService.get",
                   return_value=expired):
            with self.assertRaises(HTTPException) as ctx:
                dep(user=_FakeUser())
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(ctx.exception.detail["errorCode"], "FEATURE_BLOCKED")
        # quota was never even consulted
        mockRemaining.assert_not_called()

    def test_current_paid_entitlement_offers_topup(self):
        with self.assertRaises(HTTPException) as ctx:
            self._run(0, entitlement=_entitlement(True))
        self.assertTrue(ctx.exception.detail["topupAvailable"])

    def test_current_trial_entitlement_does_not_offer_topup(self):
        with self.assertRaises(HTTPException) as ctx:
            self._run(0, entitlement=_entitlement(False))
        self.assertFalse(ctx.exception.detail["topupAvailable"])

    def test_unavailable_entitlement_is_retryable_503(self):
        # Eligibility is checked first: when it cannot be verified, the
        # request is unavailable rather than fail-open.
        dep = requireCredits("reporting_query")
        with patch(
            "api.services.credits.creditService.creditService.getBalanceSnapshot",
            return_value={"remainingTokens": 0, "periodEnd": "2026-08-01T00:00:00+00:00"},
        ), patch(
            "api.services.credits.creditService.creditService.getRemainingTokens",
            return_value=0,
        ), patch(
            "api.commons.subscriptionEntitlementService.get",
            side_effect=EntitlementUnavailableError("unavailable"),
        ) as mockGet:
            with self.assertRaises(HTTPException) as ctx:
                dep(user=_FakeUser())

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail["errorCode"], "ENTITLEMENT_UNAVAILABLE")
        mockGet.assert_called_once_with("u1")

    @patch("api.commons.subscriptionEntitlementService.get")
    def test_successful_credit_check_loads_eligibility_exactly_once(self, mockGet):
        # The new gate loads the entitlement once for eligibility, then
        # checks quota; success does not load it a second time for topups.
        dep = requireCredits("reporting_query")
        with patch(
            "api.services.credits.creditService.creditService.getRemainingTokens",
            return_value=5000,
        ):
            result = dep(user=_FakeUser())
        self.assertEqual(result.userId, "u1")
        self.assertEqual(mockGet.call_count, 1)

    def test_purchased_tokens_alone_satisfy_the_operation_minimum(self):
        self.assertEqual(self._run(5000).userId, "u1")


if __name__ == "__main__":
    unittest.main()
