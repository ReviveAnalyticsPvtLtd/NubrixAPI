"""Admin credit resets against a disposable PostgreSQL database.

Proves the real forward migration, strict-audit rollback, trigger
immutability, RLS/grants and two-session locking that SQLite cannot prove.
Opt-in only through the existing guarded MANUAL_BILLING_TEST_DATABASE_URL.
"""
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

import psycopg2
import pytest
from psycopg2.extras import Json, RealDictCursor

from api.services.adminAuthService import AdminContext
from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
from test.test_manual_billing_postgres_integration import NOW, payment, postgres  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_MANUAL_BILLING_INTEGRATION") != "1",
    reason="Disposable PostgreSQL opt-in required; skipped is UNVERIFIED",
)

ADMIN = AdminContext(adminId=str(uuid.uuid4()), email="ops@example.com", name="Ops",
                     sessionId=str(uuid.uuid4()), token="admin-token-never-stored")
REASON = "Support-approved allowance reset"


def quota(plan, count):
    return {"pro": 10000, "annual": 30000, "free": 5000}[plan] * (count if plan != "free" else 1)


@pytest.fixture(autouse=True)
def configuredQuota():
    with patch("api.services.credits.creditConfig.getTokenQuotaForPlan", side_effect=quota):
        yield


def query(url, sql, params=()):
    with psycopg2.connect(url) as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()] if cursor.description else []


def execute(url, sql, params=()):
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)


@pytest.fixture
def active(payment):
    from api.services.adminCreditResetRepository import AdminCreditResetRepository
    repository, evidence, url = payment
    repository.finalizeCapturedPayment(evidence)
    execute(url, "update public.credit_balances set used_tokens=7000,remaining_tokens=3000,"
                 "topup_tokens=2500 where user_id=%s", (evidence.userId,))
    return AdminCreditResetRepository(repository), repository, evidence.userId, url


def balance(url, userId):
    return query(url, "select * from public.credit_balances where user_id=%s", (userId,))[0]


def audits(url, userId):
    return query(url, "select * from public.admin_audit_log where action='credits.reset' and target_id=%s", (userId,))


def operation(resets, userId, key=None, admin=ADMIN, scope="individual"):
    return resets.createOrGetOperation(scope, userId if scope == "individual" else None,
                                       REASON, key or str(uuid.uuid4()), admin)


def test_real_migration_enforces_rls_grants_and_terminal_immutability(active):
    resets, _, userId, url = active
    tables = query(url, "select relname, relrowsecurity from pg_class where relname in "
                        "('admin_credit_reset_operations','admin_credit_reset_targets')")
    assert {row["relname"]: row["relrowsecurity"] for row in tables} == {
        "admin_credit_reset_operations": True, "admin_credit_reset_targets": True}
    privileges = query(url, """select
        has_table_privilege('anon','public.admin_credit_reset_targets','select') as anon_select,
        has_table_privilege('authenticated','public.admin_credit_reset_operations','insert') as auth_insert,
        has_table_privilege('service_role','public.admin_credit_reset_targets','update') as service_update,
        has_table_privilege('service_role','public.admin_credit_reset_targets','delete') as service_delete,
        has_table_privilege('service_role','public.admin_credit_reset_operations','update') as op_update""")[0]
    assert privileges == {"anon_select": False, "auth_insert": False, "service_update": True,
                          "service_delete": False, "op_update": False}
    stored = operation(resets, userId)
    target = resets.resetTarget(str(stored["id"]), userId)
    assert target["outcome"] == "RESET"
    with pytest.raises(psycopg2.Error):
        execute(url, "update public.admin_credit_reset_targets set outcome='RETRYABLE_FAILED' "
                     "where operation_id=%s", (stored["id"],))
    with pytest.raises(psycopg2.Error):
        execute(url, "update public.admin_credit_reset_targets set after_snapshot='{}'::jsonb "
                     "where operation_id=%s", (stored["id"],))
    assert resets.markCacheInvalidated(str(stored["id"]), userId)
    with pytest.raises(psycopg2.Error):
        execute(url, "update public.admin_credit_reset_targets set cache_state='PENDING' "
                     "where operation_id=%s", (stored["id"],))


