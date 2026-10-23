"""Grant rules for captured payments (spec 4.4) on real SQL with a synthetic provider; never real money.

The first captured payment in an unsettled slot grants its own attempt's frozen snapshot and settles
the slot; every other captured payment is returned in full through one durable refund operation.
"""
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest

from test.purchaseProviderFixtures import SyntheticPurchaseProvider
from test.test_purchase_checkout_postgres import checkoutDb, drafts, controlledPurchase, supersede  # noqa:F401
from test.test_subscription_erasure_postgres import erasureDb  # noqa:F401
from test.subscriptionRuntimeFixtures import postgres, runtimeDb, query  # noqa:F401
from test.test_payment_replacement_postgres import (paidAccount, monthlyRenewalInvoice, annualRenewalInvoice,
    purposePay, frozen, control, liveIn, legacyTermEndingSoon, afterTermEnd, expireAttempt)

pytestmark = pytest.mark.skipif(os.environ.get('RUN_MANUAL_BILLING_INTEGRATION') != '1',
    reason='Disposable PostgreSQL opt-in; skip is UNVERIFIED')


def providerPayment(provider, intent, status, created=None):
    """A provider payment on the intent's Order, started at ``created`` (provider seconds)."""
    item = provider.authorize(intent)
    provider.payments[item['id']].update(status=status,
        created_at=int(created if created is not None else time.time()))
    return provider.payments[item['id']]


def capturedNow(provider, paymentId):
    provider.payments[paymentId]['status'] = 'captured'
    return provider.payments[paymentId]


def evidenceFor(intent, item, observedAt=None):
    from api.services.billing.manualBillingRepository import _now
    from api.services.billing.manualPaymentService import ManualPaymentService
    return ManualPaymentService.buildPaymentEvidence(item, attemptId=intent.attemptId, invoiceId=intent.invoiceId,
        userId=intent.userId, purpose=intent.purpose, observedAt=observedAt or _now())


def finalize(repo, intent, item, observedAt=None):
    return repo.finalizeCapturedPayment(evidenceFor(intent, item, observedAt))


def after(moment):
    """The first whole provider second at or after ``moment``."""
    return math.ceil(moment.timestamp())


def before(moment, seconds=1):
    return math.floor(moment.timestamp()) - seconds


def operations(url, user=None, paymentId=None):
    clause, args = ('provider_payment_id=%s', (paymentId,)) if paymentId else ('user_id=%s', (user,))
    return query(url, f'SELECT * FROM payment_operations WHERE {clause} ORDER BY created_at', args)


def captureRow(url, paymentId):
    return query(url, '''SELECT event_status,failure_reason,metadata_json FROM billing_events
        WHERE provider_payment_id=%s AND event_type='payment.capture' ''', (paymentId,), one=True)


def slotOfAttempt(url, attemptId):
    return query(url, '''SELECT s.* FROM payment_slot_controls c JOIN payment_slots s ON s.slot_id=c.slot_id
        WHERE c.attempt_id=%s''', (attemptId,), one=True)


def invoiceRow(url, invoiceId):
    return query(url, 'SELECT * FROM "Invoices" WHERE id=%s', (invoiceId,), one=True)


def startedPurchaseThenReplaced(drafts, monkeypatch):
    """The user started paying A (an unanswered payment), then Pay with a changed selection replaced A by B."""
    repo, provider, first, _ = controlledPurchase(drafts, monkeypatch)
    started = providerPayment(provider, first, 'created')  # 'created' never holds Pay (spec 4.3)
    second = supersede(repo, provider, first)
    return repo, provider, first, second, started


# -- spec 5 case 6: a late webhook for a payment started inside the TTL --

