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
if "supabase" not in sys.modules:
    supabaseStub = types.ModuleType("supabase")
    supabaseStub.create_client = lambda *a, **k: None
    sys.modules["supabase"] = supabaseStub
    optsMod = types.ModuleType("supabase.lib.client_options")
    optsMod.ClientOptions = lambda *a, **k: None
    libMod = types.ModuleType("supabase.lib")
    sys.modules["supabase.lib"] = libMod
    sys.modules["supabase.lib.client_options"] = optsMod


_FALLBACK = 2678400


class FakeTopupState:
    """In-memory mirror of the v3 Redis hash including the purchased bucket."""

    def __init__(self, trem, tquota, pend, pnext, ttop=0):
        self.h = dict(trem=trem, ttop=ttop, tquota=tquota, pend=pend, pnext=pnext)

    def _roll(self, now):
        rolled = 0
        guard = 0
        while now >= self.h["pend"] and guard < 120:
            self.h["trem"] = self.h["tquota"]
            self.h["pend"] = self.h["pnext"]
            self.h["pnext"] = self.h["pnext"] + _FALLBACK
            rolled = 1
            guard += 1
        return rolled

    def peek(self, now):
        rolled = self._roll(now)
        return {"trem": max(0, self.h["trem"]), "ttop": max(0, self.h["ttop"]),
                "rolled": rolled}

    def deduct(self, n, now):
        rolled = self._roll(now)
        spill = 0
        self.h["trem"] -= n
        if self.h["trem"] < 0:
            spill = min(-self.h["trem"], self.h["ttop"])
            self.h["ttop"] -= spill
            self.h["trem"] = 0
        return {"trem": self.h["trem"], "ttop": self.h["ttop"],
                "spill": spill, "rolled": rolled}


class TestTopupBucketMechanics(unittest.TestCase):
    def test_monthly_bucket_is_spent_before_purchased(self):
        s = FakeTopupState(trem=10000, ttop=5000000, tquota=10000000,
                           pend=4102444800, pnext=4105123200)
        state = s.deduct(4000, 0)
        self.assertEqual(state["trem"], 6000)
        self.assertEqual(state["ttop"], 5000000)
        self.assertEqual(state["spill"], 0)

    def test_overflow_spills_exactly_the_remainder_onto_purchased(self):
        s = FakeTopupState(trem=1000, ttop=5000000, tquota=10000000,
                           pend=4102444800, pnext=4105123200)
        state = s.deduct(3000, 0)
        self.assertEqual(state["trem"], 0)
        self.assertEqual(state["spill"], 2000)
        self.assertEqual(state["ttop"], 4998000)

    def test_overspend_beyond_both_buckets_floors_at_zero(self):
        s = FakeTopupState(trem=1000, ttop=500, tquota=10000000,
                           pend=4102444800, pnext=4105123200)
        state = s.deduct(90000, 0)
        self.assertEqual(state["trem"], 0)
        self.assertEqual(state["ttop"], 0)
        self.assertEqual(state["spill"], 500)

    def test_roll_restores_monthly_and_leaves_purchased_untouched(self):
        s = FakeTopupState(trem=0, ttop=4400000, tquota=10000000,
                           pend=1000, pnext=2000)
        state = s.peek(1500)
        self.assertEqual(state["rolled"], 1)
        self.assertEqual(state["trem"], 10000000)
        self.assertEqual(state["ttop"], 4400000)

    def test_deduction_after_roll_spends_fresh_quota_not_purchased(self):
        s = FakeTopupState(trem=0, ttop=4400000, tquota=10000000,
                           pend=1000, pnext=2000)
        state = s.deduct(4800, 1500)
        self.assertEqual(state["trem"], 10000000 - 4800)
        self.assertEqual(state["ttop"], 4400000)
        self.assertEqual(state["spill"], 0)


class TestLuaScriptText(unittest.TestCase):
    """Guards the nil-safety and no-touch-on-roll invariants at the source."""

    def test_both_scripts_default_missing_ttop_to_zero(self):
        from api.services.credits import creditService as cs
        for script in (cs._PEEK_LUA, cs._DEDUCT_LUA):
            self.assertIn("tonumber(m['ttop']) or 0", script)

    def test_roll_block_never_assigns_ttop(self):
        from api.services.credits import creditService as cs
        for script in (cs._PEEK_LUA, cs._DEDUCT_LUA):
            rollBlock = script.split("while now >= pend")[1].split("end")[0]
            self.assertNotIn("ttop", rollBlock)

    def test_deduct_script_persists_and_returns_ttop_and_spill(self):
        from api.services.credits import creditService as cs
        self.assertIn("'ttop', ttop", cs._DEDUCT_LUA)
        self.assertIn("return {trem, ttop, spill, rolled}", cs._DEDUCT_LUA)
        self.assertIn("return {trem, ttop, rolled, pend}", cs._PEEK_LUA)


