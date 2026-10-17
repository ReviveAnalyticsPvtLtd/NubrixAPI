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
