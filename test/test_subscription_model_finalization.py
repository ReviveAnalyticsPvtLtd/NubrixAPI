import os
import pytest

from api.services.billing.manualBillingContracts import CheckoutRequest, VerifiedPaymentEvidence
from test.subscriptionRuntimeFixtures import (  # noqa: F401
    NOW, account, runtimeDb, postgres, query, selection, renewal, capture,
)

pytestmark = pytest.mark.skipif(os.environ.get('RUN_MANUAL_BILLING_INTEGRATION') != '1',
    reason='Guarded disposable PostgreSQL opt-in; skip is UNVERIFIED')


def test_captured_addition_uses_owned_attempt_and_original_period_preserving_usage(account):
    from api.services.credits.creditConfig import getTokenQuotaForPlan
    repo, model, intent, url = account
    query(url, 'UPDATE credit_balances SET used_tokens=700,remaining_tokens=%s,topup_tokens=500 WHERE user_id=%s',
        (getTokenQuotaForPlan('pro', 2) - 700, intent.userId))
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'monthly_prepaid',
        {'domains': ['telecom', 'supplychain']}, expectedCatalogRevision=1))
    # The pending array is a compatibility projection, never the grant authority.
    query(url, "UPDATE subscriptions SET subscribed_experts='[\"telecom\"]',pending_additions='[]' WHERE user_id=%s", (intent.userId,))
    result, evidence = capture(repo, addition)
    assert result.state == 'expert_activated'
    assert set(repo.getCoverageSnapshot(intent.userId, NOW).currentPeriod.domains) == {'banking', 'manufacturing', 'telecom', 'supplychain'}
    credit = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (intent.userId,), one=True)
    assert (credit['used_tokens'], credit['remaining_tokens'], credit['topup_tokens']) == (700, getTokenQuotaForPlan('pro', 4) - 700, 500)
    repo.finalizeCapturedPayment(evidence)
    assert query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (intent.userId,), one=True) == credit
    grant = query(url, "SELECT metadata_json FROM billing_events WHERE user_id=%s AND event_type='subscription.expert_grant'", (intent.userId,), one=True)
    assert grant['metadata_json']['expertGrant']['periodId'] == intent.invoiceId


def test_closing_one_addition_closes_owned_unpaid_bundle_without_pending_array(account):
    repo, model, intent, url = account
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'monthly_prepaid',
        {'domains': ['telecom', 'supplychain']}, expectedCatalogRevision=1))
    repo.bindProviderOrder(addition.attemptId, {'id': 'order-close-' + addition.attemptId})
    query(url, "UPDATE subscriptions SET pending_additions='[]' WHERE user_id=%s", (intent.userId,))
    result = repo.cancelExpertAddition(intent.userId, 'telecom')
    assert set(result['closedAttemptDomains']) == {'telecom', 'supplychain'}
    assert query(url, 'SELECT status FROM "Invoices" WHERE id=%s', (addition.invoiceId,), one=True)['status'] == 'VOID'
    assert repo.getCoverageSnapshot(intent.userId, NOW).currentPeriod.domains == ('banking', 'manufacturing')


def test_paid_future_experts_do_not_inherit_current_addition(account):
    repo, model, intent, url = account
    invoice = renewal(account)
    nextAttempt = repo.reserveCheckout(CheckoutRequest(intent.userId, 'renewal', 'monthly_prepaid',
        {'invoiceId': str(invoice['id'])}, expectedCatalogRevision=1))
    capture(repo, nextAttempt)
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'monthly_prepaid',
        {'domains': ['telecom']}, expectedCatalogRevision=1))
    capture(repo, addition)
    snapshot = repo.getCoverageSnapshot(intent.userId, NOW)
    assert 'telecom' in snapshot.currentPeriod.domains
    assert snapshot.nextPeriod.domains == ('banking', 'manufacturing')


def test_addition_cannot_move_to_a_later_service_period(account):
    repo, model, intent, url = account
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'monthly_prepaid',
        {'domains': ['telecom']}, expectedCatalogRevision=1))
    end = query(url, 'SELECT current_period_end FROM subscriptions WHERE user_id=%s', (intent.userId,), one=True)['current_period_end']
    result, evidence = capture(repo, addition, now=end)
    assert result.state == 'requires_reconciliation'
    assert not query(url, "SELECT id FROM billing_events WHERE user_id=%s AND event_type='subscription.expert_grant'", (intent.userId,))


