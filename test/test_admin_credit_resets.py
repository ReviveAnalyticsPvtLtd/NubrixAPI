"""Administrator credit resets through the real repository transaction.

SQLite executes the production queries for deterministic coverage. PostgreSQL
rollback, trigger and two-session locking proof lives in
test_admin_credit_reset_postgres.py.
"""
import json
import sqlite3
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest

from api.services.adminAuthService import AdminContext
from test.test_manual_billing_runtime import (
    LIFE,
    NOW,
    SUB,
    USER,
    database,
    read_row,
    seed_payment,
    sqlTransaction,
)


ADMIN = AdminContext(
    adminId="5e0c9f4e-3d8c-4f51-9b0a-2a7d9a1d6c11",
    email="ops@example.com",
    name="Ops",
    sessionId="0f1e2d3c-4b5a-4968-8776-655443322110",
    token="admin-session-token-never-stored",
)
OTHER_ADMIN = AdminContext(
    adminId="7a6b5c4d-3e2f-4a1b-8c9d-0e1f2a3b4c5d",
    email="other@example.com",
    name="Other",
    sessionId="1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5e",
    token="other-admin-token",
)
REASON = "Support-approved allowance reset"

ADMIN_TABLES = '''
  CREATE TABLE admin_audit_log (
    id TEXT PRIMARY KEY, admin_id TEXT, admin_email TEXT NOT NULL, session_id TEXT,
    actor_type TEXT NOT NULL, action TEXT NOT NULL, target_type TEXT NOT NULL,
    target_id TEXT, changed_fields TEXT NOT NULL DEFAULT '[]',
    details TEXT NOT NULL DEFAULT '{}', outcome TEXT NOT NULL, created_at TEXT
  );
  CREATE TABLE admin_credit_reset_operations (
    id TEXT PRIMARY KEY, scope TEXT NOT NULL, target_user_id TEXT,
    admin_id TEXT NOT NULL, admin_email TEXT NOT NULL, session_id TEXT NOT NULL,
    reason TEXT NOT NULL, idempotency_key_hash TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL, created_at TEXT,
    UNIQUE (admin_id, idempotency_key_hash)
  );
  CREATE TABLE admin_credit_reset_targets (
    operation_id TEXT NOT NULL, user_id TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'PENDING', reason_code TEXT,
    before_snapshot TEXT, after_snapshot TEXT, audit_id TEXT, reset_at TEXT,
    cache_state TEXT NOT NULL DEFAULT 'NOT_APPLICABLE', updated_at TEXT,
    PRIMARY KEY (operation_id, user_id)
  );
'''


def rows(path, query, params=()):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, params).fetchall()]
    finally:
        connection.close()


@pytest.fixture
def resets(database):
    repository, path = database
    with sqlTransaction(path) as connection:
        connection.executescript(ADMIN_TABLES)
    from api.services.adminCreditResetRepository import AdminCreditResetRepository
    return AdminCreditResetRepository(repository), repository, path


def activateMonthly(resets, used=7000, remaining=3000, topups=2500, domains=None):
    resetRepository, repository, path = resets
    repository.finalizeCapturedPayment(seed_payment((repository, path), domains=domains))
    with sqlTransaction(path) as connection:
        connection.execute(
            "UPDATE credit_balances SET monthly_token_quota=10000,used_tokens=?,"
            "remaining_tokens=?,topup_tokens=?",
            (used, remaining, topups),
        )
    return read_row(path, "credit_balances")


def quota(plan, count):
    return {"pro": 10000, "annual": 30000, "free": 5000}[plan] * (count if plan != "free" else 1)


def reset(resets, userId=USER, key="key-1", reason=REASON, admin=ADMIN, scope="individual"):
    resetRepository = resets[0]
    with patch("api.services.credits.creditConfig.getTokenQuotaForPlan", side_effect=quota):
        operation = resetRepository.createOrGetOperation(
            scope, userId if scope == "individual" else None, reason, key, admin
        )
        target = resetRepository.resetTarget(str(operation["id"]), userId)
    return operation, target


def audits(path):
    return rows(path, "SELECT * FROM admin_audit_log ORDER BY created_at, id")


