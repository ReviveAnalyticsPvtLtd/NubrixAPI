from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.adminErrors import AdminApiError
from api.routers import admin as router_module
from api.services.adminAuthService import verifyAdmin
from api.services.adminPricingService import AdminPricingService, getAdminPricingService
from api.services.billing.pricingCatalogContracts import CatalogSnapshot, PublishResult, CatalogConflict, CatalogUnavailable
from main import admin_exception_handler, request_validation_handler, http_exception_handler
from test.test_pricing_catalog_contracts import BASELINE

ACTOR = SimpleNamespace(adminId='admin-1', email='admin@example.test', sessionId='session-1')
CATALOG = CatalogSnapshot(1, BASELINE, datetime(2026,10,15,tzinfo=timezone.utc), None, 'system', 'Initial pricing catalog')


class Repository:
    def __init__(self): self.calls = []; self.error = None
    def loadCurrent(self): return CATALOG
    def listHistory(self, **kwargs): return [CATALOG], None
    def publish(self, **kwargs):
        self.calls.append(kwargs)
        if self.error: raise self.error
        return PublishResult(CATALOG, True)


@pytest.fixture()
def fixture():
    app = FastAPI()
    app.include_router(router_module.router, prefix='/admin')
    app.add_exception_handler(AdminApiError, admin_exception_handler)
    app.add_exception_handler(RequestValidationError, request_validation_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    repo = Repository()
    app.dependency_overrides[verifyAdmin] = lambda: ACTOR
    app.dependency_overrides[getAdminPricingService] = lambda: AdminPricingService(repo)
    with TestClient(app) as client: yield app, client, repo


def body(**changes): return {'expectedRevision':1, 'reason':'Regional review', 'prices':BASELINE, **changes}


def test_read_and_publication_contracts_are_private_and_actor_bound(fixture):
    _, client, repo = fixture
    response = client.get('/admin/pricing')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'private, no-store'
    assert response.json()['current']['prices'] == BASELINE
    response = client.put('/admin/pricing', json=body(), headers={'Idempotency-Key':'request-1'})
    assert response.status_code == 200 and response.json()['noOp']
    assert repo.calls[0]['admin'] is ACTOR
    assert repo.calls[0]['idempotencyKey'] == 'request-1'


@pytest.mark.parametrize('status', [401,403])
def test_rejected_session_cannot_read_or_publish(fixture, status):
    app, client, repo = fixture
    def denied(): raise HTTPException(status_code=status, detail='Unauthorized')
    app.dependency_overrides[verifyAdmin] = denied
    assert client.get('/admin/pricing').status_code == status
    assert client.put('/admin/pricing', json=body(), headers={'Idempotency-Key':'request-1'}).status_code == status
    assert repo.calls == []


@pytest.mark.parametrize('changes', [{'adminId':'spoof'}, {'expectedRevision':True}, {'reason':'abcd'}, {'prices':{}}])
def test_invalid_body_never_reaches_repository(fixture, changes):
    _, client, repo = fixture
    assert client.put('/admin/pricing', json=body(**changes), headers={'Idempotency-Key':'request-1'}).status_code == 422
    assert repo.calls == []


@pytest.mark.parametrize('error,status,code', [
    (CatalogConflict('PRICING_CONFLICT',2),409,'PRICING_CONFLICT'),
    (CatalogConflict('IDEMPOTENCY_CONFLICT',2),409,'IDEMPOTENCY_CONFLICT'),
    (CatalogUnavailable(),503,'PRICE_UNAVAILABLE'),
])
def test_failure_status_and_code_are_preserved(fixture,error,status,code):
    _, client, repo = fixture
    repo.error = error
    response = client.put('/admin/pricing', json=body(), headers={'Idempotency-Key':'request-1'})
    assert response.status_code == status
    assert response.json()['errors']['code'] == code


def test_missing_key_invalid_key_and_cursor_are_rejected(fixture):
    _, client, repo = fixture
    assert client.put('/admin/pricing', json=body()).status_code == 422
    assert client.put('/admin/pricing', json=body(), headers={'Idempotency-Key':'bad key'}).status_code == 422
    assert client.get('/admin/pricing?beforeRevision=0').status_code == 422
    assert repo.calls == []
