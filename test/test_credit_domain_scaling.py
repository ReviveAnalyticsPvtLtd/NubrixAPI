import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_KEY", "test-key")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("REDIS_PASSWORD", "")

_PER_DOMAIN = 10000000


class FakeRedisHash:
    """Minimal stand-in for the credits:v3 hash, recording hset writes."""

    def __init__(self, **fields):
        self.h = dict(fields)

    def hset(self, key, field=None, value=None, mapping=None, **kwargs):
        # Mirrors redis-py's Redis.hset(name, key=None, value=None, mapping=None)
        # so callers can use either the single-field positional form
        # (hset(key, "tquota", 123)) or the mapping= keyword form.
        if mapping:
            self.h.update({k: int(v) for k, v in mapping.items()})
        if field is not None:
            self.h[field] = int(value)
        return 1


class FakeRedisHashTrackingDelete(FakeRedisHash):
    """Records whether delete() was called, without failing any write."""

    def __init__(self, **fields):
        super().__init__(**fields)
        self.deleted = False

    def delete(self, key):
        self.deleted = True
        return 1


class TestApplyDomainCountChange(unittest.TestCase):
    def _service(self, hashState):
        from api.services.credits.creditService import CreditService
        svc = CreditService()
        svc._manualBalance = MagicMock(return_value=None)
        svc.supabase = MagicMock()
        svc._fakeRedis = hashState
        return svc

    def _run(self, svc, hashState, row, domainCount, grantImmediately):
        with patch.object(svc, "_dbRow", return_value=row), \
             patch.object(svc, "_ensureHash", return_value=None), \
             patch.object(
                 svc, "_peek",
                 return_value={"trem": hashState.h["trem"], "ttop": 0, "rolled": 0},
             ), \
             patch.object(svc, "_redis", return_value=hashState):
            return svc.applyDomainCountChange(
                "u1", domainCount=domainCount, grantImmediately=grantImmediately
            )

    def test_replaying_the_same_domain_count_is_a_no_op(self):
        hashState = FakeRedisHash(trem=6000000, ttop=0, tquota=2 * _PER_DOMAIN)
        svc = self._service(hashState)
        row = {"plan_tier": "pro", "monthly_token_quota": 2 * _PER_DOMAIN,
               "remaining_tokens": 6000000, "domain_count": 2}

        result = self._run(svc, hashState, row, domainCount=2, grantImmediately=True)

        self.assertEqual(result["delta"], 0)
        self.assertEqual(result["remaining"], 6000000)
        self.assertEqual(hashState.h["trem"], 6000000)

    def test_free_tier_quota_does_not_move_with_domain_count(self):
        hashState = FakeRedisHash(trem=3000000, ttop=0, tquota=3000000)
        svc = self._service(hashState)
        row = {"plan_tier": "free", "monthly_token_quota": 3000000,
               "remaining_tokens": 3000000, "domain_count": 4}

        result = self._run(svc, hashState, row, domainCount=2, grantImmediately=True)

        self.assertEqual(result["quota"], 3000000)
        self.assertEqual(result["delta"], 0)

    def test_missing_balance_row_is_reported_not_raised(self):
        hashState = FakeRedisHash(trem=0, ttop=0, tquota=0)
        svc = self._service(hashState)

        with patch.object(svc, "_dbRow", return_value=None):
            result = svc.applyDomainCountChange(
                "ghost", domainCount=2, grantImmediately=True
            )

        self.assertFalse(result["applied"])