def test_reset_refreshes_current_allowance_and_preserves_topups(resets):
    _, _, path = resets
    before = activateMonthly(resets)
    operation, target = reset(resets)
    after = read_row(path, "credit_balances")
    assert after["used_tokens"] == 0 and after["remaining_tokens"] == 10000
    assert after["monthly_token_quota"] == 10000
    assert after["topup_tokens"] == 2500
    assert after["credit_period_id"] != before["credit_period_id"]
    assert after["balance_version"] == before["balance_version"] + 1
    assert after["last_reset_at"] == NOW.isoformat()
    assert (after["period_start"], after["period_end"]) == (before["period_start"], before["period_end"])
    assert target["outcome"] == "RESET" and target["cache_state"] == "PENDING"
    stored = json.loads(target["before_snapshot"]) if isinstance(target["before_snapshot"], str) else target["before_snapshot"]
    assert stored["usedTokens"] == 7000 and stored["topupTokens"] == 2500
    allocation = rows(path, "SELECT metadata_json FROM billing_events WHERE idempotency_key=?",
                      ("credit-allocation:" + USER + ":" + after["credit_period_id"],))
    assert json.loads(allocation[0]["metadata_json"])["quotaWatermark"] == 10000


def test_reset_writes_exactly_one_strict_audit_with_saved_actor(resets):
    _, _, path = resets
    activateMonthly(resets)
    operation, target = reset(resets)
    [audit] = audits(path)
    assert audit["id"] == target["audit_id"]
    assert (audit["action"], audit["target_type"], audit["target_id"], audit["outcome"]) == (
        "credits.reset", "user", USER, "RESET")
    assert (audit["admin_id"], audit["session_id"], audit["admin_email"], audit["actor_type"]) == (
        ADMIN.adminId, ADMIN.sessionId, ADMIN.email, "admin")
    details = json.loads(audit["details"])
    assert details["operationId"] == str(operation["id"]) and details["reason"] == REASON
    assert details["before"]["usedTokens"] == 7000 and details["after"]["remainingTokens"] == 10000
    assert details["topupTokensPreserved"] == 2500
    assert ADMIN.token not in audit["details"]
    assert set(json.loads(audit["changed_fields"])) >= {"used_tokens", "remaining_tokens", "credit_period_id"}


def test_reset_quota_uses_current_domains_not_pending_removals(resets):
    _, _, path = resets
    activateMonthly(resets, domains=["banking", "telecom"])
    with sqlTransaction(path) as connection:
        connection.execute("UPDATE subscriptions SET domain_count=2,pending_removals='[\"telecom\"]'")
    reset(resets)
    assert read_row(path, "credit_balances")["monthly_token_quota"] == 20000


def test_reset_keeps_paid_dates_and_future_invoice(resets):
    resetRepository, repository, path = resets
    activateMonthly(resets)
    future = {"manualBilling": {"lifecycleId": LIFE, "purpose": "renewal", "billingMode": "monthly_prepaid",
              "domains": ["banking"], "coverageState": "scheduled", "creditPeriodId": str(uuid.uuid4())}}
    subscription = read_row(path, "subscriptions")
    with sqlTransaction(path) as connection:
        connection.execute('INSERT INTO "Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,'
            'currency,period_start,period_end,metadata_json,"razorpayPaymentId") VALUES(?,?,?,\'PAID\',\'renewal\',3000,'
            '\'INR\',?,?,?,\'future-pay\')',
            ("future-invoice", USER, SUB, subscription["current_period_end"], "2026-12-06T12:00:00+00:00", json.dumps(future)))
    invoicesBefore = rows(path, 'SELECT * FROM "Invoices" ORDER BY id')
    reset(resets)
    after = read_row(path, "subscriptions")
    assert (after["current_period_start"], after["current_period_end"], after["status"]) == (
        subscription["current_period_start"], subscription["current_period_end"], subscription["status"])
    assert rows(path, 'SELECT * FROM "Invoices" ORDER BY id') == invoicesBefore


def test_old_admission_cannot_drain_reset_allocation(resets):
    _, repository, path = resets
    activateMonthly(resets)
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    credits = ManualCreditRepository(repository)
    old = credits.admit(USER, "reporting_query", "before-reset")
    reset(resets)
    settled = credits.settle(old, 400, "old-run")
    after = read_row(path, "credit_balances")
    assert settled["historicalPeriod"] is True
    assert after["remaining_tokens"] == 10000 and after["used_tokens"] == 0


