"""Draft edits are owned selections, never financial submission instructions."""
import importlib
import importlib.util
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError


def implementation():
    name = 'api.services.billing.purchaseDraftService'
    assert importlib.util.find_spec(name), 'Owned draft service must exist'
    return importlib.import_module(name)


def test_disabled_editor_needs_no_database_and_allows_no_writes(monkeypatch):
    module = implementation()
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED', raising=False)
    repository = Mock(side_effect=AssertionError('Disabled feature must not require storage'))
    service = module.PurchaseDraftService(repository)
    result = asdict(service.get('owner'))
    assert result['enabled'] is False and result['userId'] == 'owner'
    assert result['draft']['revision'] == 0 and result['draft']['domains'] == ()
    assert result['capabilities']['mayEditDraft'] is False
    with pytest.raises(ValueError, match='EDITABLE_CHECKOUT_DISABLED'):
        service.save('owner', SimpleNamespace(expectedRevision=0, domains=['banking'], billingInterval='month'))
    assert not repository.mock_calls


@pytest.mark.parametrize('payload', [
    {'domains': []}, {'domains': ['banking'] * 2}, {'domains': ['unknown']},
    {'domains': ['banking'] * 5}, {'domains': [1]}, {'expectedRevision': True},
    {'expectedRevision': -1}, {'expectedRevision': '0'}, {'billingInterval': 'annual'},
    {'userId': 'foreign'}, {'contact': '+919999999999'},
])
def test_draft_request_rejects_invalid_or_unowned_fields(payload):
    import api.models as models
    assert hasattr(models, 'SavePurchaseDraftRequest'), 'Draft request validation must exist'
    with pytest.raises(ValidationError):
        models.SavePurchaseDraftRequest(**({'expectedRevision': 0, 'domains': ['banking'],
                                           'billingInterval': 'month'} | payload))


def test_four_domains_and_both_intervals_are_valid():
    import api.models as models
    assert hasattr(models, 'SavePurchaseDraftRequest'), 'Draft request validation must exist'
    for interval in ('month', 'year'):
        request = models.SavePurchaseDraftRequest(expectedRevision=0,
            domains=['banking', 'manufacturing', 'supplychain', 'telecom'], billingInterval=interval)
        assert len(request.domains) == 4 and request.billingInterval == interval


def test_draft_http_is_authenticated_private_and_gate_off_does_not_open_storage(checkout_client, monkeypatch):
    from test.test_manual_checkout_http import counts
    client, provider, path = checkout_client
    monkeypatch.delenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED', raising=False)
    before = counts(path)
    get = client.get('/purchase-draft')
    assert get.status_code == 200 and get.json()['enabled'] is False
    assert get.headers['cache-control'] == 'private, no-store'
    save = client.put('/purchase-draft', json={'expectedRevision': 0, 'domains': ['banking'], 'billingInterval': 'month'})
    assert save.status_code == 409 and save.json()['errorCode'] == 'EDITABLE_CHECKOUT_DISABLED'
    assert save.headers['cache-control'] == 'private, no-store'
    assert counts(path) == before and not provider.order.createdPayloads


def test_enabled_storage_error_is_sanitized_and_malformed_identity_is_rejected(checkout_client, monkeypatch):
    import os
    import sys
    from jose import jwt
    from api.commons import verifyToken
    client, _, _ = checkout_client
    module = implementation()
    monkeypatch.setenv('SUBSCRIPTION_EDITABLE_CHECKOUT_ENABLED', 'true')
    monkeypatch.setitem(sys.modules, 'utils.logger', SimpleNamespace(logger=Mock()))
    def unavailable(_):
        raise RuntimeError('private database connection detail')
    repository = SimpleNamespace(readEditor=unavailable)
    monkeypatch.setattr('api.services.billing.purchaseCheckoutRepository.getPurchaseCheckoutRepository', lambda: repository)
    response = client.get('/purchase-draft')
    assert response.status_code == 503 and response.json()['errorCode'] == 'PURCHASE_STORAGE_UNAVAILABLE'
    assert 'private database' not in response.text
    token = jwt.encode({'email': 'invalid@example.test'}, os.environ['SECRET_KEY'], algorithm='HS256')
    client.app.dependency_overrides[verifyToken] = lambda: token
    response = client.get('/purchase-draft')
    assert response.status_code == 401 and response.headers['cache-control'] == 'private, no-store'


from test.test_manual_checkout_http import checkout_client, checkout_database  # noqa: E402,F401
from test.test_manual_billing_runtime import database  # noqa: E402,F401
