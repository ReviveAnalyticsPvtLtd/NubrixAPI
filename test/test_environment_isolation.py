"""The test setup must not pass an application's configuration on to the tests."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

CONFTEST = Path(__file__).with_name("conftest.py")
LEAK = "inherited-app-value"
REPLACED = {
    "REDIS_HOST": "127.0.0.1", "REDIS_PORT": "1", "REDIS_PASSWORD": "",
    "SECRET_KEY": "test-secret", "GEMINI_API_KEY": "test-key", "GROQ_API_KEY": "test-key",
}
REMOVED = (
    "GOOGLE_API_KEY", "OPENAI_API_KEY", "CEREBRAS_API_KEY", "ANOTHER_PROVIDER_API_KEY",
    "LANGFUSE_SECRET_KEY", "LANGFUSE_PUBLIC_KEY", "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2",
    "SUPABASE_KEY_OLD", "USER_ERASURE_HMAC_SECRET", "PRICING_PROXY_SECRET",
    "SUBSCRIPTION_MODEL_PHASE", "NOTIFICATION_DISPATCH_ENABLED", "CREDIT_USAGE_SPOOL_DIR",
    "FILE_URL", "DOMAIN_FILE_URL", "PAYMENT_FAILED_EMAIL_URL",
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


def test_logsAreNotShipped():
    code = "from logtail import LogtailHandler; os.environ['LOG_SINK'] = type(LogtailHandler(source_token='')).__name__; "

    assert _environmentAfterSetup({}, code)["LOG_SINK"] == "NullHandler"