def test_accepted_addition_preserves_scheduled_removal_and_updates_only_unpaid_selection(account):
    repo, model, intent, url = account
    selection(account, ['banking'])
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'monthly_prepaid',
        {'domains': ['telecom']}, expectedCatalogRevision=1))
    capture(repo, addition)
    assert repo.getRenewalContext(intent.userId, NOW)['_verifiedCoverage']['nextExperts'] == ['banking', 'telecom']


@pytest.mark.parametrize('field,value', [('servicePeriodId', '10000000-0000-4000-8000-000000000009'),
    ('lifecycleId', '10000000-0000-4000-8000-000000000009'), ('revision', 2)])
def test_addition_capture_rejects_changed_owned_quote_identity(account, field, value):
    import json
    repo, model, intent, url = account
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'monthly_prepaid',
        {'domains': ['telecom']}, expectedCatalogRevision=1))
    query(url, 'UPDATE "Invoices" SET metadata_json=jsonb_set(metadata_json,%s,%s::jsonb) WHERE id=%s',
        (['manualBilling', field], json.dumps(value), addition.invoiceId))
    result, evidence = capture(repo, addition)
    assert result.state == 'requires_reconciliation'
    assert not query(url, "SELECT id FROM billing_events WHERE user_id=%s AND event_type='subscription.expert_grant'", (intent.userId,))


def test_capture_and_selection_race_cannot_edit_purchased_future_experts(account):
    from concurrent.futures import ThreadPoolExecutor
    repo, model, intent, url = account
    invoice = renewal(account)
    attempt = repo.reserveCheckout(CheckoutRequest(intent.userId, 'renewal', 'monthly_prepaid',
        {'invoiceId': str(invoice['id'])}, expectedCatalogRevision=1))
    # The capture callback already owns an Order; race finalization against the
    # preference write, independently of provider Order creation/binding.
    order = 'order-race-' + attempt.attemptId
    repo.bindProviderOrder(attempt.attemptId, {'id': order})
    evidence = VerifiedPaymentEvidence(attempt.attemptId, attempt.invoiceId, attempt.userId, order,
        'pay-race-' + attempt.attemptId, attempt.purpose, attempt.currency, 'captured',
        'server_observation', attempt.amount, NOW, None, None, False)
    def change():
        try:
            return selection(account, ['banking'])
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        captured, selected = pool.submit(repo.finalizeCapturedPayment, evidence), pool.submit(change)
        result = captured.result()
        selected.result()
    snapshot = repo.getCoverageSnapshot(intent.userId, NOW)
    if result.state == 'paid_scheduled':
        assert snapshot.nextPeriod.domains == ('banking', 'manufacturing')
    else:
        assert result.state == 'requires_reconciliation' and snapshot.nextPeriod is None


@pytest.mark.parametrize('account', ['annual_prepaid'], indirect=True)
def test_annual_addition_does_not_rewrite_original_service_quote(account):
    repo, model, intent, url = account
    before = query(url, 'SELECT metadata_json,total_amount,razorpay_order_id,"razorpayPaymentId" FROM "Invoices" WHERE id=%s', (intent.invoiceId,), one=True)
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'annual_prepaid',
        {'domains': ['telecom']}, expectedCatalogRevision=1))
    assert capture(repo, addition)[0].state == 'expert_activated'
    assert query(url, 'SELECT metadata_json,total_amount,razorpay_order_id,"razorpayPaymentId" FROM "Invoices" WHERE id=%s', (intent.invoiceId,), one=True) == before
    assert repo.getCoverageSnapshot(intent.userId, NOW).currentPeriod.domains == ('banking', 'manufacturing', 'telecom')


def test_explicit_addition_revocation_is_respected_without_touching_frozen_quote(account):
    repo, model, intent, url = account
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'monthly_prepaid',
        {'domains': ['telecom']}, expectedCatalogRevision=1))
    capture(repo, addition)
    import json
    query(url, 'UPDATE "Invoices" SET metadata_json=jsonb_set(metadata_json,\'{manualBilling,revokedAt}\',%s::jsonb) WHERE id=%s',
        (json.dumps(NOW.isoformat()), addition.invoiceId))
    assert repo.getCoverageSnapshot(intent.userId, NOW).currentPeriod.domains == ('banking', 'manufacturing')