def test_late_webhook_inside_ttl_grants_not_stranded(drafts, monkeypatch):
    repo, provider, intent, _ = controlledPurchase(drafts, monkeypatch)
    url = drafts[3]
    item = providerPayment(provider, intent, 'captured', created=time.time() - 60)
    expireAttempt(url, intent.attemptId)  # the webhook is observed after the checkout's TTL
    result = finalize(repo, intent, item)
    assert result.finalized and result.state == 'activated'
    assert repo.getCoverageSnapshot(intent.userId).accessAllowed
    assert captureRow(url, item['id'])['event_status'] == 'FINALIZED'
    assert slotOfAttempt(url, intent.attemptId)['settled_payment_id'] == item['id']
    assert operations(url, intent.userId) == []


# -- spec 5 cases 1, 4a: the first capture wins an unsettled slot, even on a superseded attempt --

def test_superseded_payment_wins_unsettled_slot_and_closes_newer(drafts, monkeypatch):
    url = drafts[3]
    repo, provider, first, second, started = startedPurchaseThenReplaced(drafts, monkeypatch)
    assert frozen(url, first.attemptId)[1]['closedReason'] == 'SUPERSEDED'
    result = finalize(repo, first, capturedNow(provider, started['id']))
    assert result.finalized and result.state == 'activated'
    coverage = repo.getCoverageSnapshot(first.userId)
    assert set(coverage.currentPeriod.domains) == {'banking', 'manufacturing'}  # the paid (earlier) selection
    assert invoiceRow(url, first.invoiceId)['status'] == 'PAID'
    assert invoiceRow(url, second.invoiceId)['status'] == 'VOID'
    status, billing = frozen(url, second.attemptId)
    assert status == 'cancelled' and billing['closedReason'] == 'SLOT_SETTLED'
    closed = control(url, second.attemptId)
    assert closed['closed_reason'] == 'SLOT_SETTLED' and closed['closed_at'] is not None and closed['recover_until']
    slot = slotOfAttempt(url, first.attemptId)
    assert slot['settled_payment_id'] == started['id'] and slot['slot_id'] == slotOfAttempt(url, second.attemptId)['slot_id']
    assert liveIn(url, first.userId, ['initial_purchase']) == []
    assert operations(url, first.userId) == []


def test_second_capture_in_slot_reserves_one_full_refund(drafts, monkeypatch):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    url = drafts[3]
    repo, provider, first, second, started = startedPurchaseThenReplaced(drafts, monkeypatch)
    late = providerPayment(provider, second, 'created')  # paying B in another window
    assert finalize(repo, first, capturedNow(provider, started['id'])).finalized
    duplicate = finalize(repo, second, capturedNow(provider, late['id']))
    assert duplicate.state == 'refund_pending' and not duplicate.finalized
    # Replays from every path keep one operation for the full captured amount in its own currency.
    assert finalize(repo, second, late).state == 'refund_pending'
    ManualBillingRecoveryService(repo, provider).recoverAttempt(repo.attemptById(second.userId, second.attemptId))
    ops = operations(url, second.userId)
    assert len(ops) == 1
    op = ops[0]
    assert (op['reason'], op['kind'], op['state']) == ('duplicate', 'refund', 'queued')
    assert op['amount'] == second.amount and op['currency'] == second.currency
    assert op['dedupe_key'] == 'payment:refund:' + late['id'] and str(op['attempt_id']) == second.attemptId
    assert captureRow(url, late['id'])['event_status'] == 'REFUND_RESERVED'
    assert captureRow(url, late['id'])['failure_reason'] == 'DUPLICATE'
    coverage = repo.getCoverageSnapshot(first.userId)
    assert set(coverage.currentPeriod.domains) == {'banking', 'manufacturing'}


# -- ruling 9 / spec 4.4 row 6: a payment started after its checkout ended is returned --

