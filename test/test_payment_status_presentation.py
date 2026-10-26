"""Customer payment status (spec 4.7): one checkout, the owner's refunds and one support reference."""
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from test.test_manual_billing_runtime import database, USER, NOW, SUB, sqlTransaction

OTHER = 'status-other-user'


@pytest.fixture
def statusDb(database):
    repository, path = database
    with sqlTransaction(path) as db:
        db.execute('INSERT INTO "Users"("userId") VALUES(?)', (OTHER,))
        db.execute('''CREATE TABLE payment_operations (
            id TEXT PRIMARY KEY, user_id TEXT, attempt_id TEXT, invoice_id TEXT, provider_payment_id TEXT,
            kind TEXT, reason TEXT, state TEXT, amount INTEGER, currency TEXT, error_code TEXT,
            escalation_reference TEXT, created_at TEXT, updated_at TEXT)''')
    return repository, path


def present(repository, user=USER, now=NOW):
    from api.services.billing.paymentStatusPresentation import presentPaymentStatus
    def operation(connection):
        with connection.cursor() as cursor:
            return presentPaymentStatus(cursor, user, now)
    return repository._run(operation)


def attempt(path, *, status='pending_provider_ack', attemptedAt=NOW - timedelta(minutes=5), purpose='initial_purchase',
            domains=('banking',), mode='monthly_prepaid', amount=472000, currency='INR', closed=False,
            observation=None, window=45, user=USER):
    attemptId = str(uuid.uuid4())
    frozen = {'purpose': purpose, 'domains': list(domains), 'billingMode': mode, 'captureWindowMinutes': window,
              'expiresAt': (attemptedAt + timedelta(minutes=30)).isoformat()}
    if closed:
        frozen.update(closedAt=(attemptedAt + timedelta(minutes=1)).isoformat(), closedReason='SUPERSEDED')
    if observation:
        frozen['providerObservation'] = observation
    with sqlTransaction(path) as db:
        db.execute('''INSERT INTO billing_events(id,user_id,subscription_id,invoice_id,event_category,event_type,
            event_status,payment_status,provider_order_id,amount,currency,metadata_json,attempted_at,created_at)
            VALUES(?,?,?,?,'payment_attempt','payment.checkout',?,?,?,?,?,?,?,?)''',
            (attemptId, user, SUB, 'invoice-' + attemptId, status, status, 'order-' + attemptId, amount, currency,
             json.dumps({'manualBilling': frozen}), attemptedAt.isoformat(), attemptedAt.isoformat()))
    return attemptId


def authorizedAt(started):
    return {'outcome': 'authorized', 'complete': True, 'hasPayments': True,
            'checkedAt': NOW.isoformat(), 'latestPaymentCreatedAt': started.isoformat()}


def operation(path, *, state='queued', reason='duplicate', startedAt=NOW, amount=472000, currency='INR',
              user=USER, error=None, escalated=False, attemptId=None):
    operationId = str(uuid.uuid4())
    attemptId = attemptId or str(uuid.uuid4())
    with sqlTransaction(path) as db:
        db.execute('''INSERT INTO payment_operations(id,user_id,attempt_id,invoice_id,provider_payment_id,kind,reason,
            state,amount,currency,error_code,escalation_reference,created_at,updated_at)
            VALUES(?,?,?,?,?,'refund',?,?,?,?,?,?,?,?)''',
            (operationId, user, attemptId, str(uuid.uuid4()), 'pay_' + operationId[:8], reason, state,
             amount, currency, error, 'refund:' + operationId if escalated else None,
             startedAt.isoformat(), startedAt.isoformat()))
    return operationId


def capture(path, *, status='REQUIRES_RECONCILIATION', reason='FROZEN_SNAPSHOT_INCOMPLETE',
            rulesFacts='CAPTURE_RULES', user=USER, occurredAt=NOW):
    captureId = str(uuid.uuid4())
    metadata = {'rulesFacts': rulesFacts} if rulesFacts else {}
    with sqlTransaction(path) as db:
        db.execute('''INSERT INTO billing_events(id,user_id,subscription_id,invoice_id,event_category,event_type,
            event_status,provider_payment_id,amount,currency,failure_reason,metadata_json,occurred_at,created_at)
            VALUES(?,?,?,?,'reconciliation','payment.capture',?,?,472000,'INR',?,?,?,?)''',
            (captureId, user, SUB, str(uuid.uuid4()), status, 'pay_' + captureId[:8], reason, json.dumps(metadata),
             occurredAt.isoformat(), occurredAt.isoformat()))
    return captureId


