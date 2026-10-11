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