@pytest.mark.parametrize('closure', ['superseded', 'ttl_expired'])
def test_capture_started_after_closure_refunds_without_access(drafts, monkeypatch, closure):
    url = drafts[3]
    repo, provider, first, _ = controlledPurchase(drafts, monkeypatch)
    live = None
    if closure == 'superseded':
        live = supersede(repo, provider, first)
        closedAt = control(url, first.attemptId)['closed_at']
    else:
        expireAttempt(url, first.attemptId)
        closedAt = datetime.fromisoformat(frozen(url, first.attemptId)[1]['expiresAt'])
    item = providerPayment(provider, first, 'captured', created=after(closedAt))
    result = finalize(repo, first, item)
    assert result.state == 'refund_pending' and not result.finalized
    assert not repo.getCoverageSnapshot(first.userId).accessAllowed
    ops = operations(url, first.userId)
    assert [(op['reason'], op['amount']) for op in ops] == [('paid_after_close', first.amount)]
    assert slotOfAttempt(url, first.attemptId)['settled_payment_id'] is None
    if live is not None:
        assert liveIn(url, first.userId, ['initial_purchase']) == [live.attemptId]


def test_pay_finalizes_a_refundable_capture_first_and_continues(drafts, monkeypatch):
    """Spec 4.3: an Order already captured is finalized first; when that money is returned, Pay goes on."""
    url = drafts[3]
    repo, provider, first, _ = controlledPurchase(drafts, monkeypatch)
    expireAttempt(url, first.attemptId)  # the checkout's TTL passed while it was still the live attempt
    expiresAt = datetime.fromisoformat(frozen(url, first.attemptId)[1]['expiresAt'])
    item = providerPayment(provider, first, 'captured', created=after(expiresAt))
    replacement = supersede(repo, provider, first)
    assert replacement.razorpayOrderId and replacement.state != 'confirming_earlier_payment'
    assert frozen(url, first.attemptId)[1]['closedReason'] == 'PAYMENT_REFUNDED'
    assert [(op['provider_payment_id'], op['reason']) for op in operations(url, first.userId)] == [
        (item['id'], 'paid_after_close')]
    assert liveIn(url, first.userId, ['initial_purchase']) == [replacement.attemptId]


# -- spec 5 case 8: a renewal started before the boundary is granted for its frozen term --

@pytest.mark.parametrize('mode,started,expected', [
    ('monthly_prepaid', 'before_end', 'grant'), ('annual_prepaid', 'before_end', 'grant'),
    ('monthly_prepaid', 'after_end', 'refund')])
def test_renewal_started_before_boundary_captured_after_grants_term(drafts, monkeypatch, mode, started, expected):
    url = drafts[3]
    service, provider, renewal, invoice, end = legacyTermEndingSoon(drafts, monkeypatch, mode)
    billing = service.repository
    item = providerPayment(provider, renewal, 'created', created=before(end) if started == 'before_end' else None)
    afterTermEnd(end)
    if started == 'after_end':
        item['created_at'] = after(end)
    result = finalize(billing, renewal, capturedNow(provider, item['id']))
    if expected == 'grant':
        assert result.finalized, result
        paid = invoiceRow(url, str(invoice['id']))
        assert paid['status'] == 'PAID' and paid['period_start'] == end
        assert operations(url, renewal.userId) == []
        assert slotOfAttempt(url, renewal.attemptId)['settled_payment_id'] == item['id']
    else:
        assert result.state == 'refund_pending'
        assert [op['reason'] for op in operations(url, renewal.userId)] == ['paid_after_close']
        assert invoiceRow(url, str(invoice['id']))['status'] != 'PAID'


