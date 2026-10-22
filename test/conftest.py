import os

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
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("REDIS_PORT", "6379")
os.environ.setdefault("REDIS_PASSWORD", "")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("GROQ_API_KEY", "test-key")

# Automatic-capture rules cutover; unset blocks new Orders by design.
os.environ.setdefault("PAYMENT_RULES_EFFECTIVE_AT", "2026-01-01T00:00:00+00:00")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _freshPaymentWorkerHeartbeat(request):
    """The recovery/refund worker deploys with the backend and beats every minute; Pay requires it.

    Refreshed before every PostgreSQL test that uses the shared manual-billing database, so a slow
    run can never leave it stale. Tests that need a stale worker make it stale in their own body.
    """
    if os.environ.get('RUN_MANUAL_BILLING_INTEGRATION') != '1' or 'postgres' not in request.fixturenames:
        return
    import psycopg2
    url = request.getfixturevalue('postgres')
    with psycopg2.connect(url) as db, db.cursor() as cursor:
        cursor.execute('''DO $$ BEGIN IF to_regclass('private.payment_recovery_runtime') IS NOT NULL THEN
            INSERT INTO private.payment_recovery_runtime(singleton,worker_seen_at) VALUES(true,clock_timestamp())
            ON CONFLICT(singleton) DO UPDATE SET worker_seen_at=EXCLUDED.worker_seen_at; END IF; END $$''')
    db.close()