def test_reset_commits_balance_allocation_target_and_strict_audit(active):
    resets, _, userId, url = active
    before = balance(url, userId)
    stored = operation(resets, userId)
    target = resets.resetTarget(str(stored["id"]), userId)
    after = balance(url, userId)
    assert (after["used_tokens"], after["remaining_tokens"], after["topup_tokens"]) == (0, 10000, 2500)
    assert after["credit_period_id"] != before["credit_period_id"]
    assert after["balance_version"] == before["balance_version"] + 1
    assert (after["period_start"], after["period_end"]) == (before["period_start"], before["period_end"])
    [audit] = audits(url, userId)
    assert str(audit["id"]) == target["audit_id"] and audit["outcome"] == "RESET"
    assert (str(audit["admin_id"]), str(audit["session_id"]), audit["admin_email"]) == (
        ADMIN.adminId, ADMIN.sessionId, ADMIN.email)
    assert audit["details"]["reason"] == REASON and audit["details"]["before"]["usedTokens"] == 7000
    assert ADMIN.token not in str(audit["details"])
    allocation = query(url, "select metadata_json from public.billing_events where idempotency_key=%s",
                       ("credit-allocation:" + userId + ":" + str(after["credit_period_id"]),))
    assert allocation[0]["metadata_json"]["quotaWatermark"] == 10000


def test_audit_insert_failure_rolls_back_grant_target_and_allocation(active):
    resets, _, userId, url = active
    before = balance(url, userId)
    function = "fail_reset_audit_" + uuid.uuid4().hex[:8]
    execute(url, f"""create function public.{function}() returns trigger language plpgsql as $$
        begin if new.target_id = '{userId}' then raise exception 'injected audit failure'; end if;
        return new; end $$;
        create trigger {function} before insert on public.admin_audit_log
        for each row execute function public.{function}();""")
    try:
        stored = operation(resets, userId)
        with pytest.raises(psycopg2.Error):
            resets.resetTarget(str(stored["id"]), userId)
    finally:
        execute(url, f"drop trigger {function} on public.admin_audit_log; drop function public.{function}();")
    assert balance(url, userId) == before
    [target] = query(url, "select * from public.admin_credit_reset_targets where operation_id=%s", (stored["id"],))
    assert target["outcome"] == "PENDING" and target["audit_id"] is None
    assert not query(url, "select id from public.billing_events where user_id=%s and "
                          "metadata_json->>'source'='admin_credit_reset'", (userId,))
    assert resets.resetTarget(str(stored["id"]), userId)["outcome"] == "RESET"
    assert len(audits(url, userId)) == 1


def test_two_sessions_with_one_key_create_one_operation(active):
    resets, _, userId, url = active
    key = "shared-" + str(uuid.uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: operation(resets, userId, key=key), range(2)))
    assert str(results[0]["id"]) == str(results[1]["id"])
    assert len(query(url, "select * from public.admin_credit_reset_targets where operation_id=%s",
                     (results[0]["id"],))) == 1


def test_duplicate_workers_reset_one_target_once(active):
    resets, _, userId, url = active
    before = balance(url, userId)
    stored = operation(resets, userId)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: resets.resetTarget(str(stored["id"]), userId), range(2)))
    assert [result["outcome"] for result in results] == ["RESET", "RESET"]
    assert results[0]["audit_id"] == results[1]["audit_id"]
    assert balance(url, userId)["balance_version"] == before["balance_version"] + 1
    assert len(audits(url, userId)) == 1


def test_two_operations_on_one_user_serialize_and_preserve_topups(active):
    resets, _, userId, url = active
    before = balance(url, userId)
    first, second = operation(resets, userId), operation(resets, userId)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda stored: resets.resetTarget(str(stored["id"]), userId), [first, second]))
    after = balance(url, userId)
    assert after["balance_version"] == before["balance_version"] + 2
    assert (after["remaining_tokens"], after["topup_tokens"]) == (10000, 2500)
    assert len(audits(url, userId)) == 2


def test_wait_for_owner_lock_past_expiry_skips_without_grant(active):
    resets, repository, userId, url = active
    execute(url, "update public.\"Invoices\" set period_end=clock_timestamp()+interval '1 second' "
                 "where \"userId\"=%s and billing_reason='initial_purchase'", (userId,))
    execute(url, "update public.subscriptions set current_period_end=clock_timestamp()+interval '1 second' "
                 "where user_id=%s", (userId,))
    stored = operation(resets, userId)
    blocker = repository.connectionFactory()
    with blocker.cursor() as cursor:
        repository._lockUser(cursor, userId)
    started = threading.Event()
    originalLock = repository._lockUser

    def observedLock(cursor, lockedUser):
        started.set()
        originalLock(cursor, lockedUser)

    try:
        with patch.object(repository, "_lockUser", side_effect=observedLock):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(resets.resetTarget, str(stored["id"]), userId)
                assert started.wait(5)
                time.sleep(1.2)
                blocker.commit()
                target = pending.result(timeout=10)
    finally:
        blocker.close()
    assert target["outcome"] == "SKIPPED" and target["reason_code"] == "NO_ACTIVE_COVERAGE"
    after = balance(url, userId)
    assert after["topup_tokens"] == 2500 and after["remaining_tokens"] in (0, 3000)