def test_status_confirming_ready_completed_states(statusDb):
    repository, path = statusDb
    assert present(repository) == {'checkout': None, 'refunds': [], 'paymentAttention': None}

    ready = attempt(path, domains=('banking', 'telecom'))
    status = present(repository)
    assert status['checkout'] == {'state': 'ready', 'attemptId': ready, 'amount': 472000, 'currency': 'INR',
                                  'selectionLabel': 'Pro monthly with Banking and Telecom'}
    assert status['refunds'] == [] and status['paymentAttention'] is None

    # A created-only payment (an unanswered UPI collect) never holds Pay.
    processing = attempt(path, attemptedAt=NOW - timedelta(minutes=2),
                         observation={'outcome': 'processing', 'complete': True, 'hasPayments': True})
    assert present(repository)['checkout']['state'] == 'ready'
    assert present(repository)['checkout']['attemptId'] == processing

    # A superseded attempt still holding an in-window authorized payment is what Pay waits on, even
    # though a newer open attempt exists: status and Pay agree.
    held = attempt(path, status='cancelled', attemptedAt=NOW - timedelta(minutes=20), closed=True,
                   observation=authorizedAt(NOW - timedelta(minutes=10)), domains=('manufacturing',),
                   mode='annual_prepaid', amount=5000, currency='USD')
    assert present(repository)['checkout'] == {'state': 'confirming_earlier_payment', 'attemptId': held,
        'amount': 5000, 'currency': 'USD', 'selectionLabel': 'Pro annual with Manufacturing'}

    # Past created_at + the attempt's own window the authorization is dead: the newest attempt is ready.
    assert present(repository, now=NOW + timedelta(minutes=35, seconds=1))['checkout']['attemptId'] == processing
    assert present(repository, now=NOW + timedelta(minutes=35, seconds=1))['checkout']['state'] == 'ready'
    assert present(repository, now=NOW + timedelta(minutes=34))['checkout']['state'] == 'confirming_earlier_payment'

    completed = attempt(path, status='captured', attemptedAt=NOW - timedelta(minutes=1), purpose='topup',
                        domains=(), amount=99900)
    with sqlTransaction(path) as db:  # the earlier authorization is no longer observed
        db.execute("UPDATE billing_events SET metadata_json=json_remove(metadata_json,'$.manualBilling.providerObservation') WHERE id=?", (held,))
    assert present(repository)['checkout'] == {'state': 'completed', 'attemptId': completed, 'amount': 99900,
        'currency': 'INR', 'selectionLabel': 'a credit top-up'}
    assert ready not in json.dumps(present(repository))  # only the newest attempt is shown


def test_status_never_shows_another_owners_checkout(statusDb):
    repository, path = statusDb
    attempt(path, user=OTHER, status='authorized', observation=authorizedAt(NOW - timedelta(minutes=1)))
    operation(path, user=OTHER)
    capture(path, user=OTHER)
    assert present(repository) == {'checkout': None, 'refunds': [], 'paymentAttention': None}
    assert present(repository, OTHER)['checkout']['state'] == 'confirming_earlier_payment'


def test_refund_states_and_no_ids_in_refunds(statusDb):
    repository, path = statusDb
    a, b, c, d = (str(uuid.uuid4()) for _ in range(4))
    returned = operation(path, state='succeeded', reason='duplicate', startedAt=NOW - timedelta(days=20), attemptId=a)
    moving = operation(path, state='submitted', reason='paid_after_close', startedAt=NOW - timedelta(days=1),
                       amount=5000, currency='USD', attemptId=b)
    late = operation(path, state='queued', reason='not_deliverable', startedAt=NOW - timedelta(days=15), attemptId=c)
    operation(path, state='cancelled', startedAt=NOW - timedelta(days=2))
    failed = operation(path, state='needs_attention', reason='account_closed', error='PAYMENT_REFUND_FAILED',
                       startedAt=NOW - timedelta(hours=1), attemptId=d)
    refunds = present(repository)['refunds']
    # attemptId binds API calls only (the poller's own attempt); it is never displayed.
    assert refunds == [
        {'attemptId': d, 'amount': 472000, 'currency': 'INR', 'reason': 'account_closed', 'state': 'on_its_way',
         'startedAt': (NOW - timedelta(hours=1)).isoformat()},
        {'attemptId': b, 'amount': 5000, 'currency': 'USD', 'reason': 'paid_after_close', 'state': 'on_its_way',
         'startedAt': (NOW - timedelta(days=1)).isoformat()},
        {'attemptId': c, 'amount': 472000, 'currency': 'INR', 'reason': 'not_deliverable', 'state': 'delayed',
         'startedAt': (NOW - timedelta(days=15)).isoformat()},
        {'attemptId': a, 'amount': 472000, 'currency': 'INR', 'reason': 'duplicate', 'state': 'returned',
         'startedAt': (NOW - timedelta(days=20)).isoformat()},
    ]
    with sqlite3.connect(path) as db:
        identities = {value for row in db.execute('SELECT id,invoice_id,provider_payment_id FROM payment_operations')
                      for value in row}
    assert {returned, moving, late, failed} <= identities
    text = json.dumps(present(repository))
    assert not [identity for identity in identities if identity in text]
    # A failed send the worker retries on its own within a minute is a refund on its way, not attention.
    assert present(repository)['paymentAttention'] is None


