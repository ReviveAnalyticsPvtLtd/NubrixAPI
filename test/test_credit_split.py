import unittest
import types


from api.services.credits.creditConfig import getTokenQuotaForPlan


class CreditQuotaTests(unittest.TestCase):
    def _quota(self, plan_type, domain_count):
        try:
            return getTokenQuotaForPlan(plan_type, domain_count)
        except TypeError as exc:
            self.fail("credit quota calculation does not accept domain_count")

    def test_free_quota_uses_current_three_million_token_allowance(self):
        self.assertEqual(self._quota("free", 4), 3_000_000)

    def test_paid_quota_scales_by_domain_count(self):
        self.assertEqual(self._quota("pro", 1), 10_000_000)
        self.assertEqual(self._quota("pro", 4), 40_000_000)
        self.assertEqual(self._quota("annual", 2), 20_000_000)


class CreditQuotaResizeTests(unittest.TestCase):
    @staticmethod
    def _resize_targets(old_quota, current_remaining, new_quota, grant_immediately,
                        used_floor=0):
        try:
            from api.services.credits.creditService import calculateQuotaResizeTargets
        except ImportError as exc:
            raise AssertionError("quota resize target calculation is missing") from exc
        return calculateQuotaResizeTargets(
            oldQuota=old_quota,
            currentRemaining=current_remaining,
            newQuota=new_quota,
            grantImmediately=grant_immediately,
            usedFloor=used_floor,
        )

    def test_domain_removal_preserves_consumed_usage_above_new_quota(self):
        self.assertEqual(
            self._resize_targets(
                old_quota=40_000_000,
                current_remaining=5_000_000,
                new_quota=30_000_000,
                grant_immediately=False,
            ),
            {"remaining": 0, "used": 35_000_000},
        )

    def test_domain_addition_grants_only_the_new_headroom(self):
        self.assertEqual(
            self._resize_targets(
                old_quota=20_000_000,
                current_remaining=5_000_000,
                new_quota=30_000_000,
                grant_immediately=True,
            ),
            {"remaining": 15_000_000, "used": 15_000_000},
        )

    def test_later_resize_keeps_usage_already_above_current_quota(self):
        self.assertEqual(
            self._resize_targets(
                old_quota=30_000_000,
                current_remaining=0,
                new_quota=20_000_000,
                grant_immediately=False,
                used_floor=35_000_000,
            ),
            {"remaining": 0, "used": 35_000_000},
        )

    def test_deferred_increase_does_not_reduce_over_quota_usage(self):
        self.assertEqual(
            self._resize_targets(
                old_quota=10_000_000,
                current_remaining=0,
                new_quota=20_000_000,
                grant_immediately=False,
                used_floor=40_000_000,
            ),
            {"remaining": 0, "used": 40_000_000},
        )

    def test_durable_writeback_never_reduces_recorded_usage(self):
        try:
            from api.services.credits.creditService import preserveDurableUsedTokens
        except ImportError as exc:
            raise AssertionError("durable usage preservation helper is missing") from exc

        self.assertEqual(
            preserveDurableUsedTokens(
                storedUsed=35_000_000,
                quota=30_000_000,
                remaining=0,
            ),
            35_000_000,
        )

    def test_true_period_roll_ignores_previous_period_usage_floor(self):
        from api.services.credits.creditService import preserveDurableUsedTokens

        self.assertEqual(
            preserveDurableUsedTokens(
                storedUsed=35_000_000,
                quota=30_000_000,
                remaining=29_000_000,
                periodRolled=True,
            ),
            1_000_000,
        )

    def test_domain_resize_is_one_atomic_redis_mutation(self):
        from api.services.credits.creditService import CreditService

        class EvalOnlyRedis:
            def __init__(self):
                self.calls = []

            def eval(self, *args):
                self.calls.append(args)
                return [0, 35_000_000, 0, 40_000_000]

        class UpdateQuery:
            def __init__(self):
                self.payload = None

            def update(self, payload):
                self.payload = payload
                return self

            def select(self, *_args):
                return self

            def limit(self, *_args):
                return self

            def eq(self, *_args):
                return self

            def execute(self):
                return types.SimpleNamespace(data=[{'billing_mode':'annual_prepaid'}])

        class FakeSupabase:
            def __init__(self):
                self.query = UpdateQuery()

            def table(self, _name):
                return self.query

        redis_client = EvalOnlyRedis()
        supabase = FakeSupabase()
        service = CreditService()
        service._manualBalance = lambda _: None
        service.supabase = supabase
        service._dbRow = lambda _user_id: {
            "plan_tier": "pro",
            "monthly_token_quota": 40_000_000,
            "used_tokens": 35_000_000,
            "remaining_tokens": 5_000_000,
        }
        service._ensureHash = lambda _user_id: None
        service._redis = lambda: redis_client

        result = service.applyDomainCountChange("user-1", 3, False)

        self.assertTrue(result["applied"])
        self.assertEqual(len(redis_client.calls), 1)
        self.assertEqual(redis_client.calls[0][5], 35_000_000)
        self.assertEqual(supabase.query.payload["used_tokens"], 35_000_000)
        self.assertEqual(supabase.query.payload["remaining_tokens"], 0)

    def test_deduction_after_roll_does_not_restore_previous_period_usage(self):
        from api.services.credits.creditService import CreditService

        class UpdateQuery:
            def __init__(self):
                self.payload = None

            def update(self, payload):
                self.payload = payload
                return self

            def eq(self, *_args):
                return self

            def select(self, *_args):
                return self

            def limit(self, *_args):
                return self

            def execute(self):
                return types.SimpleNamespace(data=[{'billing_mode':'annual_prepaid'}])

        class FakeSupabase:
            def __init__(self):
                self.query = UpdateQuery()

            def table(self, _name):
                return self.query

        supabase = FakeSupabase()
        service = CreditService()
        service._manualBalance = lambda _: None
        service.supabase = supabase
        service._ensureHash = lambda _user_id: None
        service._deduct = lambda *_args: {
            "trem": 29_000_000,
            "ttop": 0,
            "spill": 0,
            "rolled": 1,
        }
        service._repairPeriod = lambda *_args: None
        service._dbRow = lambda _user_id: {
            "monthly_token_quota": 30_000_000,
            "used_tokens": 35_000_000,
        }

        service.deductTokens("user-1", 1_000_000, "reporting_query")

        self.assertEqual(supabase.query.payload["used_tokens"], 1_000_000)