class TestTopupBalanceReads(unittest.TestCase):
    def _service(self):
        from api.services.credits.creditService import CreditService
        svc = CreditService()
        svc.supabase = MagicMock()
        svc._manualBalance = MagicMock(return_value=None)
        return svc

    def test_remaining_tokens_is_the_sum_of_both_buckets(self):
        svc = self._service()
        with patch.object(svc, "_ensureHash", return_value=None), \
             patch.object(svc, "_peek", return_value={"trem": 2000, "ttop": 5000, "rolled": 0}):
            self.assertEqual(svc.getRemainingTokens("u1"), 7000)
            self.assertEqual(
                svc.getRemainingParts("u1"),
                {"monthly": 2000, "topup": 5000, "rolled": False},
            )

    def test_db_fallback_adds_topup_when_redis_is_down(self):
        svc = self._service()
        row = {"monthly_token_quota": 10000000, "remaining_tokens": 0,
               "topup_tokens": 4400000, "period_end": "2099-01-01T00:00:00+00:00"}
        with patch.object(svc, "_ensureHash", return_value=None), \
             patch.object(svc, "_peek", return_value=None), \
             patch.object(svc, "_dbRow", return_value=row):
            self.assertEqual(svc.getRemainingTokens("u1"), 4400000)

    def test_db_fallback_after_period_end_adds_topup_to_fresh_quota(self):
        svc = self._service()
        row = {"monthly_token_quota": 10000000, "remaining_tokens": 12,
               "topup_tokens": 4400000, "period_end": "2020-01-01T00:00:00+00:00"}
        with patch.object(svc, "_ensureHash", return_value=None), \
             patch.object(svc, "_peek", return_value=None), \
             patch.object(svc, "_dbRow", return_value=row):
            self.assertEqual(svc.getRemainingTokens("u1"), 14400000)

    def test_dbrow_selects_topup_tokens(self):
        svc = self._service()
        svc._dbRow("u1")
        selectArg = svc.supabase.table.return_value.select.call_args[0][0]
        self.assertIn("topup_tokens", selectArg)

    def test_paid_topup_lots_filter_status_and_malformed_snapshots(self):
        svc = self._service()
        query = svc.supabase.table.return_value
        query.select.return_value = query
        query.eq.return_value = query
        query.order.return_value = query
        query.execute.return_value.data = [
            {"status": "PAID", "pricing_reference_snapshot_json": {"tokens": 5_000_000}},
            {"status": "paid", "pricing_reference_snapshot_json": {"tokens": "1_000_000"}},
            {"status": "PAYMENT_PENDING", "pricing_reference_snapshot_json": {"tokens": 9_000_000}},
            {"status": "PAID", "pricing_reference_snapshot_json": {"tokens": "bad"}},
            {"status": "PAID", "pricing_reference_snapshot_json": None},
        ]

        self.assertEqual(svc._getPaidTopupLots("u1"), [5_000_000, 1_000_000])

    def test_paid_topup_lots_return_empty_when_invoice_read_fails(self):
        svc = self._service()
        query = svc.supabase.table.return_value
        query.select.return_value = query
        query.eq.return_value = query
        query.order.return_value = query
        query.execute.side_effect = RuntimeError("invoice history unavailable")

        self.assertEqual(svc._getPaidTopupLots("u1"), [])

    def test_spill_triggers_a_relative_rpc_decrement(self):
        svc = self._service()
        with patch.object(svc, "_ensureHash", return_value=None), \
             patch.object(svc, "_deduct",
                          return_value={"trem": 0, "ttop": 4998000, "spill": 2000, "rolled": 0}), \
             patch.object(svc, "_dbRow", return_value={"monthly_token_quota": 10000000}):
            remaining = svc.deductTokens("u1", tokensUsed=3000, operationType="reporting_query")
        self.assertEqual(remaining, 4998000)
        svc.supabase.rpc.assert_called_once_with(
            "decrement_topup_tokens", {"p_user_id": "u1", "p_tokens": 2000})

    def test_no_spill_makes_no_rpc_call(self):
        svc = self._service()
        with patch.object(svc, "_ensureHash", return_value=None), \
             patch.object(svc, "_deduct",
                          return_value={"trem": 9995200, "ttop": 0, "spill": 0, "rolled": 0}), \
             patch.object(svc, "_dbRow", return_value={"monthly_token_quota": 10000000}):
            svc.deductTokens("u1", tokensUsed=4800, operationType="reporting_query")
        svc.supabase.rpc.assert_not_called()

    def test_snapshot_breaks_out_both_buckets(self):
        svc = self._service()
        row = {"plan_tier": "pro", "monthly_token_quota": 10000000,
               "topup_tokens": 5000000, "period_start": None, "period_end": None,
               "last_reset_at": None}
        with patch.object(svc, "_dbRow", return_value=row), \
             patch.object(svc, "getRemainingParts",
                          return_value={"monthly": 2000000, "topup": 5000000}):
            snap = svc.getBalanceSnapshot("u1")
        self.assertEqual(snap["monthlyRemainingTokens"], 2000000)
        self.assertEqual(snap["topupTokens"], 5000000)
        self.assertEqual(snap["topupCredits"], 500.0)
        self.assertEqual(snap["remainingTokens"], 7000000)
        self.assertEqual(snap["usedTokens"], 8000000)
        self.assertEqual(snap["usagePercentage"], 80.0)

    def test_snapshot_reports_active_fifo_topup_total_and_used(self):
        svc = self._service()
        row = {"plan_tier": "pro", "monthly_token_quota": 10_000_000,
               "topup_tokens": 6_500_000, "period_start": None, "period_end": None,
               "last_reset_at": None}
        with patch.object(svc, "_dbRow", return_value=row), \
             patch.object(svc, "getRemainingParts",
                          return_value={"monthly": 2_000_000, "topup": 6_500_000}), \
             patch.object(svc, "_getPaidTopupLots",
                          return_value=[5_000_000, 5_000_000]):
            snap = svc.getBalanceSnapshot("u1")

        self.assertEqual(snap["topupTotalTokens"], 10_000_000)
        self.assertEqual(snap["topupUsedTokens"], 3_500_000)

    def test_snapshot_skips_invoice_history_when_topup_balance_is_zero(self):
        svc = self._service()
        row = {"plan_tier": "pro", "monthly_token_quota": 10_000_000,
               "topup_tokens": 0, "period_start": None, "period_end": None,
               "last_reset_at": None}
        with patch.object(svc, "_dbRow", return_value=row), \
             patch.object(svc, "getRemainingParts",
                          return_value={"monthly": 2_000_000, "topup": 0}), \
             patch.object(svc, "_getPaidTopupLots",
                          side_effect=AssertionError("must not read invoices")):
            snap = svc.getBalanceSnapshot("u1")

        self.assertEqual(snap["topupTotalTokens"], 0)
        self.assertEqual(snap["topupUsedTokens"], 0)

    def test_snapshot_uses_available_balance_when_invoice_history_is_missing(self):
        svc = self._service()
        row = {"plan_tier": "pro", "monthly_token_quota": 10_000_000,
               "topup_tokens": 6_500_000, "period_start": None, "period_end": None,
               "last_reset_at": None}
        with patch.object(svc, "_dbRow", return_value=row), \
             patch.object(svc, "getRemainingParts",
                          return_value={"monthly": 2_000_000, "topup": 6_500_000}), \
             patch.object(svc, "_getPaidTopupLots", return_value=[]):
            snap = svc.getBalanceSnapshot("u1")

        self.assertEqual(snap["topupTotalTokens"], 6_500_000)
        self.assertEqual(snap["topupUsedTokens"], 0)