def legacyPaidTermEndingSoon(drafts, monkeypatch, seconds=6):
    from dateutil.relativedelta import relativedelta
    from api.services.billing.manualBillingContracts import CheckoutRequest
    from api.services.billing.manualPaymentService import ManualPaymentService
    _, billing, user, url = drafts
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED')
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE', 'legacy')
    provider = SyntheticPurchaseProvider()
    service = ManualPaymentService.forProduction(provider, billing)
    paid = service.createCheckout(CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid', {'domains': ['banking']},
                                                  'paid-term', expectedCatalogRevision=1))
    assert finalize(billing, paid, providerPayment(provider, paid, 'captured')).finalized
    end = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=seconds)
    query(url, 'UPDATE "Invoices" SET period_start=%s,period_end=%s WHERE id=%s',
          (end - relativedelta(months=1), end, paid.invoiceId))
    query(url, 'UPDATE subscriptions SET current_period_start=%s,current_period_end=%s,renewal_due_at=%s WHERE user_id=%s',
          (end - relativedelta(months=1), end, end, user))
    return service, provider, user, end


def test_expert_addition_captured_after_term_end_refunds(drafts, monkeypatch):
    from api.services.billing.manualBillingContracts import CheckoutRequest
    url = drafts[3]
    service, provider, user, end = legacyPaidTermEndingSoon(drafts, monkeypatch)
    addition = service.createCheckout(CheckoutRequest(user, 'expert_addition', 'monthly_prepaid',
        {'domains': ['telecom']}, 'add-before-end', expectedCatalogRevision=1))
    item = providerPayment(provider, addition, 'created')  # started while the term was current
    afterTermEnd(end)
    result = finalize(service.repository, addition, capturedNow(provider, item['id']))
    assert result.state == 'refund_pending' and not result.finalized
    assert [(op['reason'], op['amount']) for op in operations(url, user)] == [('not_deliverable', addition.amount)]
    experts = query(url, 'SELECT subscribed_experts FROM subscriptions WHERE user_id=%s AND is_canonical', (user,), one=True)
    assert 'telecom' not in experts['subscribed_experts']


# -- spec 4.4: expert addition vs renewal; a renewal on an older revision is granted as frozen --

@pytest.mark.parametrize('interval', ['month', 'year'])
def test_renewal_on_older_revision_grants_attempt_snapshot(drafts, monkeypatch, interval):
    url = drafts[3]
    mode = 'monthly_prepaid' if interval == 'month' else 'annual_prepaid'
    repo, provider, initial = paidAccount(drafts, monkeypatch, interval)
    user = initial.userId
    invoice = monthlyRenewalInvoice(repo, user) if interval == 'month' else annualRenewalInvoice(repo, user)
    renewal = purposePay(repo, provider, user, 'renewal', mode, {'invoiceId': str(invoice['id'])})
    issued = invoiceRow(url, renewal.invoiceId)
    started = providerPayment(provider, renewal, 'created')  # the renewal payment is under way
    addition = purposePay(repo, provider, user, 'expert_addition', mode, {'domains': ['telecom']})
    assert finalize(repo, addition, providerPayment(provider, addition, 'captured')).state == 'expert_activated'
    # Granting the addition re-froze the still-unpaid renewal invoice, as today.
    assert frozen(url, renewal.attemptId)[1]['closedReason'] == 'EXPERT_SELECTION_CHANGED'
    if interval == 'year':
        assert repo.prepareAnnualRenewalInvoice(renewal.invoiceId) is not None  # repriced in place
        repriced = invoiceRow(url, renewal.invoiceId)
        assert repriced['metadata_json']['manualBilling']['revision'] == 2
        assert repriced['total_amount'] != issued['total_amount']
        newer = None
    else:
        newer = monthlyRenewalInvoice(repo, user)  # the re-frozen revision for the same term
        assert str(newer['id']) != renewal.invoiceId
    result = finalize(repo, renewal, capturedNow(provider, started['id']))
    assert result.finalized and result.state == 'paid_scheduled', result
    paid = invoiceRow(url, renewal.invoiceId)
    assert paid['status'] == 'PAID' and paid['razorpayPaymentId'] == started['id']
    assert paid['razorpay_order_id'] == renewal.razorpayOrderId
    for field in ('total_amount', 'amount_before_tax', 'tax_amount', 'currency', 'tax_rule_version'):
        assert paid[field] == issued[field], field
    assert paid['metadata_json']['manualBilling']['revision'] == 1
    assert paid['metadata_json']['manualBilling']['domains'] == ['banking']
    coverage = repo.getCoverageSnapshot(user)
    assert coverage.nextPeriod and set(coverage.nextPeriod.domains) == {'banking'}  # what was paid
    assert set(coverage.currentPeriod.domains) == {'banking', 'telecom'}
    if newer is not None:
        assert invoiceRow(url, str(newer['id']))['status'] == 'VOID'
    assert operations(url, user) == []