def test_crash_after_balance_write_rolls_back_and_retry_resets_once(active):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    resets, _, userId, url = active
    before = balance(url, userId)
    stored = operation(resets, userId)
    original = ManualCreditRepository.resetQuotaLocked

    def crashAfterWrite(self, cursor, subscription, now):
        original(self, cursor, subscription, now)
        raise RuntimeError("worker died after balance write")

    with patch.object(ManualCreditRepository, "resetQuotaLocked", crashAfterWrite):
        with pytest.raises(RuntimeError):
            resets.resetTarget(str(stored["id"]), userId)
    assert balance(url, userId) == before
    assert resets.resetTarget(str(stored["id"]), userId)["outcome"] == "RESET"
    assert resets.resetTarget(str(stored["id"]), userId)["outcome"] == "RESET"
    assert balance(url, userId)["balance_version"] == before["balance_version"] + 1
    assert len(audits(url, userId)) == 1


def test_reset_racing_old_settlement_conserves_topups(active):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    resets, repository, userId, url = active
    credits = ManualCreditRepository(repository)
    old = credits.admit(userId, "reporting_query", "pre-reset-" + str(uuid.uuid4()))
    execute(url, "update public.credit_balances set remaining_tokens=100,used_tokens=9900 where user_id=%s", (userId,))
    stored = operation(resets, userId)
    barrier = threading.Barrier(2)

    def settle():
        barrier.wait()
        return credits.settle(old, 400, "old-run")

    def reset():
        barrier.wait()
        return resets.resetTarget(str(stored["id"]), userId)

    with ThreadPoolExecutor(max_workers=2) as pool:
        settled, target = pool.submit(settle), pool.submit(reset)
        settled, target = settled.result(timeout=10), target.result(timeout=10)
    after = balance(url, userId)
    assert target["outcome"] == "RESET"
    assert (after["used_tokens"], after["remaining_tokens"]) == (0, 10000)
    assert after["topup_tokens"] == 2500 - settled["topupCharged"]
    assert settled["monthlyCharged"] + settled["topupCharged"] + settled["unfundedTokens"] == 400


def test_reset_racing_topup_capture_keeps_purchase(active):
    resets, repository, userId, url = active
    subscription = query(url, "select id from public.subscriptions where user_id=%s", (userId,))[0]
    lifecycle = query(url, "select lifecycle_id from public.credit_balances where user_id=%s", (userId,))[0]["lifecycle_id"]
    invoice, order, paymentId = map(str, (uuid.uuid4(), uuid.uuid4(), uuid.uuid4()))
    execute(url, '''insert into public."Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,currency,metadata_json)
        values(%s,%s,%s,'PAYMENT_PENDING','add_on',150,'INR',%s)''', (invoice, userId, subscription["id"], Json({"tokens": 500})))
    attempt = repository.reserveCheckoutIntent(userId, "topup", invoice, invoice, {
        "subscriptionId": str(subscription["id"]), "invoiceId": invoice, "lifecycleId": str(lifecycle),
        "billingMode": "monthly_prepaid", "tokens": 500, "amount": 150, "currency": "INR",
        "expiresAt": (NOW + timedelta(minutes=30)).isoformat()})
    repository.bindProviderOrder(attempt.attemptId, {"id": order})
    capture = VerifiedPaymentEvidence(attempt.attemptId, invoice, userId, order, paymentId, "topup", "INR",
                                      "captured", "attested_capture", 150, NOW + timedelta(minutes=1),
                                      NOW + timedelta(minutes=1), None, True)
    stored = operation(resets, userId)
    barrier = threading.Barrier(2)

    def grant():
        barrier.wait()
        return repository.finalizeCapturedPayment(capture)

    def reset():
        barrier.wait()
        return resets.resetTarget(str(stored["id"]), userId)

    with ThreadPoolExecutor(max_workers=2) as pool:
        granted, target = pool.submit(grant), pool.submit(reset)
        assert granted.result(timeout=10).state == "topup_granted"
        assert target.result(timeout=10)["outcome"] == "RESET"
    after = balance(url, userId)
    assert (after["topup_tokens"], after["remaining_tokens"]) == (3000, 10000)


