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


class FakeState:
    """
    In-memory mirror of the v3 Redis hash used to fake _peek/_deduct.

    Carries the purchased bucket (`ttop`) so the double matches the Lua
    contract. These monthly-bucket tests all run with ttop=0; the top-up
    behaviour itself is covered in test_credit_topup_service.py.
    """

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


class TestCreditServiceMonthly(unittest.TestCase):
    def _service(self):
        from api.services.credits.creditService import CreditService
        svc = CreditService()
        svc.supabase = MagicMock()
        svc._manualBalance = MagicMock(return_value=None)
        return svc

    def test_deduct_subtracts_exact_tokens_without_rounding(self):
        svc = self._service()
        state = FakeState(trem=10000000, tquota=10000000, pend=4102444800, pnext=4105123200)
        with patch.object(svc, "_ensureHash", return_value=None), \
             patch.object(svc, "_deduct", side_effect=lambda uid, n: state.deduct(n, 0)), \
             patch.object(svc, "_dbRow", return_value={"monthly_token_quota": 10000000}):
            for _ in range(6):
                remaining = svc.deductTokens("u1", tokensUsed=800, operationType="reporting_query")
        # Six 800-token calls cost exactly 4800 tokens, not 6 rounded-up units.
        self.assertEqual(state.h["trem"], 10000000 - 4800)
        self.assertEqual(remaining, 10000000 - 4800)

    def test_deduct_floors_at_zero_on_overspend(self):
        svc = self._service()
        state = FakeState(trem=500, tquota=10000000, pend=4102444800, pnext=4105123200)
        with patch.object(svc, "_ensureHash", return_value=None), \
             patch.object(svc, "_deduct", side_effect=lambda uid, n: state.deduct(n, 0)), \
             patch.object(svc, "_dbRow", return_value={"monthly_token_quota": 10000000}):
            remaining = svc.deductTokens("u1", tokensUsed=90000, operationType="reporting_query")
        self.assertEqual(remaining, 0)

    def test_zero_token_call_is_not_charged(self):
        svc = self._service()
        state = FakeState(trem=10000000, tquota=10000000, pend=4102444800, pnext=4105123200)
        with patch.object(svc, "_ensureHash", return_value=None), \
             patch.object(svc, "_peek", side_effect=lambda uid: state.peek(0)), \
             patch.object(svc, "_deduct", side_effect=AssertionError("must not deduct")):
            self.assertEqual(svc.deductTokens("u1", tokensUsed=0, operationType="reporting_query"), 10000000)

    def test_deduct_returns_minus_one_when_redis_unreachable(self):
        svc = self._service()
        with patch.object(svc, "_ensureHash", return_value=None), \
             patch.object(svc, "_deduct", return_value=None):
            self.assertEqual(svc.deductTokens("u1", tokensUsed=800, operationType="reporting_query"), -1)

    def test_peek_rolls_instantly_past_period_end(self):
        svc = self._service()
        state = FakeState(trem=0, tquota=10000000, pend=1000, pnext=2000)
        with patch.object(svc, "_ensureHash", return_value=None), \
             patch.object(svc, "_peek", side_effect=lambda uid: state.peek(1500)), \
             patch.object(svc, "_repairPeriod") as repair:
            self.assertEqual(svc.getRemainingTokens("u1"), 10000000)
        self.assertEqual(state.h["pend"], 2000)
        self.assertEqual(state.h["pnext"], 2000 + _FALLBACK)
        repair.assert_called_once()

    def test_deduct_after_roll_applies_to_fresh_quota(self):
        svc = self._service()
        state = FakeState(trem=0, tquota=10000000, pend=1000, pnext=2000)
        with patch.object(svc, "_ensureHash", return_value=None), \
             patch.object(svc, "_deduct", side_effect=lambda uid, n: state.deduct(n, 1500)), \
             patch.object(svc, "_dbRow", return_value={"monthly_token_quota": 10000000}), \
             patch.object(svc, "_repairPeriod"):
            remaining = svc.deductTokens("u1", tokensUsed=4800, operationType="reporting_query")
        self.assertEqual(remaining, 10000000 - 4800)

    def test_second_request_in_same_period_does_not_roll_twice(self):
        svc = self._service()
        state = FakeState(trem=0, tquota=10000000, pend=1000, pnext=9999999999)
        with patch.object(svc, "_ensureHash", return_value=None), \
             patch.object(svc, "_peek", side_effect=lambda uid: state.peek(1500)), \
             patch.object(svc, "_repairPeriod") as repair:
            svc.getRemainingTokens("u1")
            svc.getRemainingTokens("u1")
        self.assertEqual(state.h["pend"], 9999999999)
        repair.assert_called_once()

    def test_roll_catches_up_across_several_missed_periods(self):
        svc = self._service()
        # pend far in the past; the fallback bound advances 31 days per iteration.
        state = FakeState(trem=0, tquota=10000000, pend=1000, pnext=2000)
        with patch.object(svc, "_ensureHash", return_value=None), \
             patch.object(svc, "_peek", side_effect=lambda uid: state.peek(2000 + _FALLBACK * 3)), \
             patch.object(svc, "_repairPeriod"):
            self.assertEqual(svc.getRemainingTokens("u1"), 10000000)
        self.assertGreater(state.h["pend"], 2000 + _FALLBACK * 3)

    def test_snapshot_exposes_tokens_and_derived_credits(self):
        svc = self._service()
        row = {
            "plan_tier": "pro", "monthly_token_quota": 10000000, "used_tokens": 4800,
            "remaining_tokens": 9995200, "period_start": "2026-07-01T00:00:00+00:00",
            "period_end": "2026-08-01T00:00:00+00:00", "last_reset_at": None,
        }
        with patch.object(svc, "_dbRow", return_value=row), \
             patch.object(svc, "getRemainingParts",
                          return_value={"monthly": 9995200, "topup": 0}):
            snap = svc.getBalanceSnapshot("u1")
        self.assertEqual(snap["monthlyTokenQuota"], 10000000)
        self.assertEqual(snap["usedTokens"], 4800)
        self.assertEqual(snap["remainingTokens"], 9995200)
        self.assertEqual(snap["monthlyCredits"], 1000.0)
        self.assertEqual(snap["usedCredits"], 0.48)
        self.assertEqual(snap["remainingCredits"], 999.52)
        self.assertEqual(snap["usagePercentage"], 0.05)
        self.assertTrue(snap["initialized"])
        self.assertNotIn("dailyQuota", snap)
        self.assertNotIn("effectiveRemaining", snap)

    def test_snapshot_defaults_when_no_row(self):
        svc = self._service()
        with patch.object(svc, "_dbRow", return_value=None):
            snap = svc.getBalanceSnapshot("u1")
        self.assertFalse(snap["initialized"])
        self.assertEqual(snap["remainingTokens"], 0)
        self.assertEqual(snap["remainingCredits"], 0.0)

    def test_db_fallback_returns_full_quota_when_period_ended(self):
        svc = self._service()
        row = {
            "monthly_token_quota": 10000000,
            "remaining_tokens": 12,
            "period_end": "2020-01-01T00:00:00+00:00",
        }
        with patch.object(svc, "_ensureHash", return_value=None), \
             patch.object(svc, "_peek", return_value=None), \
             patch.object(svc, "_dbRow", return_value=row):
            self.assertEqual(svc.getRemainingTokens("u1"), 10000000)

    def test_redis_key_is_v3(self):
        from api.services.credits.creditService import CreditService
        self.assertEqual(CreditService._redisKey("u1"), "credits:v3:u1")


if __name__ == "__main__":
    unittest.main()