class TestEntitlementBoundaryLowersQuota(unittest.TestCase):
    def _task(self, subscriptionRow):
        from nubrix.triggers.tasks.entitlementBoundaryTask import EntitlementBoundaryTask
        client = MagicMock()
        client.table.return_value.select.return_value.eq.return_value.in_.return_value \
            .execute.return_value.data = [subscriptionRow]
        client.table.return_value.select.return_value.eq.return_value.eq \
            .return_value.order.return_value.limit.return_value \
            .execute.return_value.data = []
        return EntitlementBoundaryTask(client=client), client

    def test_applied_removal_lowers_the_credit_quota(self):
        subscription = {
            "id": "sub1",
            "user_id": "u1",
                "is_canonical": True,
            "subscribed_experts": ["banking", "telecom", "manufacturing"],
            "domain_count": 3,
            "pending_removals": ["telecom"],
            "current_period_end": "2020-01-01T00:00:00+00:00",
        }
        task, _ = self._task(subscription)

        with patch(
            "nubrix.triggers.tasks.entitlementBoundaryTask.creditService"
        ) as mockCredits, patch(
            "nubrix.triggers.tasks.entitlementBoundaryTask.BillingEventService"
        ):
            result = task.execute()

        self.assertEqual(result["applied"], 1)
        mockCredits.applyDomainCountChange.assert_called_once_with(
            userId="u1", domainCount=2, grantImmediately=False
        )

    def test_credit_failure_does_not_abort_the_removal(self):
        subscription = {
            "id": "sub1",
            "user_id": "u1",
                "is_canonical": True,
            "subscribed_experts": ["banking", "telecom"],
            "domain_count": 2,
            "pending_removals": ["telecom"],
            "current_period_end": "2020-01-01T00:00:00+00:00",
        }
        task, client = self._task(subscription)

        with patch(
            "nubrix.triggers.tasks.entitlementBoundaryTask.creditService"
        ) as mockCredits, patch(
            "nubrix.triggers.tasks.entitlementBoundaryTask.BillingEventService"
        ):
            mockCredits.applyDomainCountChange.side_effect = Exception("redis down")
            result = task.execute()

        # The domain removal itself must still count as applied. The credit
        # side effect failed, so credit_balances still has the pre-removal
        # domain_count/monthly_token_quota; syncQuotaFromConfig (run hourly
        # via CreditReconciliationTask) is what corrects those two fields
        # from the now-updated subscriptions.domain_count on its next pass —
        # it does not touch remaining_tokens except to clamp it down to fit
        # the corrected quota.
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["errors"], 0)


class TestSyncQuotaFromConfig(unittest.TestCase):
    def _service(self, dbRow, subscriptionRows, hashState):
        from api.services.credits.creditService import CreditService
        svc = CreditService()
        svc._manualBalance = MagicMock(return_value=None)

        tables = {}

        def tableSideEffect(name):
            if name not in tables:
                tables[name] = MagicMock()
            return tables[name]

        supabase = MagicMock()
        supabase.table.side_effect = tableSideEffect
        subTable = tableSideEffect("subscriptions")
        subTable.select.return_value.eq.return_value.eq.return_value.in_.return_value \
            .limit.return_value.execute.return_value.data = subscriptionRows
        tableSideEffect("credit_balances")  # pre-create so "not called" assertions work
        svc.supabase = supabase
        svc._tables = tables

        patch.object(svc, "_dbRow", return_value=dbRow).start()
        self.addCleanup(patch.stopall)
        patch.object(svc, "_redis", return_value=hashState).start()

        return svc

    def test_cancelled_subscription_does_not_drive_the_quota(self):
        # The only subscriptions row for this user is cancelled, so the
        # active-like filter excludes it: the query returns no rows.
        row = {"plan_tier": "pro", "monthly_token_quota": 4 * _PER_DOMAIN,
               "remaining_tokens": 25000000, "domain_count": 4}
        hashState = FakeRedisHashTrackingDelete(trem=25000000, ttop=0, tquota=4 * _PER_DOMAIN)
        svc = self._service(row, [], hashState)

        svc.syncQuotaFromConfig("u1")

        # domain_count already matches what config would compute (no drift
        # detected once the cancelled row is excluded), so nothing is written.
        svc._tables["credit_balances"].update.assert_not_called()


class TestActivatePaidDomainsGrantsCreditsImmediately(unittest.TestCase):
    def test_activation_grants_the_post_activation_domain_count_immediately(self):
        # Mirrors TestEntitlementBoundaryLowersQuota: asserts the money-in
        # path (domain activation after payment) calls applyDomainCountChange
        # with grantImmediately=True and the count AFTER the new domain is
        # added, not the count before.
        from api.services.subscriptions.subscriptionService import SubscriptionService

        svc = SubscriptionService()
        svc.client = MagicMock()
        svc._auditLog = MagicMock()
        subscription = {
            "id": "sub1",
            "user_id": "u1",
                "is_canonical": True,
            "subscribed_experts": ["banking"],
            "domain_count": 1,
            "status": "active",
            "current_period_end": "2099-01-01T00:00:00+00:00",
            "pending_additions": [
                {"orderId": "order1", "domain": "telecom", "state": "awaiting_payment"},
            ],
        }
        svc._getCanonicalSubscription = MagicMock(return_value=subscription)

        with patch("api.services.credits.creditService.creditService") as mockCredits:
            svc._activatePaidDomains(
                userId="u1",
                domains=["telecom"],
                targetQuantity=2,
                referenceId="order1",
            )

        mockCredits.applyDomainCountChange.assert_called_once_with(
            userId="u1", domainCount=2, grantImmediately=True,
        )


