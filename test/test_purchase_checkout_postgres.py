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
            for recovery in (ROOT / 'supabase/migrations').glob('*_recover_cancelled_purchase.sql'):
                cursor.execute(recovery.read_text())
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
    repo.recordPurchaseWorkerHeartbeat()
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


STORAGE_TABLES = ['subscription_purchase_drafts', 'payment_slots', 'payment_slot_controls', 'payment_operations']
SLOT_KIND = {'initial_purchase': 'pro_term', 'renewal': 'pro_term', 'expert_addition': 'expert_addition', 'topup': 'topup'}
REFUND_REASONS = ('duplicate', 'paid_after_close', 'not_deliverable', 'account_closed')
VERBS = ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER')


@pytest.mark.parametrize('table', STORAGE_TABLES + ['private.payment_recovery_runtime'])
def test_storage_rls_and_privileges(checkoutDb, table):
    relation = table if '.' in table else 'public.' + table
    def allowed(role, verb):
        return query(checkoutDb, 'SELECT has_table_privilege(%s,%s,%s) AS allowed', (role, relation, verb), one=True)['allowed']
    for role in ('anon', 'authenticated'):
        assert not any(allowed(role, verb) for verb in VERBS), role
    assert query(checkoutDb, '''SELECT count(*) AS grants FROM pg_class c, aclexplode(c.relacl) a
        WHERE c.oid=%s::regclass AND a.grantee=0''', (relation,), one=True)['grants'] == 0
    expected = {'SELECT', 'INSERT', 'UPDATE'} | ({'DELETE'} if table == 'subscription_purchase_drafts' else set())
    assert {verb for verb in VERBS if allowed('service_role', verb)} == expected
    assert query(checkoutDb, 'SELECT relrowsecurity AS enabled FROM pg_class WHERE oid=%s::regclass',
                 (relation,), one=True)['enabled']
    for retired in ('public.subscription_purchase_' + 'checkout_controls', 'public.subscription_purchase_' + 'operations',
                    'private.purchase_recovery_runtime'):
        assert query(checkoutDb, 'SELECT to_regclass(%s) AS relation', (retired,), one=True)['relation'] is None


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


def seedAttempt(drafts, purpose='initial_purchase', domains=('banking',)):
    from api.services.billing.manualBillingContracts import CheckoutRequest
    _, billing, user, url = drafts
    intent = billing.reserveCheckout(CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid',
        {'domains': list(domains)}, expectedCatalogRevision=1))
    order = 'order-' + str(uuid4())
    billing.bindProviderOrder(intent.attemptId, {'id': order})
    if purpose != 'initial_purchase':
        query(url, '''UPDATE billing_events SET metadata_json=jsonb_set(metadata_json,'{manualBilling,purpose}',to_jsonb(%s::text))
            WHERE id=%s''', (purpose, intent.attemptId))
    return intent, order


def proveClosed(url, attemptId):
    # Reconciled closure lets the same owner reserve another synthetic attempt.
    query(url, """UPDATE billing_events SET payment_status='cancelled',metadata_json=jsonb_set(metadata_json,'{manualBilling}',
        metadata_json->'manualBilling'||jsonb_build_object('closedAt',clock_timestamp(),'closureReconciledAt',clock_timestamp()))
        WHERE id=%s""", (attemptId,))


def slotFor(url, user, kind='pro_term', key='open'):
    query(url, '''INSERT INTO payment_slots(user_id,kind,slot_key) VALUES(%s,%s,%s)
        ON CONFLICT(user_id,kind,slot_key) WHERE settled_payment_id IS NULL DO NOTHING''', (user, kind, key))
    return query(url, '''SELECT slot_id FROM payment_slots WHERE user_id=%s AND kind=%s AND slot_key=%s
        AND settled_payment_id IS NULL''', (user, kind, key), one=True)['slot_id']


def seedControl(url, user, intent, order, purpose='initial_purchase', slot=None, policy='automatic'):
    slot = slot or slotFor(url, user, SLOT_KIND[purpose])
    query(url, '''INSERT INTO payment_slot_controls
        (attempt_id,slot_id,invoice_id,user_id,purpose,draft_revision,provider_order_id,capture_policy)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''', (intent.attemptId, slot, intent.invoiceId, user, purpose,
        1 if purpose == 'initial_purchase' else None, order, policy))
    return slot