class TestTopupLifecycleGuards(unittest.TestCase):
    def _service(self):
        from api.services.credits.creditService import CreditService
        svc = CreditService()
        svc.supabase = MagicMock()
        svc._manualBalance = MagicMock(return_value=None)
        return svc

    def test_initialize_never_writes_topup_tokens(self):
        svc = self._service()
        with patch.object(svc, "_dbRow", return_value={"topup_tokens": 5000000}), \
             patch.object(svc, "_redis", side_effect=Exception("no redis")):
            svc.initializeCreditBalance("u1", "pro")
        payload = svc.supabase.table.return_value.upsert.call_args[0][0]
        self.assertNotIn("topup_tokens", payload)

    def test_initialize_reseeds_ttop_from_the_existing_row(self):
        svc = self._service()
        fakeRedis = MagicMock()
        with patch.object(svc, "_dbRow", return_value={"topup_tokens": 5000000}), \
             patch.object(svc, "_redis", return_value=fakeRedis):
            svc.initializeCreditBalance("u1", "pro")
        mapping = fakeRedis.hset.call_args[1]["mapping"]
        self.assertEqual(mapping["ttop"], 5000000)

    def test_initialize_seeds_zero_for_a_brand_new_user(self):
        svc = self._service()
        fakeRedis = MagicMock()
        with patch.object(svc, "_dbRow", return_value=None), \
             patch.object(svc, "_redis", return_value=fakeRedis):
            svc.initializeCreditBalance("u1", "free")
        self.assertEqual(fakeRedis.hset.call_args[1]["mapping"]["ttop"], 0)

    def test_reconcile_never_writes_topup_tokens(self):
        svc = self._service()
        with patch.object(svc, "syncQuotaFromConfig"), \
             patch.object(svc, "_peek", return_value={"trem": 500, "ttop": 4000, "rolled": 0}), \
             patch.object(svc, "_dbRow", return_value={"monthly_token_quota": 10000000}):
            svc.reconcile("u1")
        rpcName, payload = svc.supabase.rpc.call_args[0]
        self.assertEqual(
            rpcName, "reconcile_credit_balance_if_no_admin_refresh"
        )
        self.assertNotIn("topup_tokens", payload)

    def test_force_reset_with_usage_reset_never_writes_topup_tokens(self):
        svc = self._service()
        svc.supabase.table.return_value.select.return_value.execute.return_value = \
            MagicMock(data=[{"user_id": "u1", "plan_tier": "pro"}])
        with patch.object(svc, "_redis", side_effect=Exception("no redis")):
            svc.forceResetAllQuotas(resetUsage=True)
        for call in svc.supabase.table.return_value.update.call_args_list:
            self.assertNotIn("topup_tokens", call[0][0])


