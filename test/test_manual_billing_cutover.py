"""Cutover test fixtures: guarded contraction, inventory, backfill dry-run."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.manual_billing_inventory import (  # noqa: E402
    buildInventoryQueries,
    redactValue,
)
from scripts.manual_billing_backfill import (  # noqa: E402
    buildCanonicalBackfillMapping,
    dryRunReport,
)


def _contractSql() -> str:
    matches = list(
        Path("supabase/manual_billing_contract").glob("*_contract_recurring_billing_fields.sql")
    )
    assert len(matches) == 1
    return matches[0].read_text(encoding="utf-8")


# --- contract migration gating ------------------------------------------------


def test_contract_drops_exactly_the_four_columns():
    sql = _contractSql().lower()
    for column in (
        "razorpay_customer_id",
        "razorpay_token_id",
        "subscription_anchor_day",
        "recurring_failures",
    ):
        assert f"drop column {column}" in sql or (
            f"drop column if exists {column}" in sql
        ), f"contract migration must drop {column}"


def test_contract_migration_checks_preconditions_first():
    sql = _contractSql()
    # Precondition queries must run BEFORE the drops with an actionable
    # exception on unsafe state.
    lowered = sql.lower()
    preconditionPos = lowered.find("do $$")
    dropPos = lowered.find("drop column")
    assert preconditionPos != -1 and dropPos != -1
    assert preconditionPos < dropPos
    assert "raise exception" in lowered


def test_contract_rejects_active_token_mandates():
    sql = _contractSql().lower()
    assert "razorpay_token_id is not null" in sql


# --- inventory script -----------------------------------------------------------


def test_inventory_is_read_only_and_redacted():
    queries = buildInventoryQueries()
    joined = "\n".join(queries.values()).lower()
    assert "select" in joined
    assert "insert" not in joined
    assert "update" not in joined
    assert "delete" not in joined
    assert "drop" not in joined


def test_inventory_covers_required_dimensions():
    queries = buildInventoryQueries()
    joined = " ".join(queries.keys()).lower()
    for dimension in ("subscriptions", "invoices", "credits", "webhook", "canonical"):
        assert dimension in joined


def test_inventory_runs_before_expansion(monkeypatch, capsys):
    """The pre-mutation inventory must work on a pre-expansion schema (no credit identity columns)."""
    import json
    import types
    from scripts.manual_billing_inventory import main

    preExpansion = {'subscriptions': ['id', 'user_id', 'razorpay_token_id'],
                    'credit_balances': ['id', 'user_id', 'period_start']}

    class Cursor:
        def __init__(self):
            self.rows = []
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def execute(self, sql, params=None):
            if 'information_schema.columns' in sql and 'table_name=' in sql.replace(' ', ''):
                table = 'credit_balances' if 'credit_balances' in sql else 'subscriptions'
                self.rows = [{'column_name': column} for column in preExpansion[table]]
            elif 'lifecycle_id' in sql or 'credit_period_id' in sql:
                raise RuntimeError('column "lifecycle_id" does not exist')
            elif 'from public.credit_balances' in sql:
                self.rows = [{'balance_rows': 6}]
            else:
                self.rows = []
        def fetchall(self):
            return self.rows

    class Connection:
        def set_session(self, **kwargs):
            pass
        def cursor(self, **kwargs):
            return Cursor()
        def close(self):
            pass

    monkeypatch.setenv('DATABASE_URL', 'postgresql://inventory@localhost/db')
    monkeypatch.setitem(sys.modules, 'psycopg2', types.SimpleNamespace(connect=lambda *a, **k: Connection()))
    monkeypatch.setitem(sys.modules, 'psycopg2.extras', types.SimpleNamespace(RealDictCursor=object))
    assert main(['--json']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['credits_identity_coverage'] == [{'balance_rows': 6, 'identityColumnsAbsent': True}]


def test_redact_masks_values():
    assert redactValue("cust_123456789") == "cust_***"
    assert redactValue(None) is None
    assert redactValue(42) == "***"
    assert redactValue("ab") == "***"  # short values fully masked


# --- backfill script --------------------------------------------------------------


@pytest.fixture
def backfill_clock(monkeypatch):
    class FixtureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    monkeypatch.setattr('scripts.manual_billing_backfill.datetime', FixtureClock)


def test_backfill_dry_run_reports_candidates_conflicts_and_fields(backfill_clock):
    subscriptionRows = [
        {
            "id": "sub-a",
            "user_id": "user-1",
            "status": "active",
            "billing_mode": "monthly_recurring",
            "current_period_start": "2026-09-20T10:00:00+00:00",
            "current_period_end": "2026-10-20T10:00:00+00:00",
        },
        {
            "id": "sub-b",
            "user_id": "user-2",
            "status": "expired",
            "billing_mode": "none",
            "current_period_start": None,
            "current_period_end": None,
        },
    ]
    invoiceRows = [
        {
            "id": "inv-1",
            "userId": "user-1",
            "subscription_id": "sub-a",
            "billing_reason": "initial_purchase",
            "status": "PAID",
        },
    ]
    report = dryRunReport(subscriptionRows, invoiceRows)
    assert report["candidates"][0]["user_id"] == "user-1"
    assert report["candidates"][0]["promote_id"] == "sub-a"
    assert report["candidates"][0]["reason"] == "active_paid_or_trial"
    assert report["conflicts"] == []
    oldFields = report["candidates"][0]["old_fields"]
    assert oldFields["billing_mode"] == "monthly_recurring"
    assert report["candidates"][0]["new_fields"]["billing_mode"] == "monthly_prepaid"


def test_backfill_conflicting_paid_rows_are_reported_not_guessed(backfill_clock):
    subscriptionRows = [
        {
            "id": "sub-x",
            "user_id": "user-1",
            "status": "active",
            "billing_mode": "monthly_recurring",
            "current_period_start": "2026-09-20T10:00:00+00:00",
            "current_period_end": "2026-10-20T10:00:00+00:00",
        },
        {
            "id": "sub-y",
            "user_id": "user-1",
            "status": "active",
            "billing_mode": "annual_prepaid",
            "current_period_start": "2026-09-20T10:00:00+00:00",
            "current_period_end": "2027-09-20T10:00:00+00:00",
        },
    ]
    report = dryRunReport(subscriptionRows, [])
    assert len(report["conflicts"]) == 1
    assert report["conflicts"][0]["user_id"] == "user-1"
    assert "sub-x" in report["conflicts"][0]["candidate_ids"]
    assert "sub-y" in report["conflicts"][0]["candidate_ids"]


def test_backfill_mapping_prefers_verified_coverage_over_timestamps():
    rows = [
        {
            "id": "sub-old",
            "user_id": "user-1",
            "status": "active",
            "billing_mode": "monthly_recurring",
            "current_period_start": "2026-09-20T10:00:00+00:00",
            "current_period_end": "2026-10-20T10:00:00+00:00",
            "updated_at": "2026-10-19T00:00:00+00:00",
        },
        {
            "id": "sub-history",
            "user_id": "user-1",
            "status": "expired",
            "billing_mode": "none",
            "current_period_start": None,
            "current_period_end": None,
            "updated_at": "2026-10-21T00:00:00+00:00",
        },
    ]
    invoices = [
        {
            "id": "inv-1",
            "userId": "user-1",
            "subscription_id": "sub-old",
            "billing_reason": "initial_purchase",
            "status": "PAID",
        },
    ]
    mapping = buildCanonicalBackfillMapping(rows, invoices)
    # The verified-coverage row wins even though the historical row was
    # updated later.
    assert mapping["user-1"] == "sub-old"


def test_cancelled_rows_backfill_opt_out():
    rows = [
        {
            "id": "sub-c",
            "user_id": "user-3",
            "status": "cancelled",
            "billing_mode": "monthly_recurring",
            "current_period_start": "2026-09-20T10:00:00+00:00",
            "current_period_end": "2026-10-20T10:00:00+00:00",
            "cancellation_reason": "done",
        },
    ]
    report = dryRunReport(rows, [])
    candidate = report["candidates"][0]
    assert candidate["user_id"] == "user-3"
    assert candidate["new_fields"]["renewal_opt_out"] is True
    assert candidate["new_fields"]["billing_mode"] == "monthly_prepaid"
