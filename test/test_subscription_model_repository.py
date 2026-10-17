import os
from concurrent.futures import ThreadPoolExecutor

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from api.services.subscriptions.subscriptionModel import SubscriptionAuthorityUnavailable
from test.subscriptionRuntimeFixtures import (  # noqa: F401
    NOW, account, runtimeDb, postgres, query, selection, renewal, capture,
)
from api.services.billing.manualBillingContracts import CheckoutRequest

pytestmark = pytest.mark.skipif(os.environ.get('RUN_MANUAL_BILLING_INTEGRATION') != '1',
    reason='Guarded disposable PostgreSQL opt-in; skip is UNVERIFIED')


def test_mutable_mode_experts_and_pending_arrays_cannot_override_purchased_coverage(account):
    repo, model, intent, url = account
    query(url, "UPDATE subscriptions SET billing_mode='annual_prepaid',subscribed_experts='[\"telecom\"]',pending_removals='[\"banking\"]' WHERE user_id=%s", (intent.userId,))
    snapshot = repo.getCoverageSnapshot(intent.userId, NOW)
    assert snapshot.billingMode == 'monthly_prepaid'
    assert snapshot.currentPeriod.domains == ('banking', 'manufacturing')
    assert snapshot.currentPeriod.invoiceId == intent.invoiceId


def test_missing_finalized_capture_cannot_grant_paid_access(account):
    repo, model, intent, url = account
    query(url, "UPDATE billing_events SET event_status='OBSERVED' WHERE invoice_id=%s AND event_type='payment.capture'", (intent.invoiceId,))
    with pytest.raises(SubscriptionAuthorityUnavailable):
        repo.getCoverageSnapshot(intent.userId, NOW)


def test_quote_freezes_selection_created_before_invoice_and_ignores_array_tampering(account):
    repo, model, intent, url = account
    selected = selection(account, ['banking'])
    query(url, "UPDATE subscriptions SET pending_removals='[]' WHERE user_id=%s", (intent.userId,))
    invoice = renewal(account)
    frozen = invoice['metadata_json']['manualBilling']
    assert frozen['domains'] == ['banking']
    assert frozen['selectionEventId'] == selected['id']
    assert query(url, 'SELECT invoice_id FROM billing_events WHERE id=%s', (selected['id'],), one=True)['invoice_id'] is None
    assert repo.getCoverageSnapshot(intent.userId, NOW).currentPeriod.domains == ('banking', 'manufacturing')


def test_paid_future_selection_is_immutable(account):
    repo, model, intent, url = account
    invoice = renewal(account)
    attempt = repo.reserveCheckout(CheckoutRequest(intent.userId, 'renewal', 'monthly_prepaid',
        {'invoiceId': str(invoice['id'])}, expectedCatalogRevision=1))
    assert capture(repo, attempt)[0].state == 'paid_scheduled'
    with pytest.raises(ValueError, match='PAID_FUTURE_SELECTION_IMMUTABLE'):
        selection(account, ['banking'])


def test_concurrent_selection_writers_preserve_one_revision(account):
    def write(_):
        try:
            return selection(account, ['banking'])
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, range(2)))
    assert any(isinstance(r, dict) for r in results)
    repo, model, intent, url = account
    rows = query(url, "SELECT * FROM billing_events WHERE user_id=%s AND event_type='subscription.next_selection'", (intent.userId,))
    assert len(rows) == 1


@pytest.mark.parametrize('change', ['owner', 'order', 'currency'])
def test_accepted_period_loader_rejects_capture_identity_mismatch(account, change):
    repo, model, intent, url = account
    statements = {'owner': 'UPDATE billing_events SET user_id=NULL',
        'order': "UPDATE billing_events SET provider_order_id='wrong'",
        'currency': "UPDATE billing_events SET currency='USD'"}
    query(url, statements[change] + " WHERE invoice_id=%s AND event_type='payment.capture'", (intent.invoiceId,))
    subscriptionId = query(url, 'SELECT id FROM subscriptions WHERE user_id=%s', (intent.userId,), one=True)['id']
    with psycopg2.connect(url) as db, db.cursor(cursor_factory=RealDictCursor) as cursor:
        with pytest.raises(SubscriptionAuthorityUnavailable):
            model.loadAcceptedPeriods(cursor, str(subscriptionId), intent.lifecycleId)


