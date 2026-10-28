from copy import deepcopy
from datetime import timedelta

import pytest

from api.services.subscriptions.subscriptionModelBackfill import (
    buildBackfillProposal, legacyRowHash, factsFromRecords,
)
from api.services.subscriptions.subscriptionModel import SubscriptionAuthorityUnavailable, evaluateSubscription
from test.subscriptionBackfillFixtures import NOW, source


@pytest.mark.parametrize('condition,resolution,plan', [
    ('none', 'verified', None), ('trial', 'verified', 'pro'), ('paid', 'verified', 'pro'),
])
def test_only_proven_facts_are_promoted_without_mutating_financial_records(condition, resolution, plan):
    records = source(condition)
    before = deepcopy(records)
    proposal = buildBackfillProposal(*records, now=NOW)
    assert proposal['resolution'] == resolution and proposal['patch']['plan_code'] == plan
    assert set(proposal['patch']) == {'plan_code', 'lifecycle_id', 'trial_started_at',
        'trial_ends_at', 'trial_closed_at', 'cancellation_requested_at'}
    assert records == before
    assert proposal['legacyHash'] == legacyRowHash(records[0])


@pytest.mark.parametrize('field,value', [('razorpayPaymentId', None), ('period_end', None), ('userId', 'another-owner')])
def test_paid_label_without_complete_owned_capture_evidence_requires_review(field, value):
    row, owner, invoices, events = source('paid')
    invoices[0][field] = value
    result = buildBackfillProposal(row, owner, invoices, events, now=NOW)
    assert result['resolution'] == 'needs_review' and result['patch'] is None


def test_missing_capture_and_legacy_annual_without_frozen_metadata_are_not_invented():
    row, owner, invoices, events = source('paid')
    assert buildBackfillProposal(row, owner, invoices, [], now=NOW)['resolution'] == 'needs_review'
    invoices[0]['metadata_json'] = {}
    row.update(billing_mode='annual_prepaid', plan_type='annual')
    assert buildBackfillProposal(row, owner, invoices, events, now=NOW)['resolution'] == 'needs_review'


def test_consumed_trial_without_dates_requires_reviewed_consumed_proof():
    row, owner, invoices, events = source()
    row.update(status='expired', plan_type='free')
    row['billing_state']['trialConsumed'] = True
    assert buildBackfillProposal(row, owner, invoices, events, now=NOW)['resolution'] == 'needs_review'
    result = buildBackfillProposal(row, owner, invoices, events, now=NOW,
        trialReview={'consumed': True, 'reviewReference': 'synthetic-reviewed-case'})
    assert result['resolution'] == 'consumed_dates_unknown'
    assert result['trialConsumed'] and result['patch']['trial_started_at'] is None
    facts = factsFromRecords(row | result['patch'], owner, invoices, events, consumed=True)
    snapshot = evaluateSubscription(facts, NOW)
    assert snapshot.status == 'expired' and not snapshot.trialAvailable
    assert snapshot.purchaseAllowed and snapshot.trial is None


def test_unknown_date_review_cannot_claim_an_active_trial_without_proven_dates():
    row, owner, invoices, events = source('trial')
    row['billing_state'].pop('trialConsumedAt')
    proposal = buildBackfillProposal(row, owner, invoices, events, now=NOW,
        trialReview={'consumed': True, 'reviewReference': 'synthetic-reviewed-case'})
    assert proposal['resolution'] == 'needs_review'


def test_placeholder_with_another_historical_row_is_not_assumed_trial_eligible():
    records = source()
    historical = records[0] | {'id': '10000000-0000-4000-8000-000000000009', 'is_canonical': False}
    assert buildBackfillProposal(*records, now=NOW, otherSubscriptions=[historical])['resolution'] == 'needs_review'


def test_hash_preserves_unknown_json_and_detects_changes_but_ignores_new_columns():
    row = source()[0]
    original = legacyRowHash(row)
    assert legacyRowHash(row | {'plan_code': 'pro', 'trial_started_at': NOW}) == original
    row['billing_state']['unknown']['keep'].append(3)
    assert legacyRowHash(row) != original


def test_valid_trial_uses_actual_consumption_start_and_audited_deadline():
    records = source('trial')
    records[0]['current_period_start'] += timedelta(days=2)  # An admin restart does not erase first consumption.
    proposal = buildBackfillProposal(*records, now=NOW)
    assert proposal['patch']['trial_started_at'] == '2026-10-10T10:00:00+00:00'
    assert proposal['patch']['trial_ends_at'] == '2026-10-22T10:00:00+00:00'


def test_expert_addition_authority_waits_for_the_owned_grant_migration():
    row, owner, invoices, events = source('paid')
    invoices.append(invoices[0] | {'id': 'addition', 'billing_reason': 'expert_addition',
        'metadata_json': {'manualBilling': {'purpose': 'expert_addition'}}})
    assert buildBackfillProposal(row, owner, invoices, events, now=NOW)['resolution'] == 'needs_review'


