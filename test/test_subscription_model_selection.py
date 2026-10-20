import pytest

from api.services.subscriptions.subscriptionFieldUtils import subscriptionExperts, subscriptionRenewalExperts
from api.services.subscriptions.subscriptionModel import SubscriptionAuthorityUnavailable


def test_v2_helpers_use_verified_context_instead_of_subscription_arrays(monkeypatch):
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE', 'v2')
    row = {'subscribed_experts': ['telecom'], 'pending_removals': [],
        '_verifiedCoverage': {'experts': ['banking', 'manufacturing'], 'nextExperts': ['banking']}}
    assert subscriptionExperts(row) == ['banking', 'manufacturing']
    assert subscriptionRenewalExperts(row) == ['banking']


def test_v2_helpers_cannot_silently_fall_back_to_mutable_fields(monkeypatch):
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE', 'dual')
    with pytest.raises(SubscriptionAuthorityUnavailable):
        subscriptionRenewalExperts({'subscribed_experts': ['telecom']})


def test_purchase_eligibility_uses_the_same_verified_projection_as_snapshot():
    from api.services.subscriptions import subscriptionModel as model
    from test.subscriptionModelFixtures import factsFor, NOW
    facts = factsFor('expired')
    assert model.evaluatePurchaseEligibility(facts, NOW) == (True, None)
    assert model.evaluatePurchaseEligibility(factsFor('annual'), NOW) == (False, 'current_coverage')


def test_new_phase_access_helper_requires_verified_snapshot_instead_of_old_labels(monkeypatch):
    from api.services.subscriptions.paymentValidationService import isAccessActive
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE', 'v2')
    with pytest.raises(SubscriptionAuthorityUnavailable):
        isAccessActive({'status': 'active', 'current_period_end': '2099-01-01T00:00:00Z'})


def test_verified_access_helper_stops_at_exact_paid_end_even_with_stale_active_claims(monkeypatch):
    from api.services.subscriptions.paymentValidationService import isAccessActive, blocksNewCheckout
    from api.services.subscriptions.subscriptionModel import evaluateSubscription
    from test.subscriptionModelFixtures import factsFor, NOW
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE', 'v2')
    facts = factsFor('annual')
    snapshot = evaluateSubscription(facts, NOW)
    assert isAccessActive({'status': 'expired'}, NOW, snapshot=snapshot)
    assert not isAccessActive({'status': 'active'}, snapshot.currentPeriod.end, snapshot=snapshot)
    assert not blocksNewCheckout({'status': 'active'}, NOW, facts=factsFor('expired'))