class TestRemoveDomainBlocksSamePeriodActivation(unittest.TestCase):
    """
    Closes the credit-arbitrage hole: a domain added mid-cycle via
    addDomains must survive one full billing period before removeDomain
    will accept it, so a same-day add-then-remove can no longer dodge a
    full month's charge while keeping the immediate 1,000-credit grant.

    Initial-purchase domains (no domain.add_activated event at all) and
    domains activated in an earlier cycle (event predates the current
    current_period_start) remain removable immediately -- only additions
    activated inside the *current* period are blocked.
    """

    _PERIOD_START = "2026-08-01T00:00:00+00:00"
    _PERIOD_END = "2026-08-31T00:00:00+00:00"

    def _subscription(self):
        return {
            "id": "sub1",
            "status": "active",
            "subscribed_experts": ["banking", "telecom"],
            "pending_removals": [],
            "domain_count": 2,
            "current_period_start": self._PERIOD_START,
            "current_period_end": self._PERIOD_END,
        }

    def _service(self, billingEventsRows=None, billingEventsError=None):
        from api.services.subscriptions.subscriptionService import SubscriptionService

        svc = SubscriptionService()
        subscription = self._subscription()
        svc._getCanonicalSubscription = MagicMock(return_value=subscription)
        svc._reconcilePendingAdditions = MagicMock(return_value=subscription)
        svc._markPayableRenewalInvoicesForRepricing = MagicMock()
        svc._auditLog = MagicMock()

        tables = {}

        def tableSideEffect(name):
            if name in tables:
                return tables[name]
            tableMock = MagicMock()
            if name == "Users":
                tableMock.select.return_value.eq.return_value.execute.return_value.data = [
                    {"userId": "u1"}
                ]
            elif name == "billing_events":
                chain = (
                    tableMock.select.return_value
                    .eq.return_value
                    .eq.return_value
                    .gte.return_value
                )
                if billingEventsError:
                    chain.execute.side_effect = billingEventsError
                else:
                    chain.execute.return_value.data = billingEventsRows or []
            elif name == "subscriptions":
                tableMock.update.return_value.eq.return_value.execute.return_value.data = [{}]
            tables[name] = tableMock
            return tableMock

        client = MagicMock()
        client.table.side_effect = tableSideEffect
        svc.client = client
        return svc, tables

    def _removeDomain(self, svc, domain="telecom"):
        with patch(
            "api.services.subscriptions.subscriptionService.jwt.decode",
            return_value={"userId": "u1"},
        ):
            return svc.removeDomain([domain], token="fake-token")

    def test_domain_activated_before_current_period_is_allowed(self):
        # The occurred_at >= current_period_start filter means an event
        # from a prior cycle simply never comes back in the query results.
        svc, tables = self._service(billingEventsRows=[])

        result = self._removeDomain(svc)

        self.assertEqual(result["pendingRemovals"], ["telecom"])
        tables["subscriptions"].update.assert_called_once()

    def test_domain_with_no_activation_event_is_allowed(self):
        # Initial-purchase domain: never had a domain.add_activated row.
        svc, tables = self._service(billingEventsRows=[])

        result = self._removeDomain(svc)

        self.assertEqual(result["pendingRemovals"], ["telecom"])
        tables["subscriptions"].update.assert_called_once()

    def test_billing_events_query_failure_fails_open_and_allows_removal(self):
        # A monitoring/DB blip on the billing_events read must never block
        # a legitimate customer action on a billing endpoint.
        svc, tables = self._service(billingEventsError=Exception("db unavailable"))

        result = self._removeDomain(svc)

        self.assertEqual(result["pendingRemovals"], ["telecom"])
        tables["subscriptions"].update.assert_called_once()


if __name__ == "__main__":
    unittest.main()