# -- arrival order, concurrency, recovery --

@pytest.mark.parametrize('order', ['earlier_first', 'newer_first'])
def test_capture_order_independent_of_event_arrival(drafts, monkeypatch, order):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    url = drafts[3]
    repo, provider, first, second, started = startedPurchaseThenReplaced(drafts, monkeypatch)
    newer = providerPayment(provider, second, 'created')
    capturedNow(provider, started['id'])
    capturedNow(provider, newer['id'])
    sequence = [(first, started), (second, newer)]
    if order == 'newer_first':
        sequence.reverse()
    (winner, winning), (loser, losing) = sequence
    assert finalize(repo, winner, winning).finalized
    assert finalize(repo, loser, losing).state == 'refund_pending'
    # Every later delivery (webhook retry, browser verify, recovery) replays the stored outcome.
    for intent, item in sequence * 2:
        finalize(repo, intent, item)
        ManualBillingRecoveryService(repo, provider).recoverAttempt(repo.attemptById(intent.userId, intent.attemptId))
    assert [op['provider_payment_id'] for op in operations(url, first.userId)] == [losing['id']]
    assert captureRow(url, winning['id'])['event_status'] == 'FINALIZED'
    assert slotOfAttempt(url, winner.attemptId)['settled_payment_id'] == winning['id']
    expected = {'banking', 'manufacturing'} if winner is first else {'banking'}
    assert set(repo.getCoverageSnapshot(first.userId).currentPeriod.domains) == expected


def test_concurrent_finalizers_one_outcome(drafts, monkeypatch):
    url = drafts[3]
    repo, provider, first, second, started = startedPurchaseThenReplaced(drafts, monkeypatch)
    newer = providerPayment(provider, second, 'created')
    capturedNow(provider, started['id'])
    capturedNow(provider, newer['id'])
    jobs = [(first, started)] * 3 + [(second, newer)] * 3
    gate = Barrier(len(jobs))
    def run(job):
        gate.wait(timeout=10)
        return finalize(repo, *job).state
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        states = list(pool.map(run, jobs))
    granted = [state for state in states if state == 'activated']
    assert len(granted) == 1, states
    assert set(states) <= {'activated', 'already_finalized', 'refund_pending'}
    assert len(operations(url, first.userId)) == 1
    for item in (started, newer):
        assert query(url, '''SELECT count(*) AS n FROM billing_events WHERE provider_payment_id=%s
            AND event_type='payment.capture' ''', (item['id'],), one=True)['n'] == 1
    settled = query(url, 'SELECT settled_payment_id FROM payment_slots WHERE user_id=%s AND settled_payment_id IS NOT NULL',
                    (first.userId,))
    assert len(settled) == 1


def test_recovery_pass_finalizes_without_browser_or_webhook(drafts, monkeypatch):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    url = drafts[3]
    repo, provider, intent, _ = controlledPurchase(drafts, monkeypatch)
    item = providerPayment(provider, intent, 'captured')
    query(url, "UPDATE billing_events SET updated_at=clock_timestamp()-interval '30 days' WHERE id=%s", (intent.attemptId,))
    # The minute worker's pass, scoped to this account's due row (other tests' synthetic Orders are not ours).
    due = [row for row in repo.pendingRecoveryRows(limit=50) if str(row['id']) == intent.attemptId]
    assert len(due) == 1
    assert ManualBillingRecoveryService(repo, provider).recoverAttempt(due[0]) == 1
    assert repo.getCoverageSnapshot(intent.userId).accessAllowed
    assert captureRow(url, item['id'])['metadata_json']['paymentCreatedAt'] == datetime.fromtimestamp(
        item['created_at'], timezone.utc).isoformat()


