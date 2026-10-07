from dataclasses import replace
from datetime import timedelta
import json
from unittest.mock import Mock, patch
from test.test_manual_billing_runtime import database, USER, NOW, sqlTransaction
from test.test_manual_checkout_http import checkout_database, request
from test.test_manual_payment_entrypoints import paid_then_request, evidence
from test.test_billing_notification_revisions import deliveries


def unresolved_renewal(checkout_database):
    repo, path = checkout_database
    manual, checkout = paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    pending = manual.createCheckout(checkout)
    with sqlTransaction(path) as db:
        end = db.execute('select current_period_end from subscriptions').fetchone()[0]
    from api.services.billing.manualBillingRepository import _utc
    later = _utc(end) + timedelta(seconds=1)
    with patch('api.services.billing.manualBillingRepository._now', return_value=later):
        result = manual.finalizeCapturedPayment(replace(evidence(pending, 'late-renewal'), observedAt=later, provenCaptureAt=None, timingVerified=False))
    assert result.state == 'requires_reconciliation'
    return repo, path, later


def test_expiry_commit_holds_for_unresolved_owned_capture(checkout_database):
    repo, path, later = unresolved_renewal(checkout_database)
    with patch('api.services.billing.manualBillingRepository._now', return_value=later):
        repo.activateDueCoverage(USER, later)
    with sqlTransaction(path) as db:
        events = db.execute("select metadata_json from billing_events where event_type='email.billing_intent.committed'").fetchall()
    assert not any(json.loads(row[0])['notificationType'] == 'monthly_subscription_expired' for row in events)


def test_expiry_dispatch_holds_for_unresolved_owned_capture(checkout_database, deliveries):
    repo, path, later = unresolved_renewal(checkout_database)
    delivery_repo, _ = deliveries
    from api.services.billing.manualBillingRepository import _utc
    with sqlTransaction(path) as db:
        end = db.execute('select current_period_end from subscriptions').fetchone()[0]
    delivery_repo.enqueueBillingNotification(USER, repo.ensureCanonicalSubscription(USER)['id'], 'monthly_subscription_expired',
        'expiry-test', end, {})
    with sqlTransaction(path) as db:
        db.execute("update notification_deliveries set status='SENDING',lease_owner='worker',claimed_payload_version=payload_version,lease_expires_at=?", ((later+timedelta(minutes=5)).isoformat(),))
    with patch('api.services.billing.manualBillingRepository._now', return_value=later):
        assert not delivery_repo.authorizeBillingSubmission('delivery-one','worker',1)


# -- solicitation hold for unresolved owned-cycle money ------------------------

import pytest


def renewal_cycle(checkout_database):
    """Paid current cycle with an unpaid, prepared renewal invoice 'renewal'."""
    repo, path = checkout_database
    paid_then_request(checkout_database, 'monthly_prepaid', 'renewal')
    with sqlTransaction(path) as db:
        sub, end = db.execute('select id,current_period_end from subscriptions').fetchone()
    from api.services.billing.manualBillingRepository import _utc
    return repo, path, _utc(end), sub


def owned_capture(path, sub, *, user=USER, invoice='renewal', key='held-capture'):
    with sqlTransaction(path) as db:
        db.execute('''insert into billing_events(id,user_id,subscription_id,invoice_id,event_category,event_type,
            event_status,provider_payment_id,idempotency_key,created_at)
            values(?,?,?,?,'reconciliation','payment.capture','REQUIRES_RECONCILIATION',?,?,?)''',
            (key, user, sub, invoice, 'pay-' + key, key, NOW.isoformat()))


def claim(path, at):
    with sqlTransaction(path) as db:
        db.execute("update notification_deliveries set status='SENDING',lease_owner='worker',claimed_payload_version=payload_version,lease_expires_at=?",
                   ((at + timedelta(minutes=5)).isoformat(),))


SOLICITATIONS = [('monthly_renewal_ready', timedelta(days=5)), ('monthly_renewal_reminder', timedelta(hours=12))]


@pytest.mark.parametrize('kind,before', SOLICITATIONS)
def test_central_eligibility_holds_ready_and_reminder(kind, before):
    from api.services.notifications.billingNotificationService import isBillingNotificationEligible
    end = NOW + timedelta(days=10)
    row = {'notification_type': kind, 'period_end': end.isoformat(), 'metadata_json': {}}
    snapshot = {'subscription': {'current_period_end': end.isoformat()}, 'invoice': {'status': 'UPCOMING'}}
    assert isBillingNotificationEligible(row, snapshot, end - before)
    assert not isBillingNotificationEligible(row, {**snapshot, 'unresolvedOwnedCapture': True}, end - before)


def test_central_eligibility_keeps_receipts_deliverable_during_hold():
    from api.services.notifications.billingNotificationService import isBillingNotificationEligible
    assert isBillingNotificationEligible({'notification_type': 'payment_receipt'},
        {'subscription': {}, 'unresolvedOwnedCapture': True}, NOW)


@pytest.mark.parametrize('kind,before', SOLICITATIONS)
def test_solicitation_queued_before_capture_is_held_at_submission(checkout_database, deliveries, kind, before):
    repo, path, end, sub = renewal_cycle(checkout_database)
    delivery_repo, _ = deliveries
    at = end - before
    delivery_repo.enqueueBillingNotification(USER, sub, kind, kind + '-test', end.isoformat(), {'invoiceId': 'renewal'})
    owned_capture(path, sub)
    claim(path, at)
    with patch('api.services.billing.manualBillingRepository._now', return_value=at):
        assert delivery_repo.authorizeBillingSubmissionResult('delivery-one', 'worker', 1) == 'HELD'
    with sqlTransaction(path) as db:
        assert db.execute('select submission_started_at from notification_deliveries').fetchone()[0] is None


