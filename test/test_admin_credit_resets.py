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
from test.test_admin_auth_service import authFixture, passwordHasher  # noqa: F401
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


# ---- HTTP: genuine admin authentication and the individual endpoint -----------

class FakeCreditProjection:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls = []

    def invalidateCreditProjection(self, userId):
        self.calls.append(userId)
        return self.outcomes.pop(0) if self.outcomes else True


@pytest.fixture
def api(resets, authFixture):
    from fastapi.testclient import TestClient
    from api.services.adminAuthService import getAdminAuthService
    from api.services.adminCreditResetService import (
        AdminCreditResetService,
        getAdminCreditResetService,
    )
    from main import app as productionApp
    from test.test_admin_auth_service import VALID_PASSWORD
    resetRepository, repository, path = resets
    projection = FakeCreditProjection()
    service = AdminCreditResetService(repository=resetRepository, creditService=projection)
    productionApp.dependency_overrides[getAdminAuthService] = lambda: authFixture.service
    productionApp.dependency_overrides[getAdminCreditResetService] = lambda: service
    login = authFixture.service.login("admin@example.com", VALID_PASSWORD, "203.0.113.10")
    client = TestClient(productionApp, raise_server_exceptions=False)
    quotaPatch = patch("api.services.credits.creditConfig.getTokenQuotaForPlan", side_effect=quota)
    quotaPatch.start()
    try:
        yield {"client": client, "token": login["token"], "path": path, "projection": projection,
               "auth": authFixture, "adminId": login["admin"]["id"], "service": service}
    finally:
        quotaPatch.stop()
        client.close()
        productionApp.dependency_overrides.clear()


def headers(api, key="reset-key-1", token=None):
    values = {"Authorization": "Bearer " + (token or api["token"])}
    if key is not None:
        values["Idempotency-Key"] = key
    return values


def resetUser(api, userId=USER, body=None, key="reset-key-1"):
    return api["client"].post(f"/admin/users/{userId}/credits/reset",
                              json={"reason": REASON} if body is None else body,
                              headers=headers(api, key=key))


def test_genuine_admin_resets_one_user(api, resets):
    activateMonthly(resets)
    response = resetUser(api)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scope"] == "individual" and body["status"] == "COMPLETED"
    assert body["requestedByAdminId"] == api["adminId"] and body["reason"] == REASON
    assert (body["totalTargets"], body["resetCount"], body["skippedCount"], body["pendingCount"],
            body["retryableFailureCount"], body["cachePendingCount"]) == (1, 1, 0, 0, 0, 0)
    [target] = body["targets"]
    assert target["userId"] == USER and target["outcome"] == "RESET"
    assert target["before"]["usedTokens"] == 7000 and target["after"]["remainingTokens"] == 10000
    assert target["after"]["topupTokens"] == 2500 and target["cacheState"] == "INVALIDATED"
    assert api["projection"].calls == [USER]
    assert audits(api["path"])[0]["admin_id"] == api["adminId"]
    assert api["token"] not in response.text


def test_ordinary_and_billing_admin_user_tokens_are_rejected(api, resets, monkeypatch):
    import os
    from jose import jwt
    activateMonthly(resets)
    monkeypatch.setenv("BILLING_ADMIN_USER_IDS", USER)
    userToken = jwt.encode({"userId": USER, "email": "user@example.com"},
                           os.environ["SECRET_KEY"], algorithm="HS256")
    for token in (userToken, "not-a-token"):
        response = api["client"].post(f"/admin/users/{USER}/credits/reset", json={"reason": REASON},
                                      headers=headers(api, token=token))
        assert response.status_code == 401
    missing = api["client"].post(f"/admin/users/{USER}/credits/reset", json={"reason": REASON},
                                 headers={"Idempotency-Key": "k"})
    assert missing.status_code == 401
    assert not rows(api["path"], "SELECT * FROM admin_credit_reset_operations")


def test_revoked_admin_session_cannot_reset(api, resets):
    activateMonthly(resets)
    api["auth"].client.rows["admin_sessions"][-1]["revoked_at"] = "2030-01-02T03:04:05+00:00"
    assert resetUser(api).status_code == 401
    assert not rows(api["path"], "SELECT * FROM admin_credit_reset_operations")


