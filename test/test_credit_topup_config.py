import os
import sys
import types
import unittest

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


class TestTopupPackConfig(unittest.TestCase):
    def test_config_version_is_4_0_1(self):
        from api.services.credits.creditConfig import CREDIT_CONFIG
        self.assertEqual(CREDIT_CONFIG["version"], "4.0.1")

    def test_returns_the_three_active_packs(self):
        from api.services.credits.creditConfig import getTopupPacks
        packs = getTopupPacks()
        self.assertEqual(sorted(packs.keys()), ["large", "medium", "small"])

    def test_pack_carries_tokens_and_paise_amount(self):
        from api.services.credits.creditConfig import getTopupPack
        pack = getTopupPack("medium")
        self.assertEqual(pack["tokens"], 5000000)
        self.assertEqual(pack["amount"], 200000)

    def test_unknown_pack_returns_none(self):
        from api.services.credits.creditConfig import getTopupPack
        self.assertIsNone(getTopupPack("enormous"))

    def test_inactive_pack_is_hidden_and_unresolvable(self):
        from api.services.credits import creditConfig
        original = creditConfig._cachedCreditConfig
        try:
            creditConfig._cachedCreditConfig = {
                "version": "4.0.0", "token_to_credit_ratio": 10000,
                "quotas": {}, "operation_minimums": {},
                "topup_packs": {
                    "legacy": {"tokens": 1, "amount": 1, "active": False},
                    "live":   {"tokens": 2, "amount": 2, "active": True},
                },
            }
            self.assertEqual(list(creditConfig.getTopupPacks().keys()), ["live"])
            self.assertIsNone(creditConfig.getTopupPack("legacy"))
        finally:
            creditConfig._cachedCreditConfig = original

    def test_missing_topup_packs_key_is_not_fatal(self):
        from api.services.credits import creditConfig
        original = creditConfig._cachedCreditConfig
        try:
            creditConfig._cachedCreditConfig = {
                "version": "3.1.0", "token_to_credit_ratio": 10000,
                "quotas": {}, "operation_minimums": {},
            }
            self.assertEqual(creditConfig.getTopupPacks(), {})
            self.assertIsNone(creditConfig.getTopupPack("small"))
        finally:
            creditConfig._cachedCreditConfig = original


if __name__ == "__main__":
    unittest.main()