def test_unknown_money_cannot_be_projected_as_a_fresh_purchase():
    row, owner, invoices, events = source()
    events.append(dict(id='attempt', user_id=owner['userId'], subscription_id=row['id'],
                       invoice_id='invoice', event_category='payment_attempt', event_type='checkout',
                       event_status='created', payment_status='pending_provider_ack', metadata_json={}))
    proposal = buildBackfillProposal(row, owner, invoices, events, now=NOW)
    facts = factsFromRecords(row | proposal['patch'], owner, invoices, events, consumed=False)
    snapshot = evaluateSubscription(facts, NOW)
    assert snapshot.status == 'none' and not snapshot.purchaseAllowed
    assert snapshot.purchaseBlockReason == 'recovery_required'


def test_inventory_and_backfill_do_not_fall_back_to_application_dsn(monkeypatch, capsys):
    from scripts.subscription_model_inventory import main as inventory
    from scripts.subscription_model_backfill import main as backfill
    monkeypatch.delenv('SUBSCRIPTION_MODEL_BACKFILL_DATABASE_URL', raising=False)
    monkeypatch.setenv('DATABASE_URL', 'postgresql://must-not-connect.invalid/production')
    assert inventory([]) == 2 and backfill([]) == 2
    assert 'SUBSCRIPTION_MODEL_BACKFILL_DATABASE_URL' in capsys.readouterr().err


def test_backfill_refuses_active_v2_phases_without_opening_a_connection(monkeypatch):
    from api.services.subscriptions.subscriptionModelRepository import SubscriptionModelRepository
    def forbidden():
        raise AssertionError('Must refuse before opening a database connection')
    approved = buildBackfillProposal(*source(), now=NOW) | {'reviewedBy': 'operator', 'reviewReference': 'review'}
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE', 'v2')
    with pytest.raises(ValueError, match='REVIEW_PHASE'):
        SubscriptionModelRepository(forbidden).applyReviewedDecision(approved)


def test_historical_row_consumption_cannot_be_reviewed_without_a_proof_reference():
    records = source()
    historical = records[0] | {'id': '10000000-0000-4000-8000-000000000009', 'is_canonical': False}
    proposal = buildBackfillProposal(*records, now=NOW, otherSubscriptions=[historical], trialReview={'consumed': True})
    assert proposal['resolution'] == 'needs_review'


@pytest.mark.parametrize('status', ['failed', 'expired', 'cancelled'])
def test_terminal_attempt_label_without_closure_proof_does_not_allow_a_second_purchase(status):
    row, owner, invoices, events = source()
    attempt = dict(id='attempt', user_id=owner['userId'], subscription_id=row['id'], invoice_id='invoice',
        event_category='payment_attempt', event_type='checkout', payment_status=status,
        provider_order_id='old-order', metadata_json={})
    proposal = buildBackfillProposal(row, owner, invoices, [attempt], now=NOW)
    facts = factsFromRecords(row | proposal['patch'], owner, invoices, [attempt], consumed=False)
    snapshot = evaluateSubscription(facts, NOW)
    assert not snapshot.purchaseAllowed and snapshot.purchaseBlockReason == 'recovery_required'
    attempt['metadata_json'] = {'manualBilling': {'closedAt': NOW.isoformat(), 'closureReconciledAt': NOW.isoformat()}}
    cleared = factsFromRecords(row | proposal['patch'], owner, invoices, [attempt], consumed=False)
    assert evaluateSubscription(cleared, NOW).purchaseAllowed


def test_empty_subscription_labels_with_credit_history_require_review():
    row, owner, invoices, events = source()
    owner['creditHistory'] = True
    assert buildBackfillProposal(row, owner, invoices, events, now=NOW)['resolution'] == 'needs_review'


@pytest.mark.parametrize('churned', [False, True])
def test_reviewed_consumed_proof_survives_missing_legacy_markers(churned):
    row, owner, invoices, events = source()
    if churned:
        row['billing_state']['churn_snapshot'] = {'historical': True}
        row['status'] = 'expired'
    proposal = buildBackfillProposal(row, owner, invoices, events, now=NOW,
        trialReview={'consumed': True, 'reviewReference': 'reviewed-original-trial'})
    assert proposal['resolution'] == 'consumed_dates_unknown'
    facts = factsFromRecords(row | proposal['patch'], owner, invoices, events, consumed=proposal['trialConsumed'])
    snapshot = evaluateSubscription(facts, NOW)
    assert snapshot.status == 'expired' and not snapshot.trialAvailable and snapshot.purchaseAllowed


@pytest.mark.parametrize('condition,eventType', [('paid', 'credit.usage_reported'), ('trial', 'free_trial.activated')])
def test_owner_scoped_audits_do_not_disqualify_proven_service(condition, eventType):
    row, owner, invoices, events = source(condition)
    events.append(dict(id='owned-audit', user_id=owner['userId'], subscription_id=None,
        event_category='audit', event_type=eventType, event_status='SETTLED', metadata_json={}))
    proposal = buildBackfillProposal(row, owner, invoices, events, now=NOW)
    assert proposal['resolution'] == 'verified'
    facts = factsFromRecords(row | proposal['patch'], owner, invoices, events, consumed=proposal['trialConsumed'])
    assert evaluateSubscription(facts, NOW).accessAllowed
    events[-1]['user_id'] = 'different-owner'
    assert buildBackfillProposal(row, owner, invoices, events, now=NOW)['resolution'] == 'needs_review'


