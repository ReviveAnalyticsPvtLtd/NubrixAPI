"""Owned selection CAS, financial bindings and retained erasure on guarded PostgreSQL."""
import importlib
import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extras import Json

from test.subscriptionRuntimeFixtures import postgres, runtimeDb, query  # noqa:F401
from test.test_subscription_erasure_postgres import erasureDb, startErasure, runErasure  # noqa:F401

pytestmark = pytest.mark.skipif(os.environ.get('RUN_MANUAL_BILLING_INTEGRATION') != '1',
    reason='Guarded disposable PostgreSQL opt-in; skip is UNVERIFIED')
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def checkoutDb(erasureDb):
    paths = list((ROOT / 'supabase/migrations').glob('*_add_editable_purchase_checkout.sql'))
    assert len(paths) == 1, 'Additive editable checkout migration must exist'
    for _ in range(2):
        with psycopg2.connect(erasureDb) as db, db.cursor() as cursor:
            cursor.execute(paths[0].read_text())
    return erasureDb


@pytest.fixture
def drafts(checkoutDb, monkeypatch):
    name = 'api.services.billing.purchaseCheckoutRepository'
    assert importlib.util.find_spec(name), 'Owned draft repository must exist'
    monkeypatch.setenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED', 'true')
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE', 'v2')
    user = 'draft-' + str(uuid4())
    query(checkoutDb, 'INSERT INTO "Users"("userId") VALUES(%s)', (user,))
    from api.services.billing.manualBillingRepository import ManualBillingRepository
    billing = ManualBillingRepository(lambda: psycopg2.connect(checkoutDb))
    billing.ensureCanonicalSubscription(user)
    repo = importlib.import_module(name).PurchaseCheckoutRepository(lambda: psycopg2.connect(checkoutDb))
    return repo, billing, user, checkoutDb


def test_empty_read_is_unpersisted_and_save_reload_increments_revision(drafts):
    repo, _, user, url = drafts
    first = repo.readEditor(user)
    assert first.draft.revision == 0 and first.draft.domains == ()
    assert first.capabilities.mayEditDraft and not first.capabilities.maySubmitLatestDraft
    assert query(url, 'SELECT * FROM subscription_purchase_drafts WHERE user_id=%s', (user,)) == []
    saved = repo.saveDraft(user, 0, ['banking', 'manufacturing'], 'year')
    assert saved.revision == 1 and saved.billingInterval == 'year'
    assert repo.readEditor(user).draft == saved
    second = repo.saveDraft(user, 1, ['banking'], 'month')
    assert second.revision == 2 and second.domains == ('banking',)
    assert query(url, 'SELECT * FROM "Invoices" WHERE "userId"=%s', (user,)) == []


def test_concurrent_draft_save_has_one_winner_and_no_financial_objects(drafts):
    repo, _, user, url = drafts
    def save(domain):
        try:
            return repo.saveDraft(user, 0, [domain], 'month').revision
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ['banking', 'telecom']))
    assert sorted(map(str, results)) == ['1', 'PURCHASE_DRAFT_STALE']
    assert repo.readEditor(user).draft.revision == 1
    assert query(url, 'SELECT * FROM "Invoices" WHERE "userId"=%s', (user,)) == []


@pytest.mark.parametrize('outcome', ['authorized', 'unknown'])
def test_unresolved_original_money_does_not_prevent_saving_selection(drafts, outcome):
    from api.services.billing.manualBillingContracts import CheckoutRequest
    repo, billing, user, url = drafts
    intent = billing.reserveCheckout(CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid',
        {'domains': ['banking', 'manufacturing']}, expectedCatalogRevision=1))
    query(url, "UPDATE billing_events SET metadata_json=jsonb_set(metadata_json,'{manualBilling,providerObservation}',%s) WHERE id=%s",
          (Json({'outcome': outcome, 'complete': outcome != 'unknown'}), intent.attemptId))
    initial = repo.readEditor(user)
    assert initial.draft.revision == 0 and initial.draft.domains == ('banking', 'manufacturing')
    before = query(url, 'SELECT to_jsonb(i) AS row FROM "Invoices" i WHERE id=%s', (intent.invoiceId,), one=True)
    repo.saveDraft(user, 0, ['banking'], 'year')
    assert repo.readEditor(user).draft.domains == ('banking',)
    assert query(url, 'SELECT to_jsonb(i) AS row FROM "Invoices" i WHERE id=%s', (intent.invoiceId,), one=True) == before
    assert not repo.readEditor(user).capabilities.maySubmitLatestDraft