@pytest.mark.parametrize("body,key", [
    ({"reason": "   "}, "k-1"),
    ({"reason": "x" * 2001}, "k-1"),
    ({}, "k-1"),
    ({"reason": REASON, "amount": 50000}, "k-1"),
    ({"reason": REASON, "resetTopups": True}, "k-1"),
    ({"reason": REASON, "adminId": "someone-else"}, "k-1"),
    ({"reason": REASON}, None),
    ({"reason": REASON}, "   "),
    ({"reason": REASON}, "k" * 129),
])
def test_invalid_reset_requests_are_rejected_without_mutation(api, resets, body, key):
    activateMonthly(resets)
    assert resetUser(api, body=body, key=key).status_code == 422
    assert not rows(api["path"], "SELECT * FROM admin_credit_reset_operations")


def test_overlong_user_id_is_rejected(api):
    assert resetUser(api, userId="u" * 129).status_code == 422
    assert not rows(api["path"], "SELECT * FROM admin_credit_reset_operations")


def test_unknown_user_is_404_with_audited_operation(api):
    response = resetUser(api, userId="nobody-here")
    assert response.status_code == 404
    errors = response.json()["errors"]
    assert errors["reasonCode"] == "USER_NOT_FOUND" and errors["operationId"]
    assert audits(api["path"])[0]["outcome"] == "SKIPPED"


def test_ineligible_user_is_409_with_operation_and_replays_same(api, resets):
    activateMonthly(resets)
    with sqlTransaction(api["path"]) as connection:
        connection.execute("UPDATE subscriptions SET status='suspended'")
    first, again = resetUser(api), resetUser(api)
    assert first.status_code == again.status_code == 409
    assert first.json()["errors"]["reasonCode"] == "RESTRICTED_SUBSCRIPTION"
    assert first.json()["errors"]["operationId"] == again.json()["errors"]["operationId"]
    assert len(audits(api["path"])) == 1


def test_conflicting_key_reuse_is_409_without_second_operation(api, resets):
    activateMonthly(resets)
    resetUser(api)
    assert resetUser(api, body={"reason": "Different reason"}).status_code == 409
    assert len(rows(api["path"], "SELECT * FROM admin_credit_reset_operations")) == 1


def test_cache_failure_stays_pending_and_replay_repairs_without_regrant(api, resets):
    activateMonthly(resets)
    api["projection"].outcomes = [False]
    first = resetUser(api)
    assert first.status_code == 200
    assert first.json()["targets"][0]["cacheState"] == "PENDING" and first.json()["cachePendingCount"] == 1
    version = read_row(api["path"], "credit_balances")["balance_version"]
    with sqlTransaction(api["path"]) as connection:
        connection.execute("UPDATE credit_balances SET used_tokens=10,remaining_tokens=9990")
    again = resetUser(api)
    assert again.status_code == 200
    assert again.json()["targets"][0]["cacheState"] == "INVALIDATED"
    assert again.json()["targets"][0]["after"]["remainingTokens"] == 10000
    assert read_row(api["path"], "credit_balances")["remaining_tokens"] == 9990
    assert read_row(api["path"], "credit_balances")["balance_version"] == version
    assert len(audits(api["path"])) == 1 and api["projection"].calls == [USER, USER]


def test_audit_outage_is_503_without_grant(api, resets):
    before = activateMonthly(resets)
    with sqlTransaction(api["path"]) as connection:
        connection.execute("DROP TABLE admin_audit_log")
    assert resetUser(api).status_code == 503
    assert read_row(api["path"], "credit_balances") == before
    assert rows(api["path"], "SELECT outcome FROM admin_credit_reset_targets")[0]["outcome"] == "RETRYABLE_FAILED"


def test_legacy_force_reset_route_is_removed():
    from fastapi.routing import APIRoute
    from fastapi.testclient import TestClient
    from main import app as productionApp
    paths = {route.path for route in productionApp.routes if isinstance(route, APIRoute)}
    assert not [path for path in paths if "force-reset" in path]
    with TestClient(productionApp, raise_server_exceptions=False) as client:
        assert client.post("/billing-admin/credits/force-reset?resetUsage=true").status_code == 404


def test_reset_routes_use_genuine_admin_dependency():
    from fastapi.routing import APIRoute
    from api.services.adminAuthService import verifyAdmin
    from main import app as productionApp
    routes = [route for route in productionApp.routes if isinstance(route, APIRoute)
              and route.path.startswith("/admin/") and "credits" in route.path]
    assert routes
    for route in routes:
        assert any(dependency.call is verifyAdmin for dependency in route.dependant.dependencies)


