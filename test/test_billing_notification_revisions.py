"""Exercise delivery revisions using actual repository SQL (PG races separately)."""
import json
import pytest
from test.test_manual_billing_runtime import database, SqlConnection, SqlCursor, sqlTransaction, USER, SUB, NOW
from api.services.notifications.notificationDeliveryRepository import NotificationDeliveryRepository


@pytest.fixture
def deliveries(database):
    _, path = database
    with sqlTransaction(path) as db:
        db.executescript('''CREATE TABLE notification_deliveries (
          id TEXT PRIMARY KEY DEFAULT 'delivery-one', notification_type TEXT,
          template_version TEXT, dedupe_key TEXT UNIQUE, user_id TEXT,
          subscription_id TEXT, period_end TEXT, metadata_json TEXT,
          status TEXT DEFAULT 'PENDING', payload_version INTEGER DEFAULT 1,
          claimed_payload_version INTEGER, submission_started_at TEXT,
          provider_message_id TEXT, provider TEXT, provider_status TEXT, accepted_at TEXT, last_error_code TEXT,
          next_attempt_at TEXT, next_reconcile_at TEXT, lease_owner TEXT,
          lease_expires_at TEXT, terminal_at TEXT, updated_at TEXT
        );''')
    return NotificationDeliveryRepository(lambda: SqlConnection(path)), path


def enqueue(repo, invoice):
    return repo.enqueueBillingNotification(USER, SUB, 'monthly_renewal_ready',
        'monthly:life:cycle:ready', NOW.isoformat(), {'invoiceId': invoice})


def test_reprice_updates_queued_milestone_invoice(deliveries):
    repo, _ = deliveries
    first, created = enqueue(repo, 'old')
    second, created = enqueue(repo, 'replacement')
    assert second['dedupe_key'] == first['dedupe_key']
    assert json.loads(second['metadata_json'])['invoiceId'] == 'replacement'
    assert second['payload_version'] == 2
    assert not created


@pytest.mark.parametrize('status,error,submitted', [
    ('ACCEPTED', None, NOW.isoformat()), ('DELIVERED', None, NOW.isoformat()),
    ('RETRY_PENDING', 'AMBIGUOUS_SEND', NOW.isoformat()),
    ('SENDING', None, NOW.isoformat()), ('CANCELLED', 'USER_ERASED', None),
])
def test_accepted_or_ambiguous_milestone_is_immutable(deliveries,status,error,submitted):
    repo, path = deliveries
    enqueue(repo, 'original')
    with sqlTransaction(path) as db:
        db.execute('UPDATE notification_deliveries SET status=?,last_error_code=?,submission_started_at=?',
                   (status,error,submitted))
    row, _ = enqueue(repo, 'replacement')
    assert json.loads(row['metadata_json'])['invoiceId'] == 'original'
    assert row['payload_version'] == 1


def test_resume_reenables_eligible_undelivered_milestone(deliveries):
    repo, path = deliveries
    enqueue(repo, 'void')
    with sqlTransaction(path) as db:
        db.execute("UPDATE notification_deliveries SET status='CANCELLED',last_error_code='SUBSCRIPTION_NOT_ELIGIBLE'")
    row, _ = enqueue(repo, 'resumed')
    assert row['status'] == 'PENDING'
    assert json.loads(row['metadata_json'])['invoiceId'] == 'resumed'


def test_stale_claim_cannot_acknowledge_revision(deliveries, monkeypatch):
    original = SqlCursor.execute
    monkeypatch.setattr(SqlCursor, 'execute', lambda self, query, params=(): original(self, query.replace(" + interval '5 minutes'", ''), params))
    repo, path = deliveries
    enqueue(repo, 'old')
    with sqlTransaction(path) as db:
        db.execute("UPDATE notification_deliveries SET status='SENDING',lease_owner='worker',claimed_payload_version=1")
    row, _ = enqueue(repo, 'replacement')
    assert row['payload_version'] == 2
    assert not repo.markAccepted(row['id'], 'worker', 'message', NOW.isoformat(), payloadVersion=1)


@pytest.mark.parametrize('erased', [False, True])
def test_submission_fence_rechecks_privacy_and_cannot_submit_twice(deliveries, erased):
    repo, path = deliveries
    row, _ = repo.enqueueBillingNotification(USER,SUB,'payment_receipt','receipt:pay',NOW.isoformat(),{'amount':3000})
    with sqlTransaction(path) as db:
        db.execute("UPDATE notification_deliveries SET status='SENDING',lease_owner='worker',claimed_payload_version=1")
        db.execute('UPDATE subscriptions SET erasure_pending=?',(erased,))
    assert repo.authorizeBillingSubmission(row['id'],'worker',1) is (not erased)
    assert not repo.authorizeBillingSubmission(row['id'],'worker',1)


def test_dispatch_racing_reprice_cannot_submit_stale_claim(deliveries):
    repo, path = deliveries
    row, _ = enqueue(repo,'old')
    with sqlTransaction(path) as db:
        db.execute("UPDATE notification_deliveries SET status='SENDING',lease_owner='worker',claimed_payload_version=1")
    enqueue(repo,'new')
    assert not repo.authorizeBillingSubmission(row['id'],'worker',1)
