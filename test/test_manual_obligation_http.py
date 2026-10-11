import os
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from test.test_manual_obligation_reports import obligations
from test.test_billing_notification_revisions import deliveries
from test.test_manual_billing_runtime import database


@pytest.mark.parametrize('user,expected',[('ordinary-user',403),('staff',200)])
def test_obligation_http_enforces_actual_staff_allowlist(obligations,monkeypatch,user,expected):
    import api.routers.billingAdmin as routes
    service,_=obligations
    monkeypatch.setenv('BILLING_ADMIN_USER_IDS','staff')
    monkeypatch.setattr(routes,'ReconciliationService',lambda:service)
    app=FastAPI()
    app.include_router(routes.router)
    token=jwt.encode({'userId':user,'email':'test@example.test'},os.environ['SECRET_KEY'],algorithm='HS256')
    # Session transport only: the real billing-admin JWT/allowlist guard runs.
    app.dependency_overrides[routes.verifyToken]=lambda:token
    response=TestClient(app).get('/reconciliation/obligations?limit=2')
    assert response.status_code==expected
    if expected==200:
        assert len(response.json()['data']['items'])==2
        assert response.json()['data']['nextCursor']