def test_credit_projection_invalidation_deletes_only_one_key():
    from api.services.credits.creditService import CreditService
    deleted = []

    class Redis:
        def delete(self, *keys):
            deleted.extend(keys)
            return 1

    service = CreditService.__new__(CreditService)
    service._redis = lambda: Redis()
    assert service.invalidateCreditProjection(USER) is True
    assert deleted == ["credits:v3:" + USER]
    service._redis = lambda: (_ for _ in ()).throw(RuntimeError("redis down"))
    assert service.invalidateCreditProjection(USER) is False


# ---- HTTP: resumable all-user reset and operation inspection -------------------

def addUsers(path, count, prefix="bulk-user-"):
    ids = [f"{prefix}{index:03d}" for index in range(count)]
    with sqlTransaction(path) as connection:
        connection.executemany('INSERT INTO "Users"("userId") VALUES(?)', [(userId,) for userId in ids])
    return ids


def resetAll(api, key="bulk-key-1", body=None, token=None):
    return api["client"].post("/admin/credits/reset-all",
                              json={"reason": REASON} if body is None else body,
                              headers=headers(api, key=key, token=token))


def getOperation(api, operationId, **params):
    return api["client"].get(f"/admin/credits/reset-operations/{operationId}", params=params,
                             headers={"Authorization": "Bearer " + api["token"]})


def targetRows(path):
    return rows(path, "SELECT * FROM admin_credit_reset_targets ORDER BY user_id")


def test_empty_bulk_completes_with_zero_counts(api):
    with sqlTransaction(api["path"]) as connection:
        connection.execute('DELETE FROM "Users"')
    response = resetAll(api)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "COMPLETED" and body["scope"] == "all" and body["totalTargets"] == 0
    assert body["targets"] == [] and body["nextAfterUserId"] is None


def test_bulk_resets_eligible_and_audits_skips(api, resets):
    activateMonthly(resets)
    addUsers(api["path"], 3)
    with sqlTransaction(api["path"]) as connection:
        connection.execute('UPDATE "Users" SET "isBanned"=1 WHERE "userId"=?', ("bulk-user-001",))
    response = resetAll(api)
    body = response.json()
    assert response.status_code == 200 and body["status"] == "COMPLETED"
    assert (body["totalTargets"], body["resetCount"], body["skippedCount"]) == (4, 1, 3)
    outcomes = {row["user_id"]: (row["outcome"], row["reason_code"]) for row in targetRows(api["path"])}
    assert outcomes[USER] == ("RESET", None)
    assert outcomes["bulk-user-000"] == ("SKIPPED", "CANONICAL_SUBSCRIPTION_MISSING")
    assert len(audits(api["path"])) == 4
    assert read_row(api["path"], "credit_balances")["topup_tokens"] == 2500


def test_bulk_processes_bounded_batches_and_freezes_membership(api, resets):
    activateMonthly(resets)
    addUsers(api["path"], 105)
    first = resetAll(api)
    assert first.status_code == 202
    body = first.json()
    assert body["status"] == "RUNNING" and body["totalTargets"] == 106
    assert body["resetCount"] + body["skippedCount"] == 100 and body["pendingCount"] == 6
    addUsers(api["path"], 2, prefix="late-user-")
    with sqlTransaction(api["path"]) as connection:
        connection.execute('DELETE FROM "Users" WHERE "userId"=?', ("bulk-user-104",))
    second = resetAll(api)
    assert second.status_code == 200 and second.json()["status"] == "COMPLETED"
    assert second.json()["totalTargets"] == 106
    outcomes = {row["user_id"]: row for row in targetRows(api["path"])}
    assert "late-user-000" not in outcomes
    assert outcomes["bulk-user-104"]["reason_code"] == "USER_NOT_FOUND"
    assert len(audits(api["path"])) == 106
    version = read_row(api["path"], "credit_balances")["balance_version"]
    third = resetAll(api)
    assert third.status_code == 200 and len(audits(api["path"])) == 106
    assert read_row(api["path"], "credit_balances")["balance_version"] == version


