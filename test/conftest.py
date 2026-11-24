import os

import dotenv

# Tests take no application settings from the shell or from .env: scripts imported by tests must not reload .env.
# Child processes do not run this file, so they get this cleaned environment and must not read .env either.
dotenv.load_dotenv = lambda *args, **kwargs: False
_APP_SETTINGS = {
    "BILLING_ADMIN_USER_IDS", "BREVO_API_BASE_URL", "BREVO_SENDER_EMAIL", "BREVO_SENDER_NAME",
    "CREDIT_USAGE_SPOOL_DIR", "DASHBOARD_BASE_URL", "DOMAIN_FILE_URL", "FILE_URL",
    "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_GENAI_USE_VERTEXAI", "LOGTAIL_HOST",
    "NOTIFICATION_DISPATCH_ENABLED", "NOTIFICATION_RECONCILIATION_ENABLED", "PRICING_PROXY_SECRET",
    "SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED", "SUBSCRIPTION_MODEL_PHASE", "SUPABASE_ANON_KEY",
    "SUPABASE_KEY_OLD", "TRIAL_EXPIRY_EMAIL_MODE", "USER_ERASURE_HMAC_SECRET", "WEB_WORKERS",
}
for _key in list(os.environ):
    if (_key in _APP_SETTINGS or _key.endswith(("_API_KEY", "_EMAIL_URL"))
            or _key.startswith(("LANGFUSE_", "LANGSMITH_", "LANGCHAIN_"))
            # Celery reads its broker URL, and libpq its PG* settings, over the dummies below
            or _key.startswith(("CELERY_", "PG")) or _key == "MANUAL_CHECKOUT_TTL_SECONDS"):
        del os.environ[_key]

# Unit tests must never inherit the application's configured database URL.
# Real integration fixtures use MANUAL_BILLING_TEST_DATABASE_URL explicitly.
os.environ["DATABASE_URL"] = "postgresql://test:test@127.0.0.1:1/blocked_unit_database"
os.environ["LOGTAIL_TOKEN"] = ""
for _key in ('RAZORPAY_KEY_ID','RAZORPAY_KEY_SECRET','RAZORPAY_WEBHOOK_SECRET','BREVO_API_KEY'):
    os.environ[_key] = 'test-only'

# Enough dummy config for modules that read env at import time. Individual
# tests still mock Redis, Supabase, and payment providers themselves.
os.environ["SUPABASE_URL"] = "http://localhost:1"
os.environ["SUPABASE_KEY"] = "test-key"
os.environ["SECRET_KEY"] = "test-secret"
# Nothing listens on port 1. Redis integration tests use MANUAL_BILLING_TEST_REDIS_URL.
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "1"
os.environ["REDIS_PASSWORD"] = ""
os.environ["GEMINI_API_KEY"] = "test-key"
os.environ["GROQ_API_KEY"] = "test-key"

# Sandbox tests run as the developer's own uid outside the image, which sets SANDBOX_RUN_ROOT.
os.environ.setdefault("SANDBOX_ALLOW_SAME_UID", "1")

# Automatic-capture rules cutover; unset blocks new Orders by design.
os.environ["PAYMENT_RULES_EFFECTIVE_AT"] = "2026-01-01T00:00:00+00:00"


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _freshProductAccessCache():
    """The plan check remembers answers per process; no test may inherit another's."""
    import sys
    commons = sys.modules.get("api.commons")
    if commons is not None and hasattr(commons, "productAccessCache"):
        commons.productAccessCache.clear()
    yield


@pytest.fixture(autouse=True)
def _freshPaymentWorkerHeartbeat(request):
    """The recovery/refund worker deploys with the backend and beats every minute; Pay requires it.

    Refreshed before every PostgreSQL test that uses the shared manual-billing or live-schema
    database, so a slow run can never leave it stale. Every purpose's Pay requires it. Tests that
    need a stale worker make it stale in their own body.
    """
    if os.environ.get('RUN_MANUAL_BILLING_INTEGRATION') != '1':
        return
    import psycopg2
    for fixture in ('postgres', 'actualSchema'):
        if fixture not in request.fixturenames:
            continue
        url = request.getfixturevalue(fixture)
        with psycopg2.connect(url) as db, db.cursor() as cursor:
            cursor.execute('''DO $$ BEGIN IF to_regclass('private.payment_recovery_runtime') IS NOT NULL THEN
                INSERT INTO private.payment_recovery_runtime(singleton,worker_seen_at) VALUES(true,clock_timestamp())
                ON CONFLICT(singleton) DO UPDATE SET worker_seen_at=EXCLUDED.worker_seen_at; END IF; END $$''')
        db.close()