class TestTopupGrantAndClawback(unittest.TestCase):
    def _service(self, rpcData):
        from api.services.credits.creditService import CreditService
        svc = CreditService()
        svc.supabase = MagicMock()
        svc._manualBalance = MagicMock(return_value=None)
        svc.supabase.rpc.return_value.execute.return_value = MagicMock(data=rpcData)
        return svc

    def test_grant_increments_redis_and_reports_tokens(self):
        svc = self._service([{"granted": True, "tokens": 5000000, "uid": "u1"}])
        fakeRedis = MagicMock()
        with patch.object(svc, "_redis", return_value=fakeRedis):
            result = svc.grantTopupTokens("u1", "order_abc", "pay_abc")
        self.assertEqual(result, {"granted": True, "tokens": 5000000})
        svc.supabase.rpc.assert_called_once_with(
            "grant_topup_tokens", {"p_order_id": "order_abc", "p_payment_id": "pay_abc"})
        fakeRedis.hincrby.assert_called_once_with("credits:v3:u1", "ttop", 5000000)

    def test_grant_is_a_noop_when_the_race_was_already_won(self):
        svc = self._service([{"granted": False, "tokens": 0, "uid": None}])
        fakeRedis = MagicMock()
        with patch.object(svc, "_redis", return_value=fakeRedis):
            result = svc.grantTopupTokens("u1", "order_abc", "pay_abc")
        self.assertEqual(result, {"granted": False, "tokens": 0})
        fakeRedis.hincrby.assert_not_called()

    def test_failed_redis_increment_drops_the_hash_for_rebuild(self):
        svc = self._service([{"granted": True, "tokens": 5000000, "uid": "u1"}])
        fakeRedis = MagicMock()
        fakeRedis.hincrby.side_effect = Exception("redis down")
        with patch.object(svc, "_redis", return_value=fakeRedis):
            result = svc.grantTopupTokens("u1", "order_abc", "pay_abc")
        self.assertEqual(result["granted"], True)
        fakeRedis.delete.assert_called_once_with("credits:v3:u1")

    def test_empty_rpc_response_is_treated_as_not_granted(self):
        svc = self._service([])
        with patch.object(svc, "_redis", return_value=MagicMock()):
            self.assertEqual(svc.grantTopupTokens("u1", "o", "p"),
                             {"granted": False, "tokens": 0})

    def test_clawback_decrements_redis_by_the_proportional_amount(self):
        svc = self._service([{"clawed": True, "tokens": 2000000}])
        fakeRedis = MagicMock()
        with patch.object(svc, "_redis", return_value=fakeRedis):
            result = svc.clawbackTopupTokens("u1", "rfnd_1", "pay_abc", 99950)
        self.assertEqual(result, {"clawed": True, "tokens": 2000000})
        svc.supabase.rpc.assert_called_once_with("clawback_topup_tokens", {
            "p_refund_id": "rfnd_1", "p_payment_id": "pay_abc",
            "p_refund_amount": 99950})
        fakeRedis.delete.assert_called_once_with("credits:v3:u1")

    def test_duplicate_refund_delivery_touches_nothing(self):
        svc = self._service([{"clawed": False, "tokens": 0}])
        fakeRedis = MagicMock()
        with patch.object(svc, "_redis", return_value=fakeRedis):
            result = svc.clawbackTopupTokens("u1", "rfnd_1", "pay_abc", 99950)
        self.assertEqual(result, {"clawed": False, "tokens": 0})
        fakeRedis.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
