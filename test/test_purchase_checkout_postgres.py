"""Owned selection CAS, financial bindings and retained erasure on guarded PostgreSQL."""
import importlib
import importlib.util
import os
import re
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
OWNERSHIP = 'payment ownership is immutable'
SLOT_FROZEN = 'payment slot identity or settlement is immutable'
CONTROL_FROZEN = 'payment control revision or frozen identity mismatch'
CONTROL_BINDING = 'payment owner, purpose, invoice or slot mismatch'
OPERATION_OWNER = 'payment owner, purpose or invoice mismatch'
OPERATION_BINDING = 'payment operation binding mismatch'
OPERATION_MONEY = 'payment operation money is immutable'


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


def seedAttempt(drafts, purpose='initial_purchase', domains=('banking',), bind=True):
    from unittest.mock import patch
    from api.services.billing.manualBillingContracts import CheckoutRequest
    _, billing, user, url = drafts
    # These SQL-level cases insert the control row themselves, with the purpose and slot under test.
    with patch('api.services.billing.purchaseCheckoutRepository.bindPurchaseControlLocked', lambda *args: None):
        intent = billing.reserveCheckout(CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid',
            {'domains': list(domains)}, expectedCatalogRevision=1))
    order = 'order-' + str(uuid4()) if bind else None
    if bind:
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