def test_refund_binds_its_own_attempt_while_another_is_shown(statusDb):
    repository, path = statusDb
    paid = attempt(path, status='cancelled', closed=True, attemptedAt=NOW - timedelta(minutes=20))
    shown = attempt(path, attemptedAt=NOW - timedelta(minutes=5), domains=('telecom',))
    operation(path, state='queued', reason='paid_after_close', attemptId=paid)
    status = present(repository)
    assert status['checkout']['attemptId'] == shown and status['checkout']['state'] == 'ready'
    assert [refund['attemptId'] for refund in status['refunds']] == [paid]


@pytest.mark.parametrize('started,boundary', [
    # Monday noon: ten working days later is the Monday two weeks on.
    (datetime(2026, 10, 5, 12, tzinfo=timezone.utc), datetime(2026, 10, 19, 12, tzinfo=timezone.utc)),
    # Friday noon: the weekend does not count.
    (datetime(2026, 10, 9, 12, tzinfo=timezone.utc), datetime(2026, 10, 23, 12, tzinfo=timezone.utc)),
    # Saturday: the first working day is Monday.
    (datetime(2026, 10, 10, 9, tzinfo=timezone.utc), datetime(2026, 10, 23, 9, tzinfo=timezone.utc)),
])
def test_refund_is_delayed_only_after_ten_working_days(started, boundary):
    from api.services.billing.paymentStatusPresentation import refundState
    assert refundState('submitted', started, boundary) == 'on_its_way'
    assert refundState('queued', started, boundary + timedelta(microseconds=1)) == 'delayed'
    assert refundState('succeeded', started, boundary + timedelta(days=30)) == 'returned'


def test_attention_reference_format(statusDb):
    from api.services.billing.paymentStatusPresentation import supportReference
    pattern = re.compile(r'^NBX-[0-9A-HJKMNP-TV-Z]{4}$')
    samples = [str(uuid.uuid4()) for _ in range(200)]
    assert all(pattern.match(supportReference(sample)) for sample in samples)
    assert supportReference(samples[0]) == supportReference(samples[0])
    assert len({supportReference(sample) for sample in samples}) > 150

    repository, path = statusDb
    capture(path, rulesFacts='PRE_CUTOVER', reason='CAPTURE_OUTSIDE_WINDOW')  # historical: untouched
    capture(path, status='FINALIZED', reason=None)
    # A failed send not yet escalated is retried automatically by the worker within a minute.
    operation(path, state='needs_attention', error='PAYMENT_REFUND_FAILED', startedAt=NOW - timedelta(minutes=1))
    assert present(repository)['paymentAttention'] is None
    # Once unresolved past the escalation threshold the worker escalates it while it keeps retrying:
    # the escalated case shows its reference at once.
    overdue = operation(path, state='needs_attention', error='PAYMENT_REFUND_FAILED',
                        startedAt=NOW - timedelta(minutes=31), escalated=True)
    assert present(repository)['paymentAttention'] == {'reference': supportReference(overdue)}
    with sqlTransaction(path) as db:
        db.execute("UPDATE payment_operations SET state='succeeded' WHERE id=?", (overdue,))
    assert present(repository)['paymentAttention'] is None

    for index, reason in enumerate(('FROZEN_SNAPSHOT_INCOMPLETE', 'PAYMENT_SLOT_UNAVAILABLE', 'PAYMENT_EVIDENCE_MISMATCH')):
        captureId = capture(path, reason=reason, occurredAt=NOW + timedelta(seconds=index + 1))
        assert present(repository)['paymentAttention'] == {'reference': supportReference(captureId)}
    missing = capture(path, rulesFacts='RULES_FACTS_MISSING', reason='CAPTURE_TIMING_UNVERIFIED',
                      occurredAt=NOW + timedelta(minutes=1))
    assert present(repository)['paymentAttention'] == {'reference': supportReference(missing)}
    escalated = operation(path, state='submitted', startedAt=NOW + timedelta(minutes=2), escalated=True)
    assert present(repository)['paymentAttention'] == {'reference': supportReference(escalated)}
    invalid = operation(path, state='needs_attention', error='PAYMENT_REFUND_HISTORY_INVALID',
                        startedAt=NOW + timedelta(minutes=3))
    status = present(repository)
    assert status['paymentAttention'] == {'reference': supportReference(invalid)}
    # The single place a reference appears.
    assert json.dumps(status).count('NBX-') == 1


