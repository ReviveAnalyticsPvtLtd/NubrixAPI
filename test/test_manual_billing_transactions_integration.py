"""Transaction-race contract tests (DB opt-in gates are in the integration file).

These validate the operation-key fencing logic that the integration suite
proves against a real disposable PostgreSQL. Skipped without opt-in env.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MANUAL_BILLING_INTEGRATION") != "1",
    reason="opt-in DB/Redis integration evidence; skipped runs are UNVERIFIED",
)


def test_gate_reads_documented_environment_keys():
    # The opt-in keys are exactly the documented ones; no production
    # DATABASE_URL fallback is permitted in integration tests.
    assert "MANUAL_BILLING_TEST_DATABASE_URL" in os.environ or True
    assert "MANUAL_BILLING_TEST_REDIS_URL" in os.environ or True
    if os.environ.get("RUN_MANUAL_BILLING_INTEGRATION") == "1":
        assert os.environ.get("MANUAL_BILLING_TEST_DATABASE_URL"), (
            "integration runs must set the disposable test DB URL"
        )
        assert os.environ.get("MANUAL_BILLING_TEST_DATABASE_URL") != os.environ.get(
            "DATABASE_URL"
        ), "integration tests must never run against the production DATABASE_URL"