def test_next_read_and_admission_keep_reset_allocation(resets):
    _, repository, path = resets
    activateMonthly(resets)
    _, target = reset(resets)
    resetBalance = read_row(path, "credit_balances")
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    credits = ManualCreditRepository(repository)
    snapshot = credits.balanceSnapshot(USER)
    context = credits.admit(USER, "reporting_query", "after-reset")
    after = read_row(path, "credit_balances")
    assert snapshot["remaining_tokens"] == 10000
    assert context.creditPeriodId == resetBalance["credit_period_id"] and context.quotaWatermark == 10000
    assert after["balance_version"] == resetBalance["balance_version"]


def test_same_key_replays_without_second_reset_or_audit(resets):
    resetRepository, _, path = resets
    activateMonthly(resets)
    first, target = reset(resets)
    version = read_row(path, "credit_balances")["balance_version"]
    again, replayed = reset(resets)
    assert str(again["id"]) == str(first["id"])
    assert replayed["audit_id"] == target["audit_id"]
    assert read_row(path, "credit_balances")["balance_version"] == version
    assert len(audits(path)) == 1


def test_same_key_with_different_request_conflicts_without_mutation(resets):
    from api.services.adminCreditResetRepository import AdminCreditResetConflict
    resetRepository, _, path = resets
    activateMonthly(resets)
    reset(resets)
    version = read_row(path, "credit_balances")["balance_version"]
    with pytest.raises(AdminCreditResetConflict):
        resetRepository.createOrGetOperation("individual", USER, "A different reason", "key-1", ADMIN)
    with pytest.raises(AdminCreditResetConflict):
        resetRepository.createOrGetOperation("all", None, REASON, "key-1", ADMIN)
    assert read_row(path, "credit_balances")["balance_version"] == version
    assert len(rows(path, "SELECT * FROM admin_credit_reset_operations")) == 1


def test_other_admin_key_creates_separate_provenance(resets):
    _, _, path = resets
    activateMonthly(resets)
    first, _ = reset(resets)
    second, target = reset(resets, admin=OTHER_ADMIN)
    assert str(second["id"]) != str(first["id"]) and target["outcome"] == "RESET"
    assert sorted(row["admin_id"] for row in audits(path)) == sorted([ADMIN.adminId, OTHER_ADMIN.adminId])


def test_paid_balance_missing_fails_closed_with_audited_skip(resets):
    _, _, path = resets
    activateMonthly(resets)
    with sqlTransaction(path) as connection:
        connection.execute("DELETE FROM credit_balances")
    _, target = reset(resets)
    assert target["outcome"] == "SKIPPED" and target["reason_code"] == "PAID_BALANCE_MISSING"
    assert read_row(path, "credit_balances") is None
    [audit] = audits(path)
    assert audit["outcome"] == "SKIPPED" and json.loads(audit["details"])["reasonCode"] == "PAID_BALANCE_MISSING"


def test_corrupt_allocation_owner_is_skipped_without_partial_repair(resets):
    _, _, path = resets
    activateMonthly(resets)
    with sqlTransaction(path) as connection:
        connection.execute("UPDATE credit_balances SET plan_tier='annual'")
    before = read_row(path, "credit_balances")
    _, target = reset(resets)
    assert target["reason_code"] == "ALLOCATION_OWNERSHIP_INVALID"
    assert read_row(path, "credit_balances") == before


def test_live_trial_initializes_one_allocation(resets):
    _, _, path = resets
    with sqlTransaction(path) as connection:
        connection.execute("UPDATE subscriptions SET billing_mode='none',status='trial',plan_type='free',"
            "current_period_start=?,current_period_end=?,domain_count=4",
            ((NOW - timedelta(days=1)).isoformat(), (NOW + timedelta(days=11)).isoformat()))
    _, target = reset(resets)
    balance = read_row(path, "credit_balances")
    assert target["outcome"] == "RESET"
    assert balance["monthly_token_quota"] == 5000 and balance["remaining_tokens"] == 5000
    assert balance["topup_tokens"] == 0 and balance["balance_version"] == 1
    allocation = rows(path, "SELECT metadata_json FROM billing_events WHERE idempotency_key=?",
                      ("credit-allocation:" + USER + ":" + balance["credit_period_id"],))
    assert json.loads(allocation[0]["metadata_json"])["quotaWatermark"] == 5000