@pytest.mark.parametrize('statement', ["UPDATE billing_events SET metadata_json='{}'", 'DELETE FROM billing_events'])
def test_committed_next_selection_is_immutable(account, statement):
    selected = selection(account, ['banking'])
    with pytest.raises(psycopg2.errors.RaiseException, match='immutable'):
        query(account[3], statement + ' WHERE id=%s', (selected['id'],))


def test_monthly_preparation_uses_owned_context_even_with_stale_input_row(account):
    from api.services.billing.monthlyCoverageService import MonthlyCoverageService
    repo, model, intent, url = account
    selection(account, ['banking'])
    stale = query(url, 'SELECT * FROM subscriptions WHERE user_id=%s', (intent.userId,), one=True)
    stale.update(subscribed_experts=['telecom'], pending_removals=[], current_period_end=None)
    result = MonthlyCoverageService(factsRepository=repo, now=lambda: NOW).prepareRenewalInvoice(intent.userId, stale, NOW)
    assert result['currentPeriod']['domains'] == ['banking', 'manufacturing']
    assert result['nextPeriod']['domains'] == ['banking']


def test_renewal_reservation_rejects_stale_selection_event_even_for_same_experts(account):
    repo, model, intent, url = account
    selection(account, ['banking'])
    invoice = renewal(account)
    query(url, "UPDATE \"Invoices\" SET metadata_json=jsonb_set(metadata_json,'{manualBilling,selectionEventId}','\"invalid-selection\"') WHERE id=%s", (invoice['id'],))
    with pytest.raises(ValueError, match='SELECTION|REVISION'):
        repo.reserveCheckout(CheckoutRequest(intent.userId, 'renewal', 'monthly_prepaid',
            {'invoiceId': str(invoice['id'])}, expectedCatalogRevision=1))


@pytest.mark.parametrize('account', ['annual_prepaid'], indirect=True)
def test_annual_quote_uses_owned_current_and_next_experts(account):
    repo, model, intent, url = account
    selected = selection(account, ['banking'])
    query(url, "UPDATE subscriptions SET subscribed_experts='[\"telecom\"]',pending_removals='[]' WHERE user_id=%s", (intent.userId,))
    invoice = repo.createAnnualRenewalInvoice(intent.userId)
    assert invoice['metadata_json']['manualBilling']['domains'] == ['banking']
    assert invoice['metadata_json']['manualBilling']['selectionEventId'] == selected['id']
    assert repo.getCoverageSnapshot(intent.userId, NOW).currentPeriod.domains == ('banking', 'manufacturing')
    assert repo.prepareAnnualRenewalInvoice(str(invoice['id']))['status'] == 'PAYMENT_PENDING'


@pytest.mark.parametrize('change', ['cycle', 'lifecycle', 'version'])
def test_selection_rejects_changed_cycle_lifecycle_or_version_without_writes(account, change):
    from datetime import timedelta
    repo, model, intent, url = account
    row = query(url, 'SELECT * FROM subscriptions WHERE user_id=%s', (intent.userId,), one=True)
    lifecycle = intent.lifecycleId if change != 'lifecycle' else '10000000-0000-4000-8000-000000000009'
    cycle = row['current_period_end'] + (timedelta(seconds=1) if change == 'cycle' else timedelta())
    version = row['version'] + (1 if change == 'version' else 0)
    with psycopg2.connect(url) as db, db.cursor(cursor_factory=RealDictCursor) as cursor:
        with pytest.raises(ValueError, match='CYCLE|VERSION'):
            model.saveNextSelection(cursor, str(row['id']), lifecycle, cycle, version, ['banking'], now=NOW)
    assert query(url, 'SELECT version FROM subscriptions WHERE id=%s', (row['id'],), one=True)['version'] == row['version']
    assert not query(url, "SELECT id FROM billing_events WHERE user_id=%s AND event_type='subscription.next_selection'", (intent.userId,))


def test_banned_owner_cannot_save_next_selection(account):
    from uuid import uuid4
    admin = str(uuid4())
    query(account[3], "INSERT INTO admin_users(id,email,name,password_hash) VALUES(%s,%s,'Synthetic reviewer','test-only')",
        (admin, admin + '@example.test'))
    query(account[3], 'UPDATE "Users" SET "isBanned"=true,"bannedAt"=%s,"bannedBy"=%s WHERE "userId"=%s',
        (NOW, admin, account[2].userId))
    with pytest.raises(ValueError, match='OWNER_NOT_ELIGIBLE'):
        selection(account, ['banking'])
