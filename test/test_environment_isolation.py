"""The test setup must not pass an application's configuration on to the tests."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFTEST = Path(__file__).with_name("conftest.py")
LEAK = "inherited-app-value"
REPLACED = {
    "REDIS_HOST": "127.0.0.1", "REDIS_PORT": "1", "REDIS_PASSWORD": "",
    "SECRET_KEY": "test-secret", "GEMINI_API_KEY": "test-key", "GROQ_API_KEY": "test-key",
    "PAYMENT_RULES_EFFECTIVE_AT": "2026-01-01T00:00:00+00:00",
}
REMOVED = (
    "GOOGLE_API_KEY", "OPENAI_API_KEY", "CEREBRAS_API_KEY", "ANOTHER_PROVIDER_API_KEY",
    "LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY", "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2",
    "SUPABASE_KEY_OLD", "USER_ERASURE_HMAC_SECRET", "PRICING_PROXY_SECRET",
    "SUBSCRIPTION_MODEL_PHASE", "NOTIFICATION_DISPATCH_ENABLED", "CREDIT_USAGE_SPOOL_DIR",
    "FILE_URL", "DOMAIN_FILE_URL", "PAYMENT_FAILED_EMAIL_URL", "MANUAL_CHECKOUT_TTL_SECONDS",
    "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND", "PGHOSTADDR", "PGHOST", "PGPASSWORD", "PGSERVICEFILE",
)


def _environmentAfterSetup(extra, code=""):
    script = f"import json, os, runpy, sys; runpy.run_path(sys.argv[1]); {code}print(json.dumps(dict(os.environ)))"
    result = subprocess.run([sys.executable, "-c", script, str(CONFTEST)], env={**os.environ, **extra},
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("name", REPLACED)
def test_inheritedSettingIsReplaced(name):
    assert _environmentAfterSetup({name: LEAK})[name] == REPLACED[name]


def test_inheritedAppSettingsAreRemoved():
    environment = _environmentAfterSetup({name: LEAK for name in REMOVED})

    assert [name for name in REMOVED if name in environment] == []


def test_dotenvFileCannotPutThemBack(tmp_path):
    path = tmp_path / ".env"
    path.write_text(f"DOTENV_ISOLATION_PROBE={LEAK}")
    code = f"from dotenv import load_dotenv; load_dotenv({str(path)!r}); "

    loaded = "DOTENV_ISOLATION_PROBE" in _environmentAfterSetup({}, code)
    assert not loaded  # asserting on the dict would print the environment when it fails


@pytest.mark.parametrize("token, shipped", [("", False), ("test-token", True)])
def test_logsShipOnlyWithAToken(tmp_path, token, shipped):
    # a test's child process does not run conftest.py, so the logger itself must not ship without a token
    child = "import sys, logtail; logtail.LogtailHandler = lambda *a, **k: sys.exit(3); import utils.logger"
    env = {**os.environ, "LOGTAIL_TOKEN": token,
           "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")]))}
    done = subprocess.run([sys.executable, "-c", child], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == (3 if shipped else 0), done.stderr[-2000:]