@pytest.mark.parametrize('restriction', ['banned', 'erasure', 'paid'])
def test_restricted_or_current_paid_accounts_cannot_write_drafts(drafts, restriction):
    repo, billing, user, url = drafts
    if restriction == 'banned':
        admin = query(url, '''INSERT INTO admin_users(email,name,password_hash)
            VALUES(%s,'Synthetic','not-a-real-password') RETURNING id''', (str(uuid4())+'@example.test',), one=True)['id']
        query(url, '''UPDATE "Users" SET "isBanned"=true,"bannedAt"=now(),"bannedBy"=%s,"banReason"='synthetic'
            WHERE "userId"=%s''', (admin, user))
    elif restriction == 'erasure':
        query(url, 'UPDATE subscriptions SET erasure_pending=true WHERE user_id=%s', (user,))
    else:
        from api.services.billing.manualBillingContracts import CheckoutRequest
        from test.subscriptionRuntimeFixtures import capture
        capture(billing, billing.reserveCheckout(CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid',
            {'domains': ['banking']}, expectedCatalogRevision=1)))
    assert not repo.readEditor(user).capabilities.mayEditDraft
    with pytest.raises(ValueError, match='PURCHASE_DRAFT_NOT_ALLOWED'):
        repo.saveDraft(user, 0, ['banking'], 'month')
    assert query(url, 'SELECT * FROM subscription_purchase_drafts WHERE user_id=%s', (user,)) == []


@pytest.mark.parametrize('role', ['anon', 'authenticated'])
@pytest.mark.parametrize('table', ['subscription_purchase_drafts', 'subscription_purchase_checkout_controls', 'subscription_purchase_operations'])
def test_direct_data_api_access_is_revoked(checkoutDb, role, table):
    for verb in ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE'):
        assert not query(checkoutDb, 'SELECT has_table_privilege(%s,%s,%s) AS allowed',
                         (role, 'public.' + table, verb), one=True)['allowed']
    assert query(checkoutDb, 'SELECT relrowsecurity AS enabled FROM pg_class WHERE oid=%s::regclass',
                 ('public.' + table,), one=True)['enabled']


@pytest.mark.parametrize('domains,interval', [([], 'month'), (['banking', 'banking'], 'month'),
    (['foreign'], 'month'), ({'banking': True}, 'month'), (['banking'], 'annual')])
def test_sql_rejects_invalid_draft_shape(checkoutDb, domains, interval):
    user = 'shape-' + str(uuid4())
    query(checkoutDb, 'INSERT INTO "Users"("userId") VALUES(%s)', (user,))
    with pytest.raises(psycopg2.IntegrityError):
        query(checkoutDb, 'INSERT INTO subscription_purchase_drafts(user_id,revision,domains,billing_interval) VALUES(%s,1,%s,%s)',
              (user, Json(domains), interval))
    assert query(checkoutDb, 'SELECT * FROM subscription_purchase_drafts WHERE user_id=%s', (user,)) == []


def test_foreign_control_is_not_found(drafts):
    repo, _, user, _ = drafts
    with pytest.raises(ValueError, match='OWNED_PAYMENT_ATTEMPT_MISSING'):
        repo.getControl(user, str(uuid4()))


def test_draft_is_removed_by_complete_erasure(drafts):
    repo, _, user, url = drafts
    repo.saveDraft(user, 0, ['banking'], 'month')
    runErasure(url, user)
    assert query(url, 'SELECT * FROM subscription_purchase_drafts WHERE user_id=%s', (user,)) == []