@pytest.mark.parametrize('variant', ['other_user', 'other_cycle', 'other_lifecycle'])
def test_unrelated_capture_does_not_hold_this_renewal(checkout_database, deliveries, variant):
    repo, path, end, sub = renewal_cycle(checkout_database)
    delivery_repo, _ = deliveries
    if variant == 'other_user':
        owned_capture(path, sub, user='other-user')
    elif variant == 'other_cycle':
        with sqlTransaction(path) as db:
            db.execute('''insert into "Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,currency,
                period_start,period_end,metadata_json) values('later',?,?,'UPCOMING','renewal',10000,'INR',?,?,'{}')''',
                (USER, sub, (end + timedelta(days=31)).isoformat(), (end + timedelta(days=61)).isoformat()))
        owned_capture(path, sub, invoice='later')
    else:
        with sqlTransaction(path) as db:
            db.execute('update "Invoices" set metadata_json=? where id=?',
                (json.dumps({'manualBilling': {'domains': ['banking'], 'revision': 1, 'lifecycleId': 'stale-lifecycle'}}), 'renewal'))
        owned_capture(path, sub)
    at = end - timedelta(days=5)
    delivery_repo.enqueueBillingNotification(USER, sub, 'monthly_renewal_ready', 'ready-test', end.isoformat(), {'invoiceId': 'renewal'})
    claim(path, at)
    with patch('api.services.billing.manualBillingRepository._now', return_value=at):
        assert delivery_repo.authorizeBillingSubmissionResult('delivery-one', 'worker', 1) == 'AUTHORIZED'


def test_receipt_submission_is_not_held(checkout_database, deliveries):
    repo, path, end, sub = renewal_cycle(checkout_database)
    delivery_repo, _ = deliveries
    owned_capture(path, sub)
    at = end - timedelta(days=5)
    delivery_repo.enqueueBillingNotification(USER, sub, 'payment_receipt', 'receipt-test', end.isoformat(), {'paymentId': 'pay'})
    claim(path, at)
    with patch('api.services.billing.manualBillingRepository._now', return_value=at):
        assert delivery_repo.authorizeBillingSubmissionResult('delivery-one', 'worker', 1) == 'AUTHORIZED'


def test_held_solicitation_past_its_window_is_rejected_not_held(checkout_database, deliveries):
    repo, path, end, sub = renewal_cycle(checkout_database)
    delivery_repo, _ = deliveries
    owned_capture(path, sub)
    at = end - timedelta(hours=12)  # T-1: the T-7 ready message is no longer sendable
    delivery_repo.enqueueBillingNotification(USER, sub, 'monthly_renewal_ready', 'ready-test', end.isoformat(), {'invoiceId': 'renewal'})
    claim(path, at)
    with patch('api.services.billing.manualBillingRepository._now', return_value=at):
        assert delivery_repo.authorizeBillingSubmissionResult('delivery-one', 'worker', 1) == 'REJECTED'


def test_expiry_submission_is_held_not_rejected(checkout_database, deliveries):
    repo, path, later = unresolved_renewal(checkout_database)
    delivery_repo, _ = deliveries
    with sqlTransaction(path) as db:
        end = db.execute('select current_period_end from subscriptions').fetchone()[0]
    delivery_repo.enqueueBillingNotification(USER, repo.ensureCanonicalSubscription(USER)['id'], 'monthly_subscription_expired',
        'expiry-test', end, {})
    claim(path, later)
    with patch('api.services.billing.manualBillingRepository._now', return_value=later):
        assert delivery_repo.authorizeBillingSubmissionResult('delivery-one', 'worker', 1) == 'HELD'


def test_hold_reschedule_does_not_consume_a_delivery_attempt(deliveries):
    delivery_repo, path = deliveries
    delivery_repo.enqueueBillingNotification(USER, 'sub', 'monthly_renewal_ready', 'ready-test', NOW.isoformat(), {})
    with sqlTransaction(path) as db:
        db.execute("update notification_deliveries set status='SENDING',lease_owner='worker',claimed_payload_version=1,attempt_count=3")
    assert delivery_repo.scheduleRetry('delivery-one', 'worker', 'PAYMENT_HOLD', NOW.isoformat(), None, payloadVersion=1)
    with sqlTransaction(path) as db:
        assert db.execute('select status,last_error_code,attempt_count from notification_deliveries').fetchone() == ('RETRY_PENDING', 'PAYMENT_HOLD', 2)


@pytest.mark.parametrize('kind,before', SOLICITATIONS)
def test_solicitation_enqueue_is_held_for_unresolved_owned_capture(checkout_database, deliveries, kind, before):
    repo, path, end, sub = renewal_cycle(checkout_database)
    owned_capture(path, sub)
    from nubrix.triggers.tasks.monthlyRenewalTask import MonthlyRenewalTask
    intent = {'userId': USER, 'notificationType': kind, 'dedupeKey': kind + '-test', 'metadata': {'invoiceId': 'renewal'}}
    with patch('api.services.billing.manualBillingRepository._now', return_value=end - before), \
         patch('api.services.billing.manualBillingRepository.getManualBillingRepository', return_value=repo):
        assert MonthlyRenewalTask._enqueue(MonthlyRenewalTask.__new__(MonthlyRenewalTask), intent) is False
    with sqlTransaction(path) as db:
        events = db.execute("select metadata_json from billing_events where event_type='email.billing_intent.committed'").fetchall()
    assert not any(json.loads(row[0])['notificationType'] == kind for row in events)
