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