@pytest.mark.parametrize('shape', [None, [], 'unstructured-original'])
@pytest.mark.parametrize('location', ['state', 'invoice', 'event'])
def test_malformed_nested_metadata_becomes_an_inspectable_discrepancy(location, shape):
    row, owner, invoices, events = source('paid')
    target = row['billing_state'] if location == 'state' else invoices[0]['metadata_json'] if location == 'invoice' else events[0]['metadata_json']
    target['manualBilling'] = shape
    proposal = buildBackfillProposal(row, owner, invoices, events, now=NOW)
    assert proposal['resolution'] == 'needs_review' and proposal['patch'] is None
    assert proposal['reasons'] == ['INVALID_MANUAL_BILLING_METADATA']


def test_historical_subscription_money_is_an_account_guard_and_review_fence():
    from api.services.subscriptions.subscriptionModelRepository import SubscriptionModelRepository
    row, owner, _, _ = source()
    old = row | {'id': '10000000-0000-4000-8000-000000000009', 'is_canonical': False}
    invoice = dict(id='historical-invoice', userId=owner['userId'], subscription_id=old['id'], status='PAYMENT_PENDING')
    attempt = dict(id='historical-attempt', user_id=owner['userId'], subscription_id=old['id'],
        invoice_id=invoice['id'], event_category='payment_attempt', event_type='checkout',
        payment_status='authorized', metadata_json={})
    invoices, events, others = SubscriptionModelRepository._related(row, [row, old], [invoice], [attempt])
    review = {'consumed': True, 'reviewReference': 'reviewed-history'}
    proposal = buildBackfillProposal(row, owner, invoices, events, now=NOW, otherSubscriptions=others, trialReview=review)
    assert proposal['resolution'] == 'consumed_dates_unknown'
    empty = buildBackfillProposal(row, owner, [], [], now=NOW, otherSubscriptions=others, trialReview=review)
    assert proposal['evidenceHash'] != empty['evidenceHash']
    facts = factsFromRecords(row | proposal['patch'], owner, invoices, events,
        consumed=True, otherSubscriptions=others)
    snapshot = evaluateSubscription(facts, NOW)
    assert not snapshot.purchaseAllowed and snapshot.purchaseBlockReason == 'recovery_required'
    attempt['payment_status'] = 'failed'
    attempt['metadata_json'] = {'manualBilling': {'closedAt': NOW.isoformat(), 'closureReconciledAt': NOW.isoformat()}}
    currentInvoices, currentEvents, _ = SubscriptionModelRepository._related(row, [row, old], [invoice], [attempt])
    changed = buildBackfillProposal(row, owner, currentInvoices, currentEvents, now=NOW, otherSubscriptions=others, trialReview=review)
    assert changed['evidenceHash'] != proposal['evidenceHash']
    cleared = factsFromRecords(row | changed['patch'], owner, currentInvoices, currentEvents, consumed=True, otherSubscriptions=others)
    assert evaluateSubscription(cleared, NOW).purchaseAllowed


@pytest.mark.parametrize('coverage', ['current', 'future', 'unproven', 'elapsed'])
def test_reviewed_trial_consumption_cannot_discard_other_owned_paid_service(coverage):
    row, owner, _, _ = source()
    old, _, invoices, events = source('paid')
    old['id'] = '10000000-0000-4000-8000-000000000009'
    old['is_canonical'] = False
    invoices[0]['subscription_id'] = old['id']
    events[0]['subscription_id'] = old['id']
    if coverage == 'future':
        invoices[0]['period_start'] = NOW + timedelta(days=1)
    elif coverage == 'elapsed':
        invoices[0]['period_start'] = NOW - timedelta(days=32)
        invoices[0]['period_end'] = NOW - timedelta(days=1)
    elif coverage == 'unproven':
        invoices[0]['period_end'] = None
    proposal = buildBackfillProposal(row, owner, invoices, events, now=NOW, otherSubscriptions=[old],
        trialReview={'consumed': True, 'reviewReference': 'trial-consumption-only'})
    if coverage != 'elapsed':
        assert proposal['resolution'] == 'needs_review' and proposal['patch'] is None
    else:
        assert proposal['resolution'] == 'consumed_dates_unknown'
        facts = factsFromRecords(row | proposal['patch'], owner, invoices, events, consumed=True, otherSubscriptions=[old])
        snapshot = evaluateSubscription(facts, NOW)
        assert snapshot.status == 'expired' and snapshot.purchaseAllowed and snapshot.currentPeriod is None


@pytest.mark.parametrize('field,value', [('pending_removals', ['banking']),
    ('pending_additions', [{'domain': 'telecom', 'state': 'awaiting_payment', 'attemptId': 'missing'}])])
def test_legacy_selection_without_owned_destination_is_reported_unresolved(field, value):
    row, owner, invoices, events = source('paid')
    row[field] = value
    assert buildBackfillProposal(row, owner, invoices, events, now=NOW)['resolution'] == 'needs_review'