# -- spec 5 case 12: banned or erasing owner --

@pytest.mark.parametrize('condition', ['banned', 'erasure_pending'])
def test_banned_or_erasing_owner_capture_refunds(drafts, monkeypatch, condition):
    url = drafts[3]
    repo, provider, intent, _ = controlledPurchase(drafts, monkeypatch)
    item = providerPayment(provider, intent, 'captured')
    if condition == 'banned':
        admin = query(url, '''INSERT INTO admin_users(email,name,password_hash)
            VALUES(%s,'Synthetic','not-a-real-password') RETURNING id''', (str(uuid4()) + '@example.test',), one=True)['id']
        query(url, '''UPDATE "Users" SET "isBanned"=true,"bannedAt"=now(),"bannedBy"=%s,"banReason"='synthetic'
            WHERE "userId"=%s''', (admin, intent.userId))
    else:
        query(url, 'UPDATE subscriptions SET erasure_pending=true WHERE user_id=%s', (intent.userId,))
    result = finalize(repo, intent, item)
    assert result.state == 'refund_pending' and not result.finalized
    assert [(op['reason'], op['amount']) for op in operations(url, intent.userId)] == [('account_closed', intent.amount)]
    status, billing = frozen(url, intent.attemptId)
    assert status != 'captured' and billing['closedReason'] == 'PAYMENT_REFUNDED'
    assert invoiceRow(url, intent.invoiceId)['status'] == 'VOID'
    assert slotOfAttempt(url, intent.attemptId)['settled_payment_id'] is None


# -- spec 5 case 16: payments started before the cutover keep today's handling --

