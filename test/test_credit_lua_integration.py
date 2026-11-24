import os
import unittest
from urllib.parse import urlparse

# Opt-in only: set RUN_REDIS_INTEGRATION=1 and MANUAL_BILLING_TEST_REDIS_URL (a disposable
# loopback Redis, never the app's) to exercise the real Lua scripts. Kept off by
# default so CI (which stubs the redis module) stays green.
RUN_INTEGRATION = os.environ.get("RUN_REDIS_INTEGRATION") == "1"

_FALLBACK = 2678400


@unittest.skipUnless(RUN_INTEGRATION, "Set RUN_REDIS_INTEGRATION=1 and MANUAL_BILLING_TEST_REDIS_URL (a disposable loopback Redis) to run")
class TestCreditLuaIntegration(unittest.TestCase):
    def setUp(self):
        import redis
        from api.services.credits.creditService import CreditService

        url = os.environ.get("MANUAL_BILLING_TEST_REDIS_URL", "")
        parsed = urlparse(url)
        assert parsed.scheme == "redis" and parsed.hostname in ("127.0.0.1", "localhost")
        assert parsed.port not in (None, 6379), "Explicit disposable random Redis port required"
        client = redis.Redis.from_url(url, decode_responses=True)
        self.addCleanup(client.close)
        self.svc = CreditService()
        self.svc._redis = lambda: client
        self.key = self.svc._redisKey("lua_test_user")
        r = self.svc._redis()
        r.delete(self.key)
        r.hset(self.key, mapping={
            "trem": 10000000, "tquota": 10000000,
            "pend": 4102444800, "pnext": 4105123200,
        })

    def tearDown(self):
        self.svc._redis().delete(self.key)

    def test_deduct_subtracts_exact_tokens(self):
        state = self.svc._deduct("lua_test_user", 4800)
        self.assertEqual(state["trem"], 9995200)
        self.assertEqual(state["rolled"], 0)
        self.assertEqual(self.svc._peek("lua_test_user")["trem"], 9995200)

    def test_peek_rolls_when_period_ended(self):
        r = self.svc._redis()
        r.hset(self.key, mapping={"trem": 0, "pend": 1000, "pnext": 4102444800})
        state = self.svc._peek("lua_test_user")
        self.assertEqual(state["rolled"], 1)
        self.assertEqual(state["trem"], 10000000)
        self.assertEqual(int(r.hget(self.key, "pend")), 4102444800)
        self.assertEqual(int(r.hget(self.key, "pnext")), 4102444800 + _FALLBACK)

    def test_second_peek_in_same_period_does_not_roll(self):
        r = self.svc._redis()
        r.hset(self.key, mapping={"trem": 0, "pend": 1000, "pnext": 4102444800})
        self.assertEqual(self.svc._peek("lua_test_user")["rolled"], 1)
        self.assertEqual(self.svc._peek("lua_test_user")["rolled"], 0)

    def test_deduct_rolls_then_charges_fresh_quota(self):
        r = self.svc._redis()
        r.hset(self.key, mapping={"trem": 0, "pend": 1000, "pnext": 4102444800})
        state = self.svc._deduct("lua_test_user", 4800)
        self.assertEqual(state["rolled"], 1)
        self.assertEqual(state["trem"], 9995200)

    def test_missing_hash_returns_none(self):
        self.svc._redis().delete(self.key)
        self.assertIsNone(self.svc._peek("lua_test_user"))
        self.assertIsNone(self.svc._deduct("lua_test_user", 100))

    def test_legacy_hash_without_ttop_is_read_as_zero(self):
        state = self.svc._peek("lua_test_user")
        self.assertEqual(state["ttop"], 0)
        state = self.svc._deduct("lua_test_user", 100)
        self.assertEqual(state["ttop"], 0)
        self.assertEqual(state["spill"], 0)

    def test_deduct_spills_onto_topup_when_monthly_exhausted(self):
        r = self.svc._redis()
        r.hset(self.key, mapping={"trem": 1000, "ttop": 5000000})
        state = self.svc._deduct("lua_test_user", 3000)
        self.assertEqual(state["trem"], 0)
        self.assertEqual(state["spill"], 2000)
        self.assertEqual(state["ttop"], 4998000)
        self.assertEqual(int(r.hget(self.key, "ttop")), 4998000)

    def test_roll_leaves_topup_untouched(self):
        r = self.svc._redis()
        r.hset(self.key, mapping={"trem": 0, "ttop": 4400000,
                                  "pend": 1000, "pnext": 4102444800})
        state = self.svc._peek("lua_test_user")
        self.assertEqual(state["rolled"], 1)
        self.assertEqual(state["trem"], 10000000)
        self.assertEqual(state["ttop"], 4400000)
        self.assertEqual(int(r.hget(self.key, "ttop")), 4400000)


if __name__ == "__main__":
    unittest.main()