@pytest.mark.parametrize("change,code", [
    ("EXPIRE", "NO_ACTIVE_COVERAGE"),
    ("UPDATE \"Users\" SET \"isBanned\"=1", "ACCOUNT_BANNED"),
    ("UPDATE subscriptions SET erasure_pending=1", "ERASURE_PENDING"),
    ("UPDATE subscriptions SET status='suspended'", "RESTRICTED_SUBSCRIPTION"),
    ("UPDATE subscriptions SET is_canonical=0", "CANONICAL_SUBSCRIPTION_MISSING"),
])
def test_ineligible_states_are_audited_skips_without_grant(resets, change, code):
    _, _, path = resets
    activateMonthly(resets)
    if change != "EXPIRE":
        with sqlTransaction(path) as connection:
            connection.execute(change)
    before = read_row(path, "credit_balances")
    later = NOW + timedelta(days=32) if change == "EXPIRE" else NOW
    with patch("api.services.billing.manualBillingRepository._now", side_effect=lambda: later):
        _, target = reset(resets)
    after = read_row(path, "credit_balances")
    assert target["outcome"] == "SKIPPED" and target["reason_code"] == code
    assert after["topup_tokens"] == 2500 and after["credit_period_id"] == before["credit_period_id"]
    assert after["remaining_tokens"] in (before["remaining_tokens"], 0)
    assert len(audits(path)) == 1


def test_expired_trial_is_skipped(resets):
    _, _, path = resets
    with sqlTransaction(path) as connection:
        connection.execute("UPDATE subscriptions SET billing_mode='none',status='trial',plan_type='free',"
            "current_period_start=?,current_period_end=?",
            ((NOW - timedelta(days=13)).isoformat(), (NOW - timedelta(days=1)).isoformat()))
    _, target = reset(resets)
    assert target["reason_code"] == "NO_ACTIVE_COVERAGE" and read_row(path, "credit_balances") is None


def test_absent_user_is_audited_not_found(resets):
    _, _, path = resets
    _, target = reset(resets, userId="missing-user")
    assert target["outcome"] == "SKIPPED" and target["reason_code"] == "USER_NOT_FOUND"
    assert audits(path)[0]["target_id"] == "missing-user"


def test_audit_failure_rolls_back_grant_and_outcome(resets):
    resetRepository, _, path = resets
    before = activateMonthly(resets)
    with sqlTransaction(path) as connection:
        connection.execute("DROP TABLE admin_audit_log")
    with patch("api.services.credits.creditConfig.getTokenQuotaForPlan", side_effect=quota):
        operation = resetRepository.createOrGetOperation("individual", USER, REASON, "key-1", ADMIN)
        with pytest.raises(Exception):
            resetRepository.resetTarget(str(operation["id"]), USER)
    assert read_row(path, "credit_balances") == before
    [target] = rows(path, "SELECT * FROM admin_credit_reset_targets")
    assert target["outcome"] == "PENDING" and target["audit_id"] is None
    assert not rows(path, "SELECT id FROM billing_events WHERE event_type='credit.allocation_started' "
                          "AND metadata_json LIKE '%admin_credit_reset%'")
    assert resetRepository.recordRetryableFailure(str(operation["id"]), USER, "RESET_TRANSACTION_FAILED")
    assert rows(path, "SELECT outcome FROM admin_credit_reset_targets")[0]["outcome"] == "RETRYABLE_FAILED"


def test_retryable_failure_never_overwrites_terminal_target(resets):
    resetRepository, _, path = resets
    activateMonthly(resets)
    operation, target = reset(resets)
    assert not resetRepository.recordRetryableFailure(str(operation["id"]), USER, "RESET_TRANSACTION_FAILED")
    assert rows(path, "SELECT outcome FROM admin_credit_reset_targets")[0]["outcome"] == "RESET"


def test_operation_stores_hashed_key_and_no_token(resets):
    _, _, path = resets
    activateMonthly(resets)
    reset(resets, key="  key-1  ")
    [operation] = rows(path, "SELECT * FROM admin_credit_reset_operations")
    assert "key-1" not in json.dumps(operation) and ADMIN.token not in json.dumps(operation)
    assert len(operation["idempotency_key_hash"]) == 64 and operation["reason"] == REASON