def test_historical_anomaly_untouched(drafts, monkeypatch):
    from api.services.billing.manualPaymentService import ManualPaymentService
    url = drafts[3]
    repo, provider, first, _ = controlledPurchase(drafts, monkeypatch)
    live = supersede(repo, provider, first)
    item = providerPayment(provider, first, 'captured')
    monkeypatch.setenv('PAYMENT_RULES_EFFECTIVE_AT', (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
    for _ in range(2):
        result = ManualPaymentService.forProduction(provider, repo).finalizeCapturedPayment(evidenceFor(first, item))
        assert result.state == 'requires_reconciliation'
    assert captureRow(url, item['id'])['event_status'] == 'REQUIRES_RECONCILIATION'
    assert operations(url, first.userId) == []
    assert slotOfAttempt(url, first.attemptId)['settled_payment_id'] is None
    assert liveIn(url, first.userId, ['initial_purchase']) == [live.attemptId]


# -- evidence carries provider created_at on every path --

@pytest.mark.parametrize('path', ['webhook', 'order_paid', 'verify', 'recovery'])
def test_every_evidence_path_carries_provider_created_at(drafts, monkeypatch, path):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    from api.services.subscriptions.subscriptionService import subscriptionService
    from api.services.webhookService import WebhookService
    url = drafts[3]
    repo, provider, intent, _ = controlledPurchase(drafts, monkeypatch)
    item = providerPayment(provider, intent, 'captured', created=time.time() - 30)
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository', lambda: repo)
    if path == 'webhook':
        WebhookService.__new__(WebhookService)._handlePaymentCaptured({'payload': {'payment': {'entity': dict(item)}}})
    elif path == 'order_paid':
        WebhookService.__new__(WebhookService)._handleOrderPaid({'payload': {'payment': {'entity': dict(item)},
            'order': {'entity': {'id': intent.razorpayOrderId}}}})
    elif path == 'verify':
        subscriptionService._finalizeManualCheckout(intent.invoiceId, intent.razorpayOrderId, item['id'],
                                                    provider.payment.fetch(item['id']))
    else:
        ManualBillingRecoveryService(repo, provider).recoverAttempt(repo.attemptById(intent.userId, intent.attemptId))
    row = captureRow(url, item['id'])
    assert row['event_status'] == 'FINALIZED'
    assert row['metadata_json']['paymentCreatedAt'] == datetime.fromtimestamp(item['created_at'], timezone.utc).isoformat()


# -- ruling 5: a reused pre-rollout attempt without a control is bound before it settles --

def test_pre_rollout_attempt_without_control_binds_and_settles(drafts, monkeypatch):
    from unittest.mock import patch
    from api.services.billing.manualBillingContracts import CheckoutRequest
    _, billing, user, url = drafts
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED')
    with patch('api.services.billing.purchaseCheckoutRepository.bindPurchaseControlLocked', lambda *args: None):
        intent = billing.reserveCheckout(CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid',
            {'domains': ['banking']}, 'pre-rollout', expectedCatalogRevision=1))
    provider = SyntheticPurchaseProvider()
    order = provider.createOrder({'amount': intent.amount, 'currency': intent.currency, 'receipt': intent.attemptId})
    intent = billing.bindProviderOrder(intent.attemptId, order)
    # Reserved before rollout: no control and no frozen capture policy (the account default applied).
    query(url, "UPDATE billing_events SET metadata_json=metadata_json #- '{manualBilling,capturePolicy}' WHERE id=%s",
          (intent.attemptId,))
    assert control(url, intent.attemptId) is None
    assert finalize(billing, intent, providerPayment(provider, intent, 'captured')).finalized
    bound = control(url, intent.attemptId)
    assert bound['capture_policy'] == 'account_default' and bound['provider_order_id'] == order['id']
    assert slotOfAttempt(url, intent.attemptId)['settled_payment_id'] is not None


# -- storage guards (rulings 3, 4) --

def test_slot_settles_only_with_a_captured_payment_of_its_own_attempts(drafts, monkeypatch):
    import psycopg2
    url = drafts[3]
    repo, provider, first, second, started = startedPurchaseThenReplaced(drafts, monkeypatch)
    newer = providerPayment(provider, second, 'created')
    slot = slotOfAttempt(url, second.attemptId)['slot_id']
    for paymentId in ('pay-unknown-' + str(uuid4()), newer['id'], started['id']):
        with pytest.raises(psycopg2.Error, match='settlement'):
            query(url, 'UPDATE payment_slots SET settled_payment_id=%s,settled_at=clock_timestamp() WHERE slot_id=%s',
                  (paymentId, slot))
    assert finalize(repo, first, capturedNow(provider, started['id'])).finalized
    assert slotOfAttempt(url, second.attemptId)['settled_payment_id'] == started['id']


def test_refund_operation_requires_an_ungranted_recorded_capture(drafts, monkeypatch):
    import psycopg2
    url = drafts[3]
    repo, provider, first, second, started = startedPurchaseThenReplaced(drafts, monkeypatch)
    assert finalize(repo, first, capturedNow(provider, started['id'])).finalized
    granted = control(url, first.attemptId)
    for paymentId in (started['id'], 'pay-never-recorded-' + str(uuid4())):
        with pytest.raises(psycopg2.Error, match='refund'):
            query(url, '''INSERT INTO payment_operations(user_id,attempt_id,invoice_id,provider_payment_id,kind,reason,
                amount,currency,dedupe_key) VALUES(%s,%s,%s,%s,'refund','duplicate',%s,%s,%s)''',
                (first.userId, first.attemptId, str(granted['invoice_id']), paymentId, first.amount, first.currency,
                 'payment:refund:' + paymentId))
    assert operations(url, first.userId) == []