class CreditPresentationTests(unittest.TestCase):
    @staticmethod
    def _presentation_module():
        try:
            from api.services.credits import creditPresentation
        except (ImportError, ModuleNotFoundError) as exc:
            raise AssertionError("creditPresentation module is missing") from exc
        return creditPresentation

    def test_fifo_window_reports_consumed_tokens_from_active_purchase_lot(self):
        presentation = self._presentation_module()
        self.assertEqual(
            presentation.deriveActiveTopupWindow(
                availableTokens=4_000_000,
                lotsNewestFirst=[5_000_000, 1_000_000],
            ),
            {"total": 5_000_000, "used": 1_000_000},
        )

    def test_balance_view_keeps_monthly_and_topup_usage_separate(self):
        presentation = self._presentation_module()
        view = presentation.buildCreditBalanceView({
            "planTier": "pro",
            "monthlyTokenQuota": 20_000_000,
            "usedTokens": 5_000_000,
            "topupTotalTokens": 5_000_000,
            "topupUsedTokens": 1_000_000,
            "periodStart": "2026-08-01T00:00:00+00:00",
            "periodEnd": "2026-09-01T00:00:00+00:00",
            "lastResetAt": "2026-08-01T00:00:00+00:00",
            "initialized": True,
        })

        self.assertEqual(view["monthlyCredits"], {
            "total": 2_000.0,
            "used": 500.0,
            "percentageUsed": 25.0,
        })
        self.assertEqual(view["topupCredits"], {
            "total": 500.0,
            "used": 100.0,
        })
        self.assertEqual(view["monthlyTokens"], {
            "total": 20_000_000,
            "used": 5_000_000,
        })
        self.assertEqual(view["topupTokens"], {
            "total": 5_000_000,
            "used": 1_000_000,
        })


if __name__ == "__main__":
    unittest.main()