def insertRefund(url, user, intent, payment, reason='paid_after_close', kind='refund', dedupe=None):
    return query(url, '''INSERT INTO payment_operations
        (user_id,attempt_id,invoice_id,provider_payment_id,kind,reason,amount,currency,dedupe_key)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (user, intent.attemptId, intent.invoiceId, payment, kind, reason, intent.amount, intent.currency,
         dedupe or 'payment:' + kind + ':' + payment), one=True)['id']


def seedOperation(drafts, kind='refund'):
    _, _, user, url = drafts
    intent, order = seedAttempt(drafts)
    seedControl(url, user, intent, order)
    return intent, insertRefund(url, user, intent, 'pay-' + str(uuid4()), kind=kind)


def test_slot_unique_while_unsettled(drafts):
    from psycopg2.extras import RealDictCursor
    from api.services.billing.purchaseCheckoutRepository import openSlotLocked
    _, _, user, url = drafts
    insert = 'INSERT INTO payment_slots(user_id,kind,slot_key) VALUES(%s,%s,%s) RETURNING slot_id'
    first = query(url, insert, (user, 'pro_term', 'open'), one=True)['slot_id']
    with pytest.raises(psycopg2.IntegrityError):
        query(url, insert, (user, 'pro_term', 'open'))
    query(url, insert, (user, 'topup', 'open'))
    query(url, insert, (user, 'pro_term', 'term-2'))
    def opened():
        with psycopg2.connect(url) as db, db.cursor(cursor_factory=RealDictCursor) as cursor:
            return openSlotLocked(cursor, user, 'pro_term', 'open')
    assert opened() == str(first) == opened()
    payment = 'pay-' + str(uuid4())
    query(url, 'UPDATE payment_slots SET settled_payment_id=%s,settled_at=clock_timestamp() WHERE slot_id=%s', (payment, first))
    for change in ("settled_payment_id='pay-other'", 'settled_at=clock_timestamp()', 'settled_payment_id=NULL,settled_at=NULL'):
        with pytest.raises(psycopg2.Error):
            query(url, f'UPDATE payment_slots SET {change} WHERE slot_id=%s', (first,))
    second = opened()
    assert second != str(first) and opened() == second
    with pytest.raises(psycopg2.IntegrityError):
        query(url, 'UPDATE payment_slots SET settled_payment_id=%s,settled_at=clock_timestamp() WHERE slot_id=%s', (payment, second))
    with pytest.raises(psycopg2.Error):
        query(url, '''INSERT INTO payment_slots(user_id,kind,slot_key,settled_payment_id,settled_at)
            VALUES(%s,'topup','settled-at-birth',%s,clock_timestamp())''', (user, 'pay-' + str(uuid4())))
    with pytest.raises(psycopg2.IntegrityError):
        query(url, insert, (user, 'renewal', 'open'))
    with pytest.raises(psycopg2.Error, match='slot must open unsettled'):
        query(url, """INSERT INTO payment_slots(user_id,kind,slot_key,erasure_request_id)
            VALUES(NULL,'topup','detached-at-birth',%s)""", (str(uuid4()),))


@pytest.mark.parametrize('policy', ['automatic', 'account_default'])
@pytest.mark.parametrize('purpose', ['initial_purchase', 'renewal', 'expert_addition', 'topup'])
def test_controls_accept_every_purpose_with_automatic_policy_only(drafts, purpose, policy):
    _, _, user, url = drafts
    intent, order = seedAttempt(drafts, purpose)
    stranger = 'stranger-' + str(uuid4())
    query(url, 'INSERT INTO "Users"("userId") VALUES(%s)', (stranger,))
    otherKind = 'topup' if SLOT_KIND[purpose] != 'topup' else 'pro_term'
    otherPurpose = 'topup' if purpose != 'topup' else 'renewal'
    rejected = [dict(policy='manual'), dict(policy='legacy'),
                dict(purpose=otherPurpose, slot=slotFor(url, user, SLOT_KIND[otherPurpose])),
                dict(slot=slotFor(url, user, otherKind)), dict(slot=slotFor(url, stranger, SLOT_KIND[purpose]))]
    for change in rejected:
        with pytest.raises(psycopg2.Error):
            seedControl(url, user, intent, order, **{'purpose': purpose, 'policy': policy, **change})
    if purpose != 'initial_purchase':
        with pytest.raises(psycopg2.Error):
            query(url, '''INSERT INTO payment_slot_controls
                (attempt_id,slot_id,invoice_id,user_id,purpose,draft_revision,provider_order_id,capture_policy)
                VALUES(%s,%s,%s,%s,%s,1,%s,'automatic')''', (intent.attemptId, slotFor(url, user, SLOT_KIND[purpose]),
                intent.invoiceId, user, purpose, order))
    with pytest.raises(psycopg2.Error, match='retained payment authority required'):
        query(url, """INSERT INTO payment_slot_controls
            (attempt_id,slot_id,invoice_id,user_id,purpose,provider_order_id,capture_policy,erasure_request_id)
            VALUES(%s,%s,%s,NULL,%s,%s,'automatic',%s)""", (intent.attemptId, slotFor(url, user, SLOT_KIND[purpose]),
            intent.invoiceId, purpose, order, str(uuid4())))
    slot = seedControl(url, user, intent, order, purpose, policy=policy)
    row = query(url, 'SELECT slot_id,purpose,capture_policy FROM payment_slot_controls WHERE attempt_id=%s',
                (intent.attemptId,), one=True)
    assert row == {'slot_id': slot, 'purpose': purpose, 'capture_policy': policy}
    insertRefund(url, user, intent, 'pay-' + str(uuid4()))


def test_refund_operation_reason_and_dedupe(drafts):
    _, _, user, url = drafts
    intent, order = seedAttempt(drafts)
    seedControl(url, user, intent, order)
    payment = 'pay-' + str(uuid4())
    for change in (dict(kind='compensation'), dict(kind='capture'), dict(reason='expert_selection_changed'),
                   dict(reason=None), dict(dedupe='purchase:refund:' + payment), dict(dedupe='payment:refund:other')):
        with pytest.raises(psycopg2.Error):
            insertRefund(url, user, intent, payment, **change)
    op = insertRefund(url, user, intent, payment, 'duplicate')
    assert query(url, 'SELECT dedupe_key FROM payment_operations WHERE id=%s', (op,), one=True)['dedupe_key'] == 'payment:refund:' + payment
    with pytest.raises(psycopg2.IntegrityError):
        insertRefund(url, user, intent, payment, 'account_closed')
    for reason in REFUND_REASONS:
        insertRefund(url, user, intent, 'pay-' + str(uuid4()), reason)
    for change in ("reason='account_closed'", "dedupe_key='payment:refund:changed',provider_payment_id='changed'"):
        with pytest.raises(psycopg2.Error):
            query(url, f'UPDATE payment_operations SET {change} WHERE id=%s', (op,))


def rejected(url, statement, args):
    try:
        query(url, statement, args)
    except psycopg2.Error:
        return True
    return False


def test_trigger_blocks_delete_truncate_and_identity_changes(drafts):
    _, _, user, url = drafts
    other, otherOrder = seedAttempt(drafts, domains=('telecom',))
    proveClosed(url, other.attemptId)
    intent, order = seedAttempt(drafts)
    proveClosed(url, intent.attemptId)
    foreign, foreignOrder = seedAttempt(drafts, 'topup', domains=('supplychain',))
    slot = seedControl(url, user, intent, order)
    seedControl(url, user, other, otherOrder, slot=slot)
    seedControl(url, user, foreign, foreignOrder, 'topup')
    op = insertRefund(url, user, intent, 'pay-' + str(uuid4()))
    for table, key, value in (('payment_slots', 'slot_id', slot), ('payment_slot_controls', 'attempt_id', intent.attemptId),
                              ('payment_operations', 'id', op)):
        with pytest.raises(psycopg2.Error, match='cannot be deleted'):
            query(url, f'DELETE FROM {table} WHERE {key}=%s', (value,))
    with pytest.raises(psycopg2.Error, match='cannot be deleted'):
        query(url, 'TRUNCATE payment_operations')
    with pytest.raises(psycopg2.Error, match='cannot be deleted'):
        query(url, 'TRUNCATE payment_operations,payment_slot_controls,payment_slots')
    elsewhere = slotFor(url, user, 'pro_term', 'elsewhere')
    for change in ("kind='topup'", "slot_key='moved'", 'created_at=clock_timestamp()', "user_id='foreign'"):
        assert rejected(url, f'UPDATE payment_slots SET {change} WHERE slot_id=%s', (slot,)), change
    bump = 'operation_revision=operation_revision+1,'
    for change in (f"{bump}slot_id='{elsewhere}'", f"{bump}purpose='renewal'", f"{bump}capture_policy='account_default'",
                   f'{bump}draft_revision=NULL', f'{bump}draft_revision=2', f"{bump}provider_order_id='order-moved'",
                   f'{bump}created_at=clock_timestamp()', f"{bump}user_id='foreign'", f"{bump}invoice_id='{other.invoiceId}'",
                   'recover_until=clock_timestamp()', 'operation_revision=operation_revision+2',
                   f"{bump}superseded_by='{other.attemptId}'"):
        assert rejected(url, f'UPDATE payment_slot_controls SET {change} WHERE attempt_id=%s', (intent.attemptId,)), change
    query(url, f'UPDATE payment_slot_controls SET {bump}recover_until=clock_timestamp() WHERE attempt_id=%s', (intent.attemptId,))
    with pytest.raises(psycopg2.Error):
        query(url, f"""UPDATE payment_slot_controls SET {bump}superseded_by=%s,closed_at=clock_timestamp(),
            closed_reason='SUPERSEDED' WHERE attempt_id=%s""", (foreign.attemptId, intent.attemptId))
    query(url, f"""UPDATE payment_slot_controls SET {bump}superseded_by=%s,closed_at=clock_timestamp(),
        closed_reason='SUPERSEDED',cancellation_requested_at=clock_timestamp() WHERE attempt_id=%s""",
        (other.attemptId, intent.attemptId))
    for change in ('superseded_by=NULL', f"superseded_by='{foreign.attemptId}'", 'closed_at=clock_timestamp()',
                   "closed_reason='EXPIRED'", 'closed_at=NULL,closed_reason=NULL,superseded_by=NULL',
                   'cancellation_requested_at=clock_timestamp()'):
        assert rejected(url, f'UPDATE payment_slot_controls SET {bump}{change} WHERE attempt_id=%s', (intent.attemptId,)), change
    for change in ('amount=1', "currency='USD'", "user_id='foreign'", f"id='{uuid4()}'", f"attempt_id='{other.attemptId}'",
                   f"invoice_id='{other.invoiceId}'", "kind='capture'", 'created_at=clock_timestamp()'):
        assert rejected(url, f'UPDATE payment_operations SET {change} WHERE id=%s', (op,)), change


def test_operation_binding_dedupe_and_terminal_state_are_enforced(drafts):
    repo, _, user, url = drafts
    intent, op = seedOperation(drafts)
    assert repo.getControl(user, intent.attemptId)['draft_revision'] == 1
    assert repo.hasUnresolvedPurchaseOperations(user)
    with pytest.raises(ValueError, match='OWNED_PAYMENT_ATTEMPT_MISSING'):
        repo.getControl('foreign', intent.attemptId)
    with pytest.raises(psycopg2.IntegrityError):
        query(url, '''INSERT INTO payment_operations
            (user_id,attempt_id,invoice_id,provider_payment_id,kind,reason,amount,currency,dedupe_key)
            SELECT user_id,attempt_id,invoice_id,provider_payment_id,kind,reason,amount,currency,dedupe_key
            FROM payment_operations WHERE id=%s''', (op,))
    for column, value in [('amount', 1), ('currency', 'USD'), ('user_id', 'foreign'), ('id', str(uuid4()))]:
        with pytest.raises(psycopg2.Error):
            query(url, f'UPDATE payment_operations SET {column}=%s WHERE id=%s', (value, op))
    with pytest.raises(psycopg2.Error):
        query(url, "UPDATE payment_operations SET state='succeeded' WHERE id=%s", (op,))
    query(url, "UPDATE payment_operations SET state='submission_started' WHERE id=%s", (op,))
    query(url, "UPDATE payment_operations SET state='succeeded' WHERE id=%s", (op,))
    assert not repo.hasUnresolvedPurchaseOperations(user)
    with pytest.raises(psycopg2.Error):
        query(url, "UPDATE payment_operations SET state='queued' WHERE id=%s", (op,))


@pytest.mark.parametrize('kind', ['refund'])
def test_unresolved_operations_block_erasure_atomically_then_resolved_originals_are_retained(drafts, kind):
    from nubrix.triggers.tasks.userErasureTask import UserErasureTask
    from test.test_user_erasure_task import FakeExternalCleanup, FakeAudit
    repo, _, user, url = drafts
    repo.saveDraft(user, 0, ['telecom'], 'year')
    intent, op = seedOperation(drafts, kind)
    original = query(url, 'SELECT to_jsonb(o) AS row FROM payment_operations o WHERE id=%s', (op,), one=True)['row']
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
    assert query(url, 'SELECT to_jsonb(o) AS row FROM payment_operations o WHERE id=%s', (op,), one=True)['row'] == original
    query(url, "UPDATE payment_operations SET state='cancelled' WHERE id=%s", (op,))
    # Retry the same durable erasure request, never create a second one.
    query(url, "UPDATE user_erasure_requests SET next_retry_at=now()-interval '1 second',lease_expires_at=NULL WHERE id=%s", (request,))
    outcome = UserErasureTask(repository, FakeExternalCleanup(), FakeAudit()).execute(request)
    assert outcome['status'] == 'COMPLETED', failures
    assert repository.verifyDatabaseErasure(user) == {}
    slot = query(url, 'SELECT slot_id FROM payment_slot_controls WHERE attempt_id=%s', (intent.attemptId,), one=True)['slot_id']
    for table, key, identity in [('payment_operations', 'id', op), ('payment_slot_controls', 'attempt_id', intent.attemptId),
                                 ('payment_slots', 'slot_id', slot)]:
        retained = query(url, 'SELECT original_record FROM private.retained_user_records WHERE source_table=%s AND source_id=%s',
                         (table, identity), one=True)
        assert retained['original_record']['user_id'] == user
        detached = query(url, f'SELECT user_id,erasure_request_id FROM {table} WHERE {key}=%s', (identity,), one=True)
        assert detached['user_id'] is None and str(detached['erasure_request_id']) == request
    counts = repository.getRequest(request)['retention_manifest']['retainedRecordCounts']
    assert counts['payment_operations'] == 1 and counts['payment_slot_controls'] == 1 and counts['payment_slots'] == 1


def test_due_operation_scan_uses_keyset_and_kind(drafts):
    repo, _, user, _ = drafts
    _, operation = seedOperation(drafts)
    rows = repo.listDueOperations('refund', None, 100)
    assert str(operation) in [str(row['id']) for row in rows]
    assert all(row['kind'] == 'refund' for row in rows)
    assert all(str(row['id']) > str(operation) for row in repo.listDueOperations('refund', str(operation), 100))
    with pytest.raises(ValueError):
        repo.listDueOperations('compensation', None, 100)
    with pytest.raises(ValueError):
        repo.listDueOperations('foreign', None, 100)


@pytest.mark.parametrize('missing', ['amount', 'provider_order_id'])
def test_operation_requires_existing_order_and_amount_authority(drafts, missing):
    _, _, user, url = drafts
    intent, op = seedOperation(drafts)
    query(url, f'UPDATE billing_events SET {missing}=NULL WHERE id=%s', (intent.attemptId,))
    payment = 'pay-' + str(uuid4())
    with pytest.raises(psycopg2.Error):
        query(url, '''INSERT INTO payment_operations
            (user_id,attempt_id,invoice_id,provider_payment_id,kind,reason,amount,currency,dedupe_key)
            SELECT user_id,attempt_id,invoice_id,%s,kind,reason,amount,currency,%s
            FROM payment_operations WHERE id=%s''', (payment, 'payment:refund:' + payment, op))


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


def controlledPurchase(drafts, monkeypatch, domains=('banking', 'manufacturing'), interval='month', currency='INR'):
    from test.purchaseProviderFixtures import SyntheticPurchaseProvider
    name = 'api.services.billing.purchaseCheckoutService'
    assert importlib.util.find_spec(name), 'Guarded purchase service must exist'
    actions = importlib.import_module(name)
    monkeypatch.setattr(actions, 'purchaseActionsReady', lambda: True)
    repo, _, user, _ = drafts
    repo.saveDraft(user, 0, list(domains), interval)
    pricing = {'userId': user, 'domains': list(domains), 'billingMode': 'monthly_prepaid' if interval == 'month' else 'annual_prepaid',
               'currency': currency, 'catalogRevision': 1}
    intent = repo.reserveDraftCheckout(user, 1, pricing, 'initial')
    provider = SyntheticPurchaseProvider()
    from api.services.billing.manualPaymentService import ManualPaymentService
    intent = ManualPaymentService.forProduction(provider, repo).openReservedCheckout(intent)
    return repo, provider, intent, actions.PurchaseCheckoutService(repo, provider)


def authorization(repo, provider, intent):
    from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
    from api.services.billing.manualBillingRepository import _now
    payment = provider.authorize(intent)
    evidence = VerifiedPaymentEvidence(intent.attemptId, intent.invoiceId, intent.userId, intent.razorpayOrderId,
        payment['id'], 'initial_purchase', intent.currency, 'authorized', 'server_observation', intent.amount,
        _now(), None, None, False)
    repo.finalizeCapturedPayment(evidence)
    return evidence


def test_cancel_idempotency_and_stale_revision_are_not_new_mutations(drafts, monkeypatch):
    repo, provider, intent, service = controlledPurchase(drafts, monkeypatch)
    first = service.cancel(intent.userId, intent.attemptId, 1, 'cancel-key')
    replay = service.cancel(intent.userId, intent.attemptId, 1, 'cancel-key')
    assert replay.checkout.operationRevision == first.checkout.operationRevision
    with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
        service.cancel(intent.userId, intent.attemptId, 2, 'cancel-key')
    with pytest.raises(ValueError, match='PURCHASE_CHECKOUT_STALE'):
        service.cancel(intent.userId, intent.attemptId, 1, 'another-key')


@pytest.mark.parametrize('currency', ['INR', 'USD'])
@pytest.mark.parametrize('interval', ['month', 'year'])
@pytest.mark.parametrize('original,changed', [(('banking',), ['banking', 'telecom']), (('banking', 'telecom'), ['banking'])])
def test_explicit_cancel_then_latest_draft_replaces_frozen_selection(drafts, monkeypatch, currency, interval, original, changed):
    repo, provider, intent, service = controlledPurchase(drafts, monkeypatch, original, interval, currency)
    assert provider.orderCalls[0]['payment']['capture'] == 'automatic'
    old = query(drafts[3], 'SELECT amount,total_amount,currency FROM "Invoices" WHERE id=%s', (intent.invoiceId,), one=True)
    repo.saveDraft(intent.userId, 1, changed, interval)
    pricing = {'userId': intent.userId, 'domains': changed, 'billingMode': intent.billingMode, 'currency': currency, 'catalogRevision': 1}
    with pytest.raises(ValueError, match='PURCHASE_REPLACEMENT_CONFIRMATION_REQUIRED'):
        repo.reserveDraftCheckout(intent.userId, 2, pricing, 'replace')
    service.cancel(intent.userId, intent.attemptId, 1, 'cancel-old')
    new = repo.reserveDraftCheckout(intent.userId, 2, pricing, 'replace')
    assert new.attemptId != intent.attemptId and new.snapshot['domains'] == sorted(changed)
    assert repo.getControl(intent.userId, new.attemptId)['draft_revision'] == 2
    replacement, original = repo.getControl(intent.userId, new.attemptId), repo.getControl(intent.userId, intent.attemptId)
    assert replacement['slot_id'] == original['slot_id'] and replacement['capture_policy'] == 'automatic'
    assert query(drafts[3], 'SELECT kind,slot_key,settled_payment_id FROM payment_slots WHERE slot_id=%s',
                 (replacement['slot_id'],), one=True) == {'kind': 'pro_term', 'slot_key': 'open', 'settled_payment_id': None}
    assert query(drafts[3], 'SELECT amount,total_amount,currency FROM "Invoices" WHERE id=%s', (intent.invoiceId,), one=True) == old
    assert len(provider.orderCalls) == 1


def test_legacy_purchase_cannot_bypass_unresolved_control_when_gate_is_off(drafts, monkeypatch):
    from api.services.billing.manualBillingContracts import CheckoutRequest
    repo, provider, intent, service = controlledPurchase(drafts, monkeypatch)
    authorization(repo, provider, intent)
    service.cancel(intent.userId, intent.attemptId, 1, 'cancel')
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED')
    with pytest.raises(ValueError, match='PURCHASE_FUNDS_PENDING'):
        drafts[1].reserveCheckout(CheckoutRequest(intent.userId, 'initial_purchase', intent.billingMode,
            {'domains': ['telecom']}, 'old-client', expectedCatalogRevision=1))


@pytest.mark.parametrize('source', ['browser', 'webhook', 'recovery'])
def test_authorized_evidence_never_submits_capture(drafts, monkeypatch, source):
    from api.services.billing.manualPaymentService import ManualPaymentService
    from api.services.billing.purchaseCheckoutStatusService import PurchaseCheckoutStatusService
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    repo, provider, intent, _ = controlledPurchase(drafts, monkeypatch)
    payment = provider.authorize(intent)
    assert PurchaseCheckoutStatusService(repo, provider).refresh(intent.userId, intent.attemptId).state == 'awaiting_capture'
    assert not provider.captureCalls
    if source == 'browser':
        from api.services.billing.manualBillingRepository import _now
        def observe():
            evidence = ManualPaymentService.buildPaymentEvidence(provider.payment.fetch(payment['id']),
                attemptId=intent.attemptId, invoiceId=intent.invoiceId, userId=intent.userId,
                purpose='initial_purchase', observedAt=_now())
            return ManualPaymentService.forProduction(provider, repo).finalizeCapturedPayment(evidence)
    elif source == 'recovery':
        def observe():
            return ManualBillingRecoveryService(repo, provider).recoverAttempt(repo.attemptById(intent.userId, intent.attemptId))
    else:
        from api.services.subscriptions.subscriptionService import SubscriptionService
        from api.services.webhookService import WebhookService
        subscription = SubscriptionService.__new__(SubscriptionService)
        subscription.razorpayClient = provider
        monkeypatch.setattr('api.services.subscriptions.subscriptionService.subscriptionService', subscription)
        monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository', lambda: repo)
        def observe():
            return WebhookService.__new__(WebhookService)._handlePaymentAuthorized({'payload': {'payment': {'entity': provider.payment.fetch(payment['id'])}}})
    first = observe()
    if source == 'browser':
        assert first.state == 'awaiting_capture' and not first.finalized
    elif source == 'webhook':
        assert first is None  # Signed-event handlers acknowledge via the webhook processor.
    else:
        assert first == 0
    observe()
    assert provider.captureCalls == []
    assert not repo.getCoverageSnapshot(intent.userId).accessAllowed
    assert repo.attemptById(intent.userId, intent.attemptId)['payment_status'] == 'authorized'


def test_late_authorization_records_money_without_reviving_cancelled_purchase(drafts, monkeypatch):
    from api.services.billing.manualPaymentService import ManualPaymentService
    from api.services.billing.manualBillingRepository import _now
    repo, provider, intent, service = controlledPurchase(drafts, monkeypatch)
    service.cancel(intent.userId, intent.attemptId, 1, 'cancel-unpaid')
    payment = provider.authorize(intent)
    evidence = ManualPaymentService.buildPaymentEvidence(payment, attemptId=intent.attemptId, invoiceId=intent.invoiceId,
        userId=intent.userId, purpose='initial_purchase', observedAt=_now())
    ManualPaymentService.forProduction(provider, repo).finalizeCapturedPayment(evidence)
    attempt = repo.attemptById(intent.userId, intent.attemptId)
    assert attempt['payment_status'] == 'authorized'
    assert not repo._json(attempt['metadata_json'])['manualBilling'].get('closureReconciledAt')
    assert not provider.captureCalls and not repo.getCoverageSnapshot(intent.userId).accessAllowed


def test_cancelled_order_acknowledgement_is_recovered_without_new_order(drafts, monkeypatch):
    from api.services.billing.manualPaymentService import ManualPaymentService
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    repo, provider, first, service = controlledPurchase(drafts, monkeypatch)
    service.cancel(first.userId, first.attemptId, 1, 'close-first')
    repo.saveDraft(first.userId, 1, ['telecom'], 'month')
    pricing = {'userId': first.userId, 'domains': ['telecom'], 'billingMode': 'monthly_prepaid', 'currency': 'INR', 'catalogRevision': 1}
    intent = repo.reserveDraftCheckout(first.userId, 2, pricing, 'second')
    def lostAcknowledgement(payload, **options):
        provider.createOrder(payload)
        raise TimeoutError('synthetic order accepted before lost reply')
    provider.order.create = lostAcknowledgement
    with pytest.raises(RuntimeError, match='ORDER_ACK_UNKNOWN'):
        ManualPaymentService.forProduction(provider, repo).openReservedCheckout(intent)
    service.cancel(first.userId, intent.attemptId, 0, 'close-second')
    ManualBillingRecoveryService(repo, provider).recoverAttempt(repo.attemptById(first.userId, intent.attemptId))
    recovered = repo.attemptById(first.userId, intent.attemptId)
    assert recovered['provider_order_id'] and repo._json(recovered['metadata_json'])['manualBilling']['closedAt']
    assert repo.getControl(first.userId, intent.attemptId)['cancellation_accepted_at'] is not None
    assert len(provider.orderCalls) == 2 and not provider.captureCalls


def test_stale_draft_and_changed_catalog_reserve_nothing(drafts, monkeypatch):
    repo, provider, first, service = controlledPurchase(drafts, monkeypatch)
    service.cancel(first.userId, first.attemptId, 1, 'cancel')
    repo.saveDraft(first.userId, 1, ['telecom'], 'year')
    pricing = {'userId': first.userId, 'domains': ['telecom'], 'billingMode': 'annual_prepaid', 'currency': 'INR', 'catalogRevision': 999}
    before = query(drafts[3], 'SELECT id FROM "Invoices" WHERE "userId"=%s', (first.userId,))
    with pytest.raises(ValueError, match='PURCHASE_DRAFT_STALE'):
        repo.reserveDraftCheckout(first.userId, 1, pricing, 'new')
    with pytest.raises(ValueError, match='PRICING_CHANGED'):
        repo.reserveDraftCheckout(first.userId, 2, pricing, 'new')
    assert query(drafts[3], 'SELECT id FROM "Invoices" WHERE "userId"=%s', (first.userId,)) == before
    assert len(provider.orderCalls) == 1


def test_same_closed_legacy_key_returns_old_identity_and_no_gateway_call(drafts, monkeypatch):
    from api.services.billing.manualBillingContracts import CheckoutRequest
    from api.services.billing.manualPaymentService import ManualPaymentService
    from test.purchaseProviderFixtures import SyntheticPurchaseProvider
    from api.services.billing.purchaseCheckoutService import PurchaseCheckoutService
    repo, billing, user, _ = drafts
    request = CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid', {'domains': ['banking']}, 'legacy', expectedCatalogRevision=1)
    provider = SyntheticPurchaseProvider()
    intent = ManualPaymentService.forProduction(provider, billing).createCheckout(request)
    assert provider.orderCalls[0]['payment']['capture'] == 'automatic'
    monkeypatch.setattr('api.services.billing.purchaseCheckoutService.purchaseActionsReady', lambda: True)
    PurchaseCheckoutService(repo, provider).cancel(user, intent.attemptId, 0, 'cancel-legacy')
    replay = ManualPaymentService.forProduction(provider, billing).createCheckout(request)
    assert replay.attemptId == intent.attemptId and replay.state == 'cancelled'
    control = repo.getControl(user, intent.attemptId)
    assert control['capture_policy'] == 'account_default' and control['draft_revision'] is None
    assert query(drafts[3], 'SELECT kind,slot_key FROM payment_slots WHERE slot_id=%s', (control['slot_id'],), one=True) == {
        'kind': 'pro_term', 'slot_key': 'open'}
    assert len(provider.orderCalls) == 1


def test_submit_http_verifies_signed_pricing_and_keeps_contact_out_of_storage(drafts, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from jose import jwt
    from api.commons import verifyToken
    from api.services.billing.pricingContext import issuePricingContext
    from test.purchaseProviderFixtures import SyntheticPurchaseProvider
    import api.routers.subscriptions as routes
    repo, _, user, url = drafts
    repo.saveDraft(user, 0, ['banking'], 'year')
    provider = SyntheticPurchaseProvider()
    monkeypatch.setattr('api.services.billing.purchaseCheckoutService.purchaseActionsReady', lambda: True)
    monkeypatch.setenv('PRICING_PROXY_SECRET', 'synthetic-test-pricing')
    monkeypatch.setattr('api.services.billing.purchaseCheckoutRepository.getPurchaseCheckoutRepository', lambda: repo)
    monkeypatch.setattr(routes.subscriptionService, 'razorpayClient', provider)
    token = jwt.encode({'userId': user, 'email': 'synthetic@example.test'}, os.environ['SECRET_KEY'], algorithm='HS256')
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[verifyToken] = lambda: token
    client = TestClient(app)
    payload = {'draftRevision': 1, 'expectedCatalogRevision': 1, 'contact': '+919999999999',
        'pricingContext': issuePricingContext('foreign-owner', 'USD', 'hosting', catalogRevision=1)}
    denied = client.post('/purchase-draft/checkout', json=payload, headers={'Idempotency-Key': 'ui-pay'})
    assert denied.status_code == 409 and denied.json()['errorCode'] == 'PRICING_CONTEXT_INVALID'
    assert not provider.orderCalls
    payload['pricingContext'] = issuePricingContext(user, 'USD', 'hosting', catalogRevision=1)
    response = client.post('/purchase-draft/checkout', json=payload, headers={'Idempotency-Key': 'ui-pay'})
    assert response.status_code == 200, response.text
    assert response.json()['checkout']['currency'] == 'USD'
    assert response.headers['cache-control'] == 'private, no-store'
    assert provider.orderCalls[0]['payment']['capture'] == 'automatic'
    frozen = query(url, 'SELECT metadata_json FROM "Invoices" WHERE "userId"=%s', (user,), one=True)['metadata_json']
    assert '+919999999999' not in str(frozen)
    assert len(provider.orderCalls) == 1


def test_newer_unknown_observation_invalidates_old_unpaid_cancellation_proof(drafts, monkeypatch):
    from dataclasses import replace
    from datetime import timedelta
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    repo, provider, intent, _ = controlledPurchase(drafts, monkeypatch)
    old = ManualBillingRecoveryService(repo, provider).inspectAttempt(repo.attemptById(intent.userId, intent.attemptId))
    repo.recordProviderObservation(intent.attemptId, old)
    repo.recordProviderObservation(intent.attemptId, replace(old, outcome='unknown', complete=False, observedAt=old.observedAt+timedelta(seconds=1)))
    repo.acceptCancellation(intent.userId, intent.attemptId, 1, 'cancel-stale-read', old)
    frozen = repo._json(repo.attemptById(intent.userId, intent.attemptId)['metadata_json'])['manualBilling']
    assert frozen['purchaseCancellationAcceptedAt']
    assert not frozen.get('closureReconciledAt')