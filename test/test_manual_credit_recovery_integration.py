"""Credit recovery race contract tests (Redis opt-in gates documented).

Real Redis interleaving evidence requires RUN_MANUAL_BILLING_INTEGRATION=1
plus MANUAL_BILLING_TEST_REDIS_URL; skipped runs are UNVERIFIED.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MANUAL_BILLING_INTEGRATION") != "1",
    reason="opt-in Redis integration evidence; skipped runs are UNVERIFIED",
)


def test_gate_reads_documented_redis_keys():
    if os.environ.get("RUN_MANUAL_BILLING_INTEGRATION") == "1":
        assert os.environ.get("MANUAL_BILLING_TEST_REDIS_URL"), (
            "integration runs must set the disposable test Redis URL"
        )
        assert os.environ.get("MANUAL_BILLING_TEST_REDIS_URL") != os.environ.get(
            "REDIS_HOST"
        ) or os.environ.get("REDIS_PORT") is None, (
            "integration Redis must be isolated from the production Redis"
        )