def insertRefund(url, user, intent, payment, reason='paid_after_close', kind='refund', dedupe=None,
                 amount=None, currency=None, erasure=None):
    return query(url, '''INSERT INTO payment_operations
        (user_id,attempt_id,invoice_id,provider_payment_id,kind,reason,amount,currency,dedupe_key,erasure_request_id)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
        (user, intent.attemptId, intent.invoiceId, payment, kind, reason, amount or intent.amount,
         currency or intent.currency, dedupe or 'payment:' + kind + ':' + payment, erasure), one=True)['id']


def assertRejected(message, action, label=None):
    """Proves the named guard fired, never an unrelated CHECK, FK or syntax error."""
    try:
        action()
    except psycopg2.Error as error:
        assert re.search(message, str(error)), (label, message, str(error))
        return
    pytest.fail(f'not rejected: {label}')


def assertStatementRejected(url, statement, args, message):
    assertRejected(message, lambda: query(url, statement, args), statement)


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
    assertStatementRejected(url, insert, (user, 'pro_term', 'open'), 'payment_slots_open_key_idx')
    query(url, insert, (user, 'topup', 'open'))
    query(url, insert, (user, 'pro_term', 'term-2'))
    def opened():
        with psycopg2.connect(url) as db, db.cursor(cursor_factory=RealDictCursor) as cursor:
            return openSlotLocked(cursor, user, 'pro_term', 'open')
    assert opened() == str(first) == opened()
    payment = 'pay-' + str(uuid4())
    query(url, 'UPDATE payment_slots SET settled_payment_id=%s,settled_at=clock_timestamp() WHERE slot_id=%s', (payment, first))
    for change in ("settled_payment_id='pay-other'", 'settled_at=clock_timestamp()', 'settled_payment_id=NULL,settled_at=NULL'):
        assertStatementRejected(url, f'UPDATE payment_slots SET {change} WHERE slot_id=%s', (first,), SLOT_FROZEN)
    second = opened()
    assert second != str(first) and opened() == second
    assertStatementRejected(url, 'UPDATE payment_slots SET settled_payment_id=%s,settled_at=clock_timestamp() WHERE slot_id=%s',
                            (payment, second), 'payment_slots_settled_payment_idx')
    assertStatementRejected(url, '''INSERT INTO payment_slots(user_id,kind,slot_key,settled_payment_id,settled_at)
        VALUES(%s,'topup','settled-at-birth',%s,clock_timestamp())''', (user, 'pay-' + str(uuid4())), 'slot must open unsettled')
    assertStatementRejected(url, insert, (user, 'renewal', 'open'), 'payment_slots_kind_check')
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
    rejected = [(dict(policy='manual'), 'payment_slot_controls_capture_policy_check'),
                (dict(policy='legacy'), 'payment_slot_controls_capture_policy_check'),
                (dict(purpose=otherPurpose, slot=slotFor(url, user, SLOT_KIND[otherPurpose])), CONTROL_BINDING),
                (dict(slot=slotFor(url, user, otherKind)), CONTROL_BINDING),
                (dict(slot=slotFor(url, stranger, SLOT_KIND[purpose])), CONTROL_BINDING)]
    for change, message in rejected:
        assertRejected(message, lambda: seedControl(url, user, intent, order, **{'purpose': purpose, 'policy': policy, **change}),
                       change)
    if purpose != 'initial_purchase':
        assertStatementRejected(url, '''INSERT INTO payment_slot_controls
            (attempt_id,slot_id,invoice_id,user_id,purpose,draft_revision,provider_order_id,capture_policy)
            VALUES(%s,%s,%s,%s,%s,1,%s,'automatic')''', (intent.attemptId, slotFor(url, user, SLOT_KIND[purpose]),
            intent.invoiceId, user, purpose, order), 'payment_slot_controls_draft_initial_check')
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
    for change, message in ((dict(kind='compensation'), 'payment_operations_kind_check'),
                            (dict(kind='capture'), 'payment_operations_kind_check'),
                            (dict(reason='expert_selection_changed'), 'payment_operations_reason_check'),
                            (dict(reason=None), 'null value in column "reason"'),
                            (dict(dedupe='purchase:refund:' + payment), 'payment_operations_dedupe_key_check'),
                            (dict(dedupe='payment:refund:other'), 'payment_operations_dedupe_key_check')):
        assertRejected(message, lambda: insertRefund(url, user, intent, payment, **change), change)
    op = insertRefund(url, user, intent, payment, 'duplicate')
    assert query(url, 'SELECT dedupe_key FROM payment_operations WHERE id=%s', (op,), one=True)['dedupe_key'] == 'payment:refund:' + payment
    assertRejected('duplicate key value violates unique constraint',
                   lambda: insertRefund(url, user, intent, payment, 'account_closed'), 'duplicate payment')
    for reason in REFUND_REASONS:
        insertRefund(url, user, intent, 'pay-' + str(uuid4()), reason)
    for change in ("reason='account_closed'", "dedupe_key='payment:refund:changed',provider_payment_id='changed'"):
        assertStatementRejected(url, f'UPDATE payment_operations SET {change} WHERE id=%s', (op,), OPERATION_MONEY)


@pytest.mark.parametrize('case,message', [
    ('amount_above_attempt', OPERATION_BINDING), ('currency_differs', OPERATION_BINDING),
    ('foreign_owner', OPERATION_OWNER), ('attempt_purpose_differs', OPERATION_OWNER),
    ('erasure_differs', OPERATION_BINDING), ('order_unbound', OPERATION_BINDING),
    ('control_in_settled_slot', CONTROL_BINDING)])
def test_refund_and_control_inserts_enforce_money_binding(drafts, case, message):
    _, _, user, url = drafts
    intent, order = seedAttempt(drafts, bind=case != 'order_unbound')
    payment = 'pay-' + str(uuid4())
    if case == 'control_in_settled_slot':
        slot = slotFor(url, user)
        query(url, 'UPDATE payment_slots SET settled_payment_id=%s,settled_at=clock_timestamp() WHERE slot_id=%s',
              ('pay-' + str(uuid4()), slot))
        assertRejected(message, lambda: seedControl(url, user, intent, order, slot=slot), case)
        assert query(url, 'SELECT * FROM payment_slot_controls WHERE attempt_id=%s', (intent.attemptId,)) == []
        seedControl(url, user, intent, order)  # The same control is valid in an unsettled slot.
        return
    seedControl(url, user, intent, order)
    owner, changes = user, {}
    if case == 'amount_above_attempt':
        changes = dict(amount=intent.amount + 1)
    elif case == 'currency_differs':
        changes = dict(currency='USD' if intent.currency == 'INR' else 'INR')
    elif case == 'foreign_owner':
        owner = 'stranger-' + str(uuid4())
        query(url, 'INSERT INTO "Users"("userId") VALUES(%s)', (owner,))
    elif case == 'attempt_purpose_differs':
        query(url, '''UPDATE billing_events SET metadata_json=jsonb_set(metadata_json,'{manualBilling,purpose}','"renewal"')
            WHERE id=%s''', (intent.attemptId,))
    elif case == 'erasure_differs':
        changes = dict(erasure=str(uuid4()))
    assertRejected(message, lambda: insertRefund(url, owner, intent, payment, **changes), case)
    assert query(url, 'SELECT * FROM payment_operations WHERE attempt_id=%s', (intent.attemptId,)) == []
    if case in ('amount_above_attempt', 'currency_differs', 'foreign_owner', 'erasure_differs'):
        insertRefund(url, user, intent, payment)  # Only the broken condition made the insert fail.


def test_only_a_started_unpaid_next_term_control_joins_the_open_term_slot(drafts):
    from datetime import datetime, timedelta, timezone
    _, _, user, url = drafts
    def termKey(delta):
        return (datetime.now(timezone.utc) + delta).replace(microsecond=0).isoformat()
    def renewalControl(slot):
        intent, order = seedAttempt(drafts, 'renewal')
        proveClosed(url, intent.attemptId)
        seedControl(url, user, intent, order, 'renewal', slot=slot)
        return intent.attemptId
    future = renewalControl(slotFor(url, user, 'pro_term', termKey(timedelta(hours=1))))
    paidSlot = slotFor(url, user, 'pro_term', termKey(-timedelta(minutes=2)))
    paidTerm = renewalControl(paidSlot)
    query(url, 'UPDATE payment_slots SET settled_payment_id=%s,settled_at=clock_timestamp() WHERE slot_id=%s',
          ('pay-' + str(uuid4()), paidSlot))
    startedSlot = slotFor(url, user, 'pro_term', termKey(-timedelta(minutes=1)))
    started = renewalControl(startedSlot)
    settledOpen = slotFor(url, user)
    query(url, 'UPDATE payment_slots SET settled_payment_id=%s,settled_at=clock_timestamp() WHERE slot_id=%s',
          ('pay-' + str(uuid4()), settledOpen))
    opened = slotFor(url, user)
    foreign = 'foreign-' + str(uuid4())
    query(url, 'INSERT INTO "Users"("userId") VALUES(%s)', (foreign,))
    move = 'UPDATE payment_slot_controls SET operation_revision=operation_revision+1,slot_id=%s WHERE attempt_id=%s'
    for target, attempt, label in ((opened, future, 'next term not started'), (opened, paidTerm, 'term already paid'),
                                   (slotFor(url, user, 'pro_term', 'elsewhere'), started, 'not the open term'),
                                   (slotFor(url, foreign), started, 'another owner'),
                                   (settledOpen, started, 'settled open term'),
                                   (slotFor(url, user, 'topup', 'topup'), started, 'another kind')):
        assertRejected(CONTROL_FROZEN, lambda: query(url, move, (target, attempt)), label)
    query(url, move, (opened, started))
    assert control(url, started)['slot_id'] == opened
    # Never back out of the open term.
    assertStatementRejected(url, move, (startedSlot, started), CONTROL_FROZEN)


def control(url, attemptId):
    return query(url, 'SELECT * FROM payment_slot_controls WHERE attempt_id=%s', (attemptId,), one=True)


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
    for change, message in (("kind='topup'", SLOT_FROZEN), ("slot_key='moved'", SLOT_FROZEN),
                            ('created_at=clock_timestamp()', SLOT_FROZEN), ("user_id='foreign'", OWNERSHIP)):
        assertStatementRejected(url, f'UPDATE payment_slots SET {change} WHERE slot_id=%s', (slot,), message)
    bump = 'operation_revision=operation_revision+1,'
    for change, message in ((f"{bump}slot_id='{elsewhere}'", CONTROL_FROZEN), (f"{bump}purpose='renewal'", CONTROL_FROZEN),
                            (f"{bump}capture_policy='account_default'", CONTROL_FROZEN),
                            (f'{bump}draft_revision=NULL', CONTROL_FROZEN), (f'{bump}draft_revision=2', CONTROL_FROZEN),
                            (f"{bump}provider_order_id='order-moved'", CONTROL_FROZEN),
                            (f'{bump}created_at=clock_timestamp()', CONTROL_FROZEN), (f"{bump}user_id='foreign'", OWNERSHIP),
                            (f"{bump}invoice_id='{other.invoiceId}'", OWNERSHIP),
                            ('recover_until=clock_timestamp()', CONTROL_FROZEN),
                            ('operation_revision=operation_revision+2', CONTROL_FROZEN),
                            (f"{bump}superseded_by='{other.attemptId}'", 'payment_slot_controls_superseded_reason_check')):
        assertStatementRejected(url, f'UPDATE payment_slot_controls SET {change} WHERE attempt_id=%s', (intent.attemptId,), message)
    query(url, f'UPDATE payment_slot_controls SET {bump}recover_until=clock_timestamp() WHERE attempt_id=%s', (intent.attemptId,))
    assertStatementRejected(url, f"""UPDATE payment_slot_controls SET {bump}superseded_by=%s,closed_at=clock_timestamp(),
        closed_reason='SUPERSEDED' WHERE attempt_id=%s""", (foreign.attemptId, intent.attemptId),
        'payment supersession owner or slot mismatch')
    query(url, f"""UPDATE payment_slot_controls SET {bump}superseded_by=%s,closed_at=clock_timestamp(),
        closed_reason='SUPERSEDED' WHERE attempt_id=%s""", (other.attemptId, intent.attemptId))
    for change in ('superseded_by=NULL', f"superseded_by='{foreign.attemptId}'", 'closed_at=clock_timestamp()',
                   "closed_reason='EXPIRED'", 'closed_at=NULL,closed_reason=NULL,superseded_by=NULL'):
        assertStatementRejected(url, f'UPDATE payment_slot_controls SET {bump}{change} WHERE attempt_id=%s',
                                (intent.attemptId,), CONTROL_FROZEN)
    for change, message in (('amount=1', OPERATION_MONEY), ("currency='USD'", OPERATION_MONEY), ("user_id='foreign'", OWNERSHIP),
                            (f"id='{uuid4()}'", OPERATION_MONEY), (f"attempt_id='{other.attemptId}'", OWNERSHIP),
                            (f"invoice_id='{other.invoiceId}'", OWNERSHIP), ("kind='capture'", OPERATION_MONEY),
                            ('created_at=clock_timestamp()', OPERATION_MONEY)):
        assertStatementRejected(url, f'UPDATE payment_operations SET {change} WHERE id=%s', (op,), message)


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
    intent = repo.reserveCheckout(repo.draftCheckoutRequest(user, 1, pricing, 'initial'))
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


@pytest.mark.parametrize('source', ['browser', 'webhook', 'recovery'])
def test_authorized_evidence_never_submits_capture(drafts, monkeypatch, source):
    from api.services.billing.manualPaymentService import ManualPaymentService
    from api.services.billing.purchaseCheckoutStatusService import PurchaseCheckoutStatusService
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    repo, provider, intent, _ = controlledPurchase(drafts, monkeypatch)
    payment = provider.authorize(intent)
    assert PurchaseCheckoutStatusService(repo, provider).refresh(intent.userId, intent.attemptId).state == 'confirming_earlier_payment'
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


def supersede(repo, provider, intent, domains=('banking',), key='replace'):
    """Pay a changed saved draft; the server replaces the earlier empty Order."""
    from api.services.billing.manualPaymentService import ManualPaymentService
    revision = repo.readEditor(intent.userId).draft.revision
    repo.saveDraft(intent.userId, revision, list(domains), 'year' if intent.billingMode == 'annual_prepaid' else 'month')
    pricing = {'userId': intent.userId, 'domains': list(domains), 'billingMode': intent.billingMode,
               'currency': intent.currency, 'catalogRevision': 1}
    replacement = ManualPaymentService.forProduction(provider, repo).createCheckout(
        repo.draftCheckoutRequest(intent.userId, revision + 1, pricing, key))
    assert replacement.attemptId != intent.attemptId
    return replacement


def test_late_authorization_records_money_without_reviving_superseded_purchase(drafts, monkeypatch):
    from api.services.billing.manualPaymentService import ManualPaymentService
    from api.services.billing.manualBillingRepository import _now
    repo, provider, intent, _ = controlledPurchase(drafts, monkeypatch)
    replacement = supersede(repo, provider, intent)
    payment = provider.authorize(intent)
    evidence = ManualPaymentService.buildPaymentEvidence(payment, attemptId=intent.attemptId, invoiceId=intent.invoiceId,
        userId=intent.userId, purpose='initial_purchase', observedAt=_now())
    ManualPaymentService.forProduction(provider, repo).finalizeCapturedPayment(evidence)
    attempt = repo.attemptById(intent.userId, intent.attemptId)
    assert attempt['payment_status'] == 'cancelled'
    assert not repo._json(attempt['metadata_json'])['manualBilling'].get('closureReconciledAt')
    assert repo.attemptById(intent.userId, replacement.attemptId)['payment_status'] == 'pending_provider_ack'
    assert not provider.captureCalls and not repo.getCoverageSnapshot(intent.userId).accessAllowed


def test_stale_draft_and_changed_catalog_reserve_nothing(drafts, monkeypatch):
    from api.services.billing.manualPaymentService import ManualPaymentService
    repo, provider, first, _ = controlledPurchase(drafts, monkeypatch)
    repo.saveDraft(first.userId, 1, ['telecom'], 'year')
    pricing = {'userId': first.userId, 'domains': ['telecom'], 'billingMode': 'annual_prepaid', 'currency': 'INR', 'catalogRevision': 999}
    before = query(drafts[3], 'SELECT to_jsonb(i) AS row FROM "Invoices" i WHERE "userId"=%s ORDER BY id', (first.userId,))
    service = ManualPaymentService.forProduction(provider, repo)
    with pytest.raises(ValueError, match='PURCHASE_DRAFT_STALE'):
        service.createCheckout(repo.draftCheckoutRequest(first.userId, 1, pricing, 'new'))
    with pytest.raises(ValueError, match='PRICING_CHANGED'):
        service.createCheckout(repo.draftCheckoutRequest(first.userId, 2, pricing, 'new'))
    # A replacement that fails pricing rolls back: the earlier attempt stays live.
    assert query(drafts[3], 'SELECT to_jsonb(i) AS row FROM "Invoices" i WHERE "userId"=%s ORDER BY id', (first.userId,)) == before
    assert repo.attemptById(first.userId, first.attemptId)['payment_status'] == 'pending_provider_ack'
    assert len(provider.orderCalls) == 1


def test_same_closed_legacy_key_returns_old_identity_and_no_gateway_call(drafts, monkeypatch):
    from api.services.billing.manualBillingContracts import CheckoutRequest
    from api.services.billing.manualPaymentService import ManualPaymentService
    from test.purchaseProviderFixtures import SyntheticPurchaseProvider
    repo, billing, user, _ = drafts
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED')
    request = CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid', {'domains': ['banking']}, 'legacy', expectedCatalogRevision=1)
    provider = SyntheticPurchaseProvider()
    intent = ManualPaymentService.forProduction(provider, billing).createCheckout(request)
    assert provider.orderCalls[0]['payment']['capture'] == 'automatic'
    ManualPaymentService.forProduction(provider, billing).createCheckout(CheckoutRequest(
        user, 'initial_purchase', 'monthly_prepaid', {'domains': ['telecom']}, 'legacy-changed', expectedCatalogRevision=1))
    assert len(provider.orderCalls) == 2
    replay = ManualPaymentService.forProduction(provider, billing).createCheckout(request)
    assert replay.attemptId == intent.attemptId and replay.state == 'cancelled'
    control = repo.getControl(user, intent.attemptId)
    assert control['capture_policy'] == 'automatic' and control['draft_revision'] is None
    assert control['closed_reason'] == 'SUPERSEDED'
    assert query(drafts[3], 'SELECT kind,slot_key FROM payment_slots WHERE slot_id=%s', (control['slot_id'],), one=True) == {
        'kind': 'pro_term', 'slot_key': 'open'}
    assert len(provider.orderCalls) == 2


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


