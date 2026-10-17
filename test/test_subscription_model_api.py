"""Public V2, compatibility and consistent read boundary regression tests."""
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi import HTTPException

from api.services.billing.manualBillingContracts import FinalizationResult
from api.services.billing.manualBillingPresentation import (
    serializeFinalizationResult, serializeSubscriptionSnapshot,
)
from api.services.subscriptions.subscriptionCompatibility import (
    getSubscriptionModelPhase, legacySubscriptionFields, resolveProBillingInput,
)
from api.services.subscriptions.subscriptionModel import (
    RecoverableCheckout, SubscriptionAuthorityUnavailable, evaluateSubscription,
)
from api.services.subscriptions.subscriptionSnapshotService import SubscriptionSnapshotService
from test.subscriptionModelFixtures import NOW, USER, factsFor


def test_public_v2_has_explicit_nulls_and_allowlists_period_without_owner_or_invoice():
    snapshot = evaluateSubscription(factsFor('annual'), NOW)
    payload = serializeSubscriptionSnapshot(snapshot)
    assert payload == {
        'schemaVersion': 2, 'subscriptionId': '00000000-0000-4000-8000-000000000001',
        'lifecycleId': '00000000-0000-4000-8000-000000000002',
        'planCode': 'pro', 'billingInterval': 'year', 'status': 'active',
        'accessAllowed': True, 'accessSource': 'paid', 'denialReason': None,
        'trialAvailable': False, 'trial': None,
        'currentPeriod': {'id': 'period-current', 'start': '2026-01-31T10:00:00+00:00',
                          'end': '2027-01-31T10:00:00+00:00',
                          'experts': ['banking', 'telecom'], 'billingInterval': 'year',
                          'creditPeriodId': None},
        'nextPeriod': None, 'paidUntil': '2027-01-31T10:00:00+00:00',
        'lastPaidEnd': '2027-01-31T10:00:00+00:00',
        'subscribedExperts': ['banking', 'telecom'], 'domainCount': 2,
        'renewalOptOut': False, 'renewalAvailable': True,
        'resumeRenewalAvailable': False, 'cancellationEffectiveAt': None,
        'purchaseAllowed': False, 'purchaseBlockReason': 'current_coverage',
        'recoverableCheckout': None, 'evaluatedAt': '2026-10-17T10:00:00+00:00',
    }
    payload['currentPeriod']['experts'].append('client-injected')
    assert snapshot.currentPeriod.experts == ('banking', 'telecom')


def test_expired_public_payload_never_fills_explicit_null_from_history():
    snapshot = evaluateSubscription(factsFor('expired'), NOW)
    payload = serializeSubscriptionSnapshot(snapshot)
    assert payload['paidUntil'] is None and payload['currentPeriod'] is None
    assert payload['lastPaidEnd'] == '2026-10-17T10:00:00+00:00'
    assert payload['purchaseAllowed'] and payload['status'] == 'expired'
    assert 'nextRefillAt' not in payload  # Credit ledger remains its authority.


def test_trial_and_recovery_fields_are_separate_from_subscription_status():
    facts = replace(factsFor('trial'), recoverableCheckout=RecoverableCheckout('invoice', 'attempt', 'processing'))
    payload = serializeSubscriptionSnapshot(evaluateSubscription(facts, NOW))
    assert payload['status'] == 'trial' and payload['accessAllowed']
    assert payload['trial'] == {'startedAt': '2026-10-10T10:00:00+00:00',
                                'endsAt': '2026-10-22T10:00:00+00:00', 'closedAt': None}
    assert payload['recoverableCheckout'] == {'invoiceId': 'invoice', 'attemptId': 'attempt', 'state': 'processing'}
    assert not payload['purchaseAllowed']


@pytest.mark.parametrize('condition,plan,status,mode', [
    ('none', 'none', 'NONE', 'none'), ('trial', 'free', 'TRIAL', 'none'),
    ('monthly', 'pro', 'ACTIVE', 'monthly_prepaid'),
    ('annual', 'annual', 'ACTIVE', 'annual_prepaid'),
    ('expired', 'none', 'EXPIRED', 'monthly_prepaid'),
])
def test_v1_labels_are_isolated_compatibility_aliases(condition, plan, status, mode):
    snapshot = evaluateSubscription(factsFor(condition), NOW)
    aliases = legacySubscriptionFields(snapshot)
    assert (aliases['planType'], aliases['status'], aliases['billingMode']) == (plan, status, mode)
    if snapshot.currentPeriod:
        assert aliases['currentPeriod']['domains'] == ['banking', 'telecom']
        assert 'experts' not in aliases['currentPeriod']
    assert serializeSubscriptionSnapshot(snapshot)['planCode'] == snapshot.planCode