def test_due_paid_boundary_materializes_before_reset_without_new_invoice(active):
    resets, repository, userId, url = active
    current = query(url, "select id from public.\"Invoices\" where \"userId\"=%s and billing_reason='initial_purchase'", (userId,))[0]
    future = query(url, "select id from public.\"Invoices\" where \"userId\"=%s and billing_reason='renewal'", (userId,))
    assert not future
    # A paid next month whose start has already passed: the reset must materialize it first.
    invoices = query(url, "select count(*) as total from public.\"Invoices\" where \"userId\"=%s", (userId,))[0]["total"]
    renewal = str(uuid.uuid4())
    sub = query(url, "select * from public.subscriptions where user_id=%s", (userId,))[0]
    lifecycle = sub["billing_state"]["manualBilling"]["lifecycleId"]
    execute(url, '''insert into public."Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,currency,
        period_start,period_end,"razorpayPaymentId",metadata_json)
        values(%s,%s,%s,'PAID','renewal',3000,'INR',clock_timestamp()-interval '1 hour',
               clock_timestamp()-interval '1 hour'+interval '1 month','renew-pay',%s)''',
        (renewal, userId, sub["id"], Json({"manualBilling": {"lifecycleId": lifecycle, "purpose": "renewal",
            "billingMode": "monthly_prepaid", "domains": ["banking"], "coverageState": "scheduled",
            "creditPeriodId": str(uuid.uuid4())}})))
    execute(url, '''update public."Invoices" set period_start=period_start-interval '1 month',
        period_end=(select period_start from public."Invoices" where id=%s) where id=%s''', (renewal, current["id"]))
    execute(url, '''update public.subscriptions set current_period_start=current_period_start-interval '1 month',
        current_period_end=(select period_start from public."Invoices" where id=%s) where user_id=%s''', (renewal, userId))
    stored = operation(resets, userId)
    target = resets.resetTarget(str(stored["id"]), userId)
    assert target["outcome"] == "RESET"
    renewed = query(url, "select metadata_json, period_start, period_end from public.\"Invoices\" where id=%s", (renewal,))[0]
    assert renewed["metadata_json"]["manualBilling"]["coverageState"] == "active"
    assert target["before_snapshot"]["creditPeriodId"] == renewed["metadata_json"]["manualBilling"]["creditPeriodId"]
    assert query(url, "select count(*) as total from public.\"Invoices\" where \"userId\"=%s", (userId,))[0]["total"] == invoices + 1
    after = balance(url, userId)
    assert after["period_end"] == renewed["period_end"] and after["remaining_tokens"] == 10000
    assert after["topup_tokens"] == 2500


def test_bulk_operation_freezes_membership_and_concurrent_resumes_reset_each_target_once(active):
    from api.services.adminCreditResetService import AdminCreditResetService
    from api.adminModels import AdminCreditResetRequest
    resets, _, userId, url = active

    class Projection:
        def invalidateCreditProjection(self, _userId):
            return True

    service = AdminCreditResetService(repository=resets, creditService=Projection())
    key = "bulk-" + str(uuid.uuid4())
    request = AdminCreditResetRequest(reason=REASON)
    with ThreadPoolExecutor(max_workers=2) as pool:
        views = list(pool.map(lambda _: service.resetAll(request, key, ADMIN), range(2)))
    assert views[0]["operationId"] == views[1]["operationId"]
    operationId = views[0]["operationId"]
    total = query(url, "select count(*) as total from public.admin_credit_reset_targets where operation_id=%s",
                  (operationId,))[0]["total"]
    finished = service.resetAll(request, key, ADMIN)
    assert finished["status"] == "COMPLETED" and finished["totalTargets"] == total
    audited = query(url, "select count(*) as total from public.admin_audit_log where action='credits.reset' "
                         "and details->>'operationId'=%s", (operationId,))[0]["total"]
    assert audited == total
    assert balance(url, userId)["remaining_tokens"] == 10000
    execute(url, 'insert into public."Users"("userId") values(%s)', ("late-" + str(uuid.uuid4()),))
    assert service.resetAll(request, key, ADMIN)["totalTargets"] == total