def seedOperation(drafts, kind='capture'):
    from api.services.billing.manualBillingContracts import CheckoutRequest
    repo, billing, user, url = drafts
    intent = billing.reserveCheckout(CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid',
        {'domains': ['banking']}, expectedCatalogRevision=1))
    order, payment = 'order-' + str(uuid4()), 'pay-' + str(uuid4())
    billing.bindProviderOrder(intent.attemptId, {'id': order})
    query(url, '''INSERT INTO subscription_purchase_checkout_controls
        (attempt_id,invoice_id,user_id,draft_revision,provider_order_id)
        VALUES(%s,%s,%s,1,%s)''', (intent.attemptId, intent.invoiceId, user, order))
    operation = query(url, '''INSERT INTO subscription_purchase_operations
        (user_id,attempt_id,invoice_id,provider_payment_id,kind,amount,currency,dedupe_key)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (user, intent.attemptId, intent.invoiceId, payment, kind, intent.amount, intent.currency,
         'purchase:' + kind + ':' + payment), one=True)['id']
    return intent, operation


def test_operation_binding_dedupe_and_terminal_state_are_enforced(drafts):
    repo, _, user, url = drafts
    intent, op = seedOperation(drafts)
    assert repo.getControl(user, intent.attemptId)['draft_revision'] == 1
    assert repo.hasUnresolvedPurchaseOperations(user)
    with pytest.raises(ValueError, match='OWNED_PAYMENT_ATTEMPT_MISSING'):
        repo.getControl('foreign', intent.attemptId)
    with pytest.raises(psycopg2.IntegrityError):
        query(url, '''INSERT INTO subscription_purchase_operations
            (user_id,attempt_id,invoice_id,provider_payment_id,kind,amount,currency,dedupe_key)
            SELECT user_id,attempt_id,invoice_id,provider_payment_id,kind,amount,currency,dedupe_key
            FROM subscription_purchase_operations WHERE id=%s''', (op,))
    for column, value in [('amount', 1), ('currency', 'USD'), ('user_id', 'foreign'), ('id', str(uuid4()))]:
        with pytest.raises(psycopg2.Error):
            query(url, f'UPDATE subscription_purchase_operations SET {column}=%s WHERE id=%s', (value, op))
    with pytest.raises(psycopg2.Error):
        query(url, "UPDATE subscription_purchase_operations SET state='succeeded' WHERE id=%s", (op,))
    query(url, "UPDATE subscription_purchase_operations SET state='submission_started' WHERE id=%s", (op,))
    query(url, "UPDATE subscription_purchase_operations SET state='succeeded' WHERE id=%s", (op,))
    assert not repo.hasUnresolvedPurchaseOperations(user)
    with pytest.raises(psycopg2.Error):
        query(url, "UPDATE subscription_purchase_operations SET state='queued' WHERE id=%s", (op,))


@pytest.mark.parametrize('kind', ['capture', 'compensation'])
def test_unresolved_operations_block_erasure_atomically_then_resolved_originals_are_retained(drafts, kind):
    from nubrix.triggers.tasks.userErasureTask import UserErasureTask
    from test.test_user_erasure_task import FakeExternalCleanup, FakeAudit
    repo, _, user, url = drafts
    repo.saveDraft(user, 0, ['telecom'], 'year')
    intent, op = seedOperation(drafts, kind)
    original = query(url, 'SELECT to_jsonb(o) AS row FROM subscription_purchase_operations o WHERE id=%s', (op,), one=True)['row']
    repository, request = startErasure(url, user)
    failures = []
    delete = repository.deleteDatabaseData
    def tracedDelete(*args, **kwargs):
        try:
            return delete(*args, **kwargs)
        except Exception as exc:
            failures.append(repr(exc))
            raise
    repository.deleteDatabaseData = tracedDelete
    result = UserErasureTask(repository, FakeExternalCleanup(), FakeAudit()).execute(request)
    assert result['status'] != 'COMPLETED'
    assert query(url, 'SELECT * FROM subscription_purchase_drafts WHERE user_id=%s', (user,))
    assert query(url, 'SELECT * FROM "Users" WHERE "userId"=%s', (user,))
    assert query(url, 'SELECT to_jsonb(o) AS row FROM subscription_purchase_operations o WHERE id=%s', (op,), one=True)['row'] == original
    query(url, "UPDATE subscription_purchase_operations SET state='cancelled' WHERE id=%s", (op,))
    # Retry the same durable erasure request, never create a second one.
    query(url, "UPDATE user_erasure_requests SET next_retry_at=now()-interval '1 second',lease_expires_at=NULL WHERE id=%s", (request,))
    outcome = UserErasureTask(repository, FakeExternalCleanup(), FakeAudit()).execute(request)
    assert outcome['status'] == 'COMPLETED', failures
    assert repository.verifyDatabaseErasure(user) == {}
    for table, identity in [('subscription_purchase_operations', op), ('subscription_purchase_checkout_controls', intent.attemptId)]:
        retained = query(url, 'SELECT original_record FROM private.retained_user_records WHERE source_table=%s AND source_id=%s',
                         (table, identity), one=True)
        assert retained['original_record']['user_id'] == user
    assert repository.getRequest(request)['retention_manifest']['retainedRecordCounts']['subscription_purchase_operations'] == 1


def test_due_operation_scan_uses_keyset_and_kind(drafts):
    repo, _, user, _ = drafts
    _, operation = seedOperation(drafts)
    rows = repo.listDueOperations('capture', None, 100)
    assert str(operation) in [str(row['id']) for row in rows]
    assert all(row['kind'] == 'capture' for row in rows)
    assert all(str(row['id']) > str(operation) for row in repo.listDueOperations('capture', str(operation), 100))
    with pytest.raises(ValueError):
        repo.listDueOperations('foreign', None, 100)


@pytest.mark.parametrize('missing', ['amount', 'provider_order_id'])
def test_operation_requires_existing_order_and_amount_authority(drafts, missing):
    _, _, user, url = drafts
    intent, op = seedOperation(drafts)
    query(url, f'UPDATE billing_events SET {missing}=NULL WHERE id=%s', (intent.attemptId,))
    payment = 'pay-' + str(uuid4())
    with pytest.raises(psycopg2.Error):
        query(url, '''INSERT INTO subscription_purchase_operations
            (user_id,attempt_id,invoice_id,provider_payment_id,kind,amount,currency,dedupe_key)
            SELECT user_id,attempt_id,invoice_id,%s,kind,amount,currency,%s
            FROM subscription_purchase_operations WHERE id=%s''', (payment, 'purchase:capture:' + payment, op))


def test_enabled_draft_http_cas_returns_current_owned_editor(drafts, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from jose import jwt
    from api.commons import verifyToken
    import api.routers.subscriptions as routes
    repo, _, user, url = drafts
    monkeypatch.setattr('api.services.billing.purchaseCheckoutRepository.getPurchaseCheckoutRepository', lambda: repo)
    token = jwt.encode({'userId': user, 'email': 'synthetic@example.test'}, os.environ['SECRET_KEY'], algorithm='HS256')
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[verifyToken] = lambda: token
    client = TestClient(app)
    request = {'expectedRevision': 0, 'domains': ['banking', 'manufacturing'], 'billingInterval': 'year'}
    def save(_):
        return client.put('/purchase-draft', json=request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, range(2)))
    assert sorted(result.status_code for result in results) == [200, 409]
    stale = next(result for result in results if result.status_code == 409)
    assert stale.json()['errorCode'] == 'PURCHASE_DRAFT_STALE'
    assert stale.json()['editor']['draft']['revision'] == 1
    assert stale.json()['editor']['userId'] == user
    assert client.get('/purchase-draft').json()['draft']['domains'] == ['banking', 'manufacturing']
    assert all(result.headers['cache-control'] == 'private, no-store' for result in results)
    assert query(url, 'SELECT * FROM "Invoices" WHERE "userId"=%s', (user,)) == []


def test_failed_draft_transaction_rolls_back_previous_selection(drafts):
    repo, _, user, url = drafts
    repo.saveDraft(user, 0, ['banking'], 'month')
    with psycopg2.connect(url) as db, db.cursor() as cursor:
        cursor.execute('''CREATE FUNCTION public.reject_synthetic_draft() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic write failure'; END $$;
            CREATE TRIGGER reject_synthetic_draft BEFORE UPDATE ON public.subscription_purchase_drafts
            FOR EACH ROW EXECUTE FUNCTION public.reject_synthetic_draft()''')
    try:
        with pytest.raises(psycopg2.Error):
            repo.saveDraft(user, 1, ['telecom'], 'year')
    finally:
        with psycopg2.connect(url) as db, db.cursor() as cursor:
            cursor.execute('DROP TRIGGER reject_synthetic_draft ON public.subscription_purchase_drafts; DROP FUNCTION public.reject_synthetic_draft()')
    assert repo.readEditor(user).draft.domains == ('banking',)
    assert repo.readEditor(user).draft.revision == 1


def test_missing_schema_fails_enabled_reads_and_preserves_disabled_path(drafts, monkeypatch):
    from api.services.billing.purchaseDraftService import PurchaseDraftService
    repo, _, user, url = drafts
    query(url, 'ALTER TABLE subscription_purchase_drafts RENAME TO synthetic_hidden_drafts')
    try:
        with pytest.raises(RuntimeError, match='PURCHASE_STORAGE_UNAVAILABLE'):
            PurchaseDraftService(repo).get(user)
        monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED')
        assert not PurchaseDraftService(repo).get(user).enabled
    finally:
        query(url, 'ALTER TABLE synthetic_hidden_drafts RENAME TO subscription_purchase_drafts')