def test_v1_paid_future_keeps_current_and_final_ends_distinct():
    snapshot = evaluateSubscription(replace(factsFor('paid_future'), renewalOptOut=True), NOW)
    aliases = legacySubscriptionFields(snapshot)
    assert aliases['planExpire'] == '2026-11-17T10:00:00+00:00'
    assert aliases['cancellationEffectiveEnd'] == '2026-12-17T10:00:00+00:00'
    assert aliases['nextPeriod']['domains'] == ['banking']
    assert aliases['autoRenewEnabled'] is False
    assert aliases['paymentCollectionMode'] == 'authenticated_checkout'


@pytest.mark.parametrize('payload,want', [
    ({'planCode': 'pro', 'billingInterval': 'month'}, ('pro', 'month')),
    ({'planCode': 'pro', 'billingInterval': 'year'}, ('pro', 'year')),
    ({'billingMode': 'monthly_prepaid'}, ('pro', 'month')),
    ({'billingMode': 'monthly_recurring'}, ('pro', 'month')),
    ({'billingMode': 'annual_prepaid'}, ('pro', 'year')),
    ({'planCode': 'pro', 'billingInterval': 'year', 'billingMode': 'annual_prepaid'}, ('pro', 'year')),
])
def test_checkout_input_adapter_preserves_manual_historical_aliases(payload, want):
    assert resolveProBillingInput(payload) == want
    assert 'autoRenewEnabled' not in payload


@pytest.mark.parametrize('payload', [
    {}, {'planCode': 'annual', 'billingInterval': 'year'},
    {'planCode': 'pro', 'billingInterval': 'annual'}, {'billingMode': 'silent_token'},
    {'planCode': 'pro', 'billingInterval': 'month', 'billingMode': 'annual_prepaid'},
    {'planCode': 'pro'}, {'billingInterval': 'year'},
    {'planCode': None, 'billingInterval': 'month'},
    {'planCode': 'pro', 'billingInterval': 'month', 'billingMode': None},
])
def test_invalid_or_conflicting_checkout_inputs_fail_422_without_guessing(payload):
    with pytest.raises(HTTPException) as caught:
        resolveProBillingInput(payload)
    assert caught.value.status_code == 422
    assert caught.value.detail['code'] == 'INVALID_SUBSCRIPTION_INPUT'


def test_phase_defaults_to_legacy_and_rejects_unknown_values(monkeypatch):
    monkeypatch.delenv('SUBSCRIPTION_MODEL_PHASE', raising=False)
    assert getSubscriptionModelPhase() == 'legacy'
    for phase in ('legacy', 'shadow', 'dual', 'v2', 'contracted'):
        monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE', phase)
        assert getSubscriptionModelPhase() == phase
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE', 'typo')
    with pytest.raises(ValueError, match='SUBSCRIPTION_MODEL_PHASE'):
        getSubscriptionModelPhase()


def test_snapshot_uses_loader_database_time_when_no_evaluation_time_supplied():
    facts = factsFor('monthly')
    database_time = facts.periods[0].end
    service = SubscriptionSnapshotService(lambda owner, now: (facts, database_time))
    snapshot = service.get(USER)
    assert snapshot.evaluatedAt == database_time and snapshot.status == 'expired'
    assert snapshot.purchaseAllowed


def test_snapshot_explicit_evaluation_uses_same_facts_transaction_time():
    service = SubscriptionSnapshotService(lambda owner, now: (factsFor('monthly'), now))
    assert service.get(USER, NOW).status == 'active'
    assert service.get(USER, factsFor('monthly').periods[0].end).status == 'expired'


@pytest.mark.parametrize('loader', [
    lambda owner, now: (factsFor('monthly'), NOW + timedelta(days=1)),
    lambda owner, now: (replace(factsFor('monthly'), userId='another-owner'), NOW),
    lambda owner, now: (None, NOW),
])
def test_snapshot_rejects_wrong_owner_missing_evidence_or_shifted_explicit_clock(loader):
    with pytest.raises(SubscriptionAuthorityUnavailable):
        SubscriptionSnapshotService(loader).get(USER, NOW)


def test_database_failure_remains_unavailable_instead_of_a_none_subscription():
    def broken_loader(owner, now):
        raise ConnectionError('database unavailable')
    with pytest.raises(SubscriptionAuthorityUnavailable) as caught:
        SubscriptionSnapshotService(broken_loader).get(USER)
    assert isinstance(caught.value.__cause__, ConnectionError)


def test_financial_result_adds_v2_only_when_supplied_and_preserves_original_envelope():
    args = dict(invoiceId='invoice-current', attemptId='attempt', state='activated',
                creditState='ready', finalized=True, creditsRefilled=False,
                renewalOptOut=False, currentPeriod=None, nextPeriod=None, anomalyId=None)
    legacy = serializeFinalizationResult(FinalizationResult(**args))
    assert 'subscription' not in legacy
    snapshot = evaluateSubscription(factsFor('monthly'), NOW)
    result = serializeFinalizationResult(FinalizationResult(**args, subscription=snapshot))
    assert result.pop('subscription') == serializeSubscriptionSnapshot(snapshot)
    assert result == legacy