# -- HTTP ---------------------------------------------------------------------

from test.test_manual_checkout_http import checkout_database, checkout_client  # noqa: E402


@pytest.fixture
def statusClient(checkout_client, monkeypatch):
    import sys
    from types import SimpleNamespace
    from unittest.mock import Mock
    client, provider, path = checkout_client
    monkeypatch.setitem(sys.modules, 'utils.logger', SimpleNamespace(logger=Mock()))
    provider.order.payments = lambda order, params: {
        'items': provider.paymentsByOrder.get(order, [])[params['skip']:params['skip'] + params['count']]}
    return client, provider, path


def test_status_is_owner_scoped_and_no_store(statusClient):
    client, provider, path = statusClient
    with sqlTransaction(path) as db:
        db.execute('INSERT INTO "Users"("userId") VALUES(?)', (OTHER,))
    foreign = attempt(path, user=OTHER, status='authorized', observation=authorizedAt(NOW - timedelta(minutes=1)))
    provider.order.fetch = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('no provider read for another owner'))
    response = client.get('/payment-status')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.json() == {'checkout': None, 'refunds': [], 'paymentAttention': None}
    assert foreign not in response.text

    from api.commons import verifyToken
    client.app.dependency_overrides.pop(verifyToken)
    unauthenticated = client.get('/payment-status')
    assert unauthenticated.status_code in (401, 403)


@pytest.mark.parametrize('age,refresh', [(timedelta(seconds=4, microseconds=999999), False),
                                         (timedelta(seconds=5), True), (timedelta(seconds=-1), True), (None, True)])
def test_provider_refresh_is_bounded_by_the_last_observation(statusDb, age, refresh):
    from api.services.billing.paymentStatusPresentation import PROVIDER_REFRESH_INTERVAL, needsProviderRefresh
    assert PROVIDER_REFRESH_INTERVAL == timedelta(seconds=5)
    _, path = statusDb
    observation = {'outcome': 'unpaid', 'complete': True, 'checkedAt': (NOW - age).isoformat()} if age is not None else None
    attemptId = attempt(path, observation=observation)
    with sqlTransaction(path) as db:
        db.row_factory = sqlite3.Row
        row = dict(db.execute('SELECT * FROM billing_events WHERE id=?', (attemptId,)).fetchone())
    assert needsProviderRefresh(row, NOW) is refresh


def test_refresh_error_still_presents_committed_status(statusClient, monkeypatch):
    from api.services.billing.manualBillingContracts import CheckoutProviderSnapshot
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    client, _, path = statusClient
    with sqlTransaction(path) as db:
        db.execute('''CREATE TABLE payment_operations (
            id TEXT PRIMARY KEY, user_id TEXT, attempt_id TEXT, invoice_id TEXT, provider_payment_id TEXT,
            kind TEXT, reason TEXT, state TEXT, amount INTEGER, currency TEXT, error_code TEXT,
            escalation_reference TEXT, created_at TEXT, updated_at TEXT)''')
    shown = attempt(path)
    refunded = attempt(path, status='cancelled', closed=True, attemptedAt=NOW - timedelta(minutes=20))
    operation(path, attemptId=refunded)
    monkeypatch.setattr(ManualBillingRecoveryService, 'inspectAttempt', lambda self, row: CheckoutProviderSnapshot(
        str(row['id']), row['provider_order_id'], 'captured', NOW, True, int(row['amount']), row['currency'], ()))
    def bound(self, row, snapshot):
        raise ValueError('PAYMENT_ALREADY_BOUND')
    monkeypatch.setattr(ManualBillingRecoveryService, 'applyObservation', bound)
    response = client.get('/payment-status')
    assert response.status_code == 200
    body = response.json()
    assert body['checkout']['attemptId'] == shown and [r['attemptId'] for r in body['refunds']] == [refunded]


def test_status_route_replaces_the_per_attempt_status_route(statusClient):
    client, _, path = statusClient
    owned = attempt(path)
    response = client.post('/purchase-checkouts/' + owned + '/status', json={})
    assert response.status_code in (404, 405)