def test_bulk_partial_failure_retries_only_unfinished_targets(api, resets):
    activateMonthly(resets)
    addUsers(api["path"], 2)
    repository = api["service"].repository
    original = repository.resetTarget
    failures = {"bulk-user-001": 1}

    def flaky(operationId, userId):
        if failures.get(userId):
            failures[userId] -= 1
            raise RuntimeError("audit database unavailable")
        return original(operationId, userId)

    repository.resetTarget = flaky
    first = resetAll(api)
    assert first.status_code == 202
    body = first.json()
    assert (body["resetCount"], body["skippedCount"], body["retryableFailureCount"], body["pendingCount"]) == (1, 1, 1, 0)
    version = read_row(api["path"], "credit_balances")["balance_version"]
    second = resetAll(api)
    assert second.status_code == 200 and second.json()["retryableFailureCount"] == 0
    assert read_row(api["path"], "credit_balances")["balance_version"] == version
    assert sorted(row["target_id"] for row in audits(api["path"])) == sorted([USER, "bulk-user-000", "bulk-user-001"])


def test_bulk_cache_failure_keeps_completed_status_and_repairs_on_replay(api, resets):
    activateMonthly(resets)
    api["projection"].outcomes = [False]
    first = resetAll(api)
    assert first.status_code == 200
    assert first.json()["status"] == "COMPLETED" and first.json()["cachePendingCount"] == 1
    version = read_row(api["path"], "credit_balances")["balance_version"]
    operationId = first.json()["operationId"]
    inspected = getOperation(api, operationId)
    assert inspected.json()["cachePendingCount"] == 1 and api["projection"].calls == [USER]
    second = resetAll(api)
    assert second.json()["cachePendingCount"] == 0
    assert read_row(api["path"], "credit_balances")["balance_version"] == version
    assert len(audits(api["path"])) == 1


def test_bulk_key_conflicts_across_scopes_and_is_scoped_to_admin(api, resets, authFixture):
    from test.test_admin_auth_service import OTHER_PASSWORD
    activateMonthly(resets)
    assert resetUser(api, key="shared-key").status_code == 200
    assert resetAll(api, key="shared-key").status_code == 409
    other = authFixture.service.login("other@example.com", OTHER_PASSWORD, "203.0.113.11")
    response = resetAll(api, key="shared-key", token=other["token"])
    assert response.status_code == 200
    assert response.json()["requestedByAdminId"] == other["admin"]["id"]
    assert len(rows(api["path"], "SELECT * FROM admin_credit_reset_operations")) == 2


def test_bulk_requires_genuine_admin_and_valid_input(api, resets):
    import os
    from jose import jwt
    activateMonthly(resets)
    userToken = jwt.encode({"userId": USER, "email": "user@example.com"}, os.environ["SECRET_KEY"], algorithm="HS256")
    assert resetAll(api, token=userToken).status_code == 401
    assert resetAll(api, body={"reason": ""}).status_code == 422
    assert resetAll(api, body={"reason": REASON, "userIds": [USER]}).status_code == 422
    assert resetAll(api, key=None).status_code == 422
    assert not rows(api["path"], "SELECT * FROM admin_credit_reset_operations")


def test_operation_read_paginates_by_user_id(api, resets):
    activateMonthly(resets)
    addUsers(api["path"], 60)
    operationId = resetAll(api).json()["operationId"]
    first = getOperation(api, operationId)
    assert first.status_code == 200
    page = first.json()
    assert len(page["targets"]) == 50 and page["totalTargets"] == 61
    assert page["targets"][-1]["userId"] == page["nextAfterUserId"]
    rest = getOperation(api, operationId, afterUserId=page["nextAfterUserId"], limit=100).json()
    assert len(rest["targets"]) == 11 and rest["nextAfterUserId"] is None
    seen = [target["userId"] for target in page["targets"] + rest["targets"]]
    assert seen == sorted(seen) and len(set(seen)) == 61
    assert getOperation(api, operationId, limit=101).status_code == 422
    assert getOperation(api, operationId, limit=0).status_code == 422


def test_operation_read_is_404_for_unknown_or_malformed_and_requires_admin(api):
    assert getOperation(api, str(uuid.uuid4())).status_code == 404
    assert getOperation(api, "not-a-uuid").status_code == 404
    unauthenticated = api["client"].get(f"/admin/credits/reset-operations/{uuid.uuid4()}")
    assert unauthenticated.status_code == 401
