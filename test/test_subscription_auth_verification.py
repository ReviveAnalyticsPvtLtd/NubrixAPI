"""Trusted refresh traverses the actual mounted route and session dependency."""
import os
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from api.services.authenticationService import AuthenticationService
from api.services.subscriptions.subscriptionSnapshotService import SubscriptionSnapshotService
from test.subscriptionModelFixtures import NOW, USER, factsFor
from test.test_user_access_enforcement import FakeClient


@pytest.fixture
def verification(monkeypatch):
    from api import commons
    from api.routers import authentication as routes
    from api.services.credits.creditService import creditService
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE','v2')
    account = {'userId':USER,'email':'verify@example.test','onboarded':False,
        'currentWorkspaceId':None,'profileImage':None,'isBanned':False}
    token = jwt.encode({'userId':USER,'email':account['email'],'sub_status':'active','plan_type':'annual'},
        os.environ['SECRET_KEY'],algorithm='HS256')
    client = FakeClient([account],sessions=[{'userId':USER,'email':account['email'],'accessToken':token}])
    service = AuthenticationService.__new__(AuthenticationService)
    service.client = client
    monkeypatch.setattr(commons,'client',client)
    monkeypatch.setattr(routes,'authenticationService',service)
    monkeypatch.setattr(creditService,'getBalanceSnapshot',lambda user:{'owner':user})
    def install(facts,now=NOW):
        snapshot = SubscriptionSnapshotService(lambda user,requested:(facts,requested or now))
        monkeypatch.setattr('api.services.subscriptions.subscriptionSnapshotService.getSubscriptionSnapshotService',lambda:snapshot)
    install(factsFor('expired'))
    app = FastAPI()
    app.include_router(routes.router,prefix='/auth')
    with TestClient(app) as http:
        yield http,{'Authorization':'Bearer '+token},client,service,install


@pytest.mark.parametrize('condition',['none','trial','monthly','annual','expired'])
def test_verify_returns_live_owned_identity_and_snapshot_with_existing_envelope(verification,condition):
    http,headers,client,_,install = verification
    install(factsFor(condition))
    result = http.get('/auth/verify',headers=headers)
    assert result.status_code==200
    body = result.json()
    assert body['status']=='SUCCESS' and body['message']=='Token is valid and has not expired.'
    assert body['credits']=={'owner':USER}
    assert body['userId']==USER and body['email']==client.rows['Users'][0]['email']
    assert body['onboarded'] is False and body['currentWorkspaceId'] is None and body['profileImage'] is None
    snapshot = body['subscription']
    assert snapshot['schemaVersion']==2
    assert snapshot['status']==('active' if condition in ('monthly','annual') else condition)
    if condition=='expired':
        assert snapshot['accessAllowed'] is False and snapshot['purchaseAllowed'] is True
        assert snapshot['paidUntil'] is None and snapshot['currentPeriod'] is None


@pytest.mark.parametrize('phase',['dual','v2','contracted'])
def test_refresh_rechecks_onboarding_and_workspace_and_ignores_browser_claims(verification,monkeypatch,phase):
    http,headers,client,_,_ = verification
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE',phase)
    first = http.get('/auth/verify?onboarded=true&accessAllowed=true&status=active',headers=headers).json()
    assert first['onboarded'] is False and first['subscription']['status']=='expired'
    client.rows['Users'][0].update(onboarded=1,currentWorkspaceId='new-owned-workspace')
    second = http.get('/auth/verify',headers=headers).json()
    assert second['onboarded'] is True and second['currentWorkspaceId']=='new-owned-workspace'
    assert second['subscription']['accessAllowed'] is False
    assert first['subscription']==second['subscription']


def test_verification_at_exact_paid_end_never_trusts_active_session_claim(verification):
    http,headers,_,_,install = verification
    facts = factsFor('monthly')
    install(facts,facts.periods[0].end)
    snapshot = http.get('/auth/verify',headers=headers).json()['subscription']
    assert snapshot['status']=='expired' and not snapshot['accessAllowed']
    assert snapshot['purchaseAllowed'] and snapshot['paidUntil'] is None


@pytest.mark.parametrize('phase',['legacy','shadow'])
def test_legacy_and_shadow_verify_keep_exact_compatibility_envelope(verification,monkeypatch,phase):
    http,headers,_,service,_ = verification
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE',phase)
    service.client = None  # Unified account reads must not run here.
    body = http.get('/auth/verify',headers=headers).json()
    assert body=={'status':'SUCCESS','message':'Token is valid and has not expired.','credits':{'owner':USER}}


def test_unavailable_snapshot_is_503_instead_of_success_with_missing_permissions(verification):
    http,headers,_,_,install = verification
    install(replace(factsFor('monthly'),userId='other-owner'))
    response = http.get('/auth/verify',headers=headers)
    assert response.status_code==503
    assert response.json()['detail']['errorCode']=='SUBSCRIPTION_AUTHORITY_UNAVAILABLE'


def test_credit_outage_is_optional_and_does_not_hide_verified_subscription(verification,monkeypatch):
    from api.services.credits.creditService import creditService
    http,headers,_,_,_ = verification
    def unavailable(_): raise RuntimeError('synthetic credit outage')
    monkeypatch.setattr(creditService,'getBalanceSnapshot',unavailable)
    response = http.get('/auth/verify',headers=headers)
    assert response.status_code==200
    assert response.json()['credits'] is None
    assert response.json()['subscription']['status']=='expired'


@pytest.mark.parametrize('restriction',['banned','missing','mismatched_session','erased'])
def test_verify_preserves_live_account_and_session_guards(verification,restriction):
    http,headers,client,_,_ = verification
    if restriction=='banned': client.rows['Users'][0]['isBanned']=True
    if restriction=='missing': client.rows['Users']=[]
    if restriction=='mismatched_session': client.rows['Sessions'][0]['userId']='other-owner'
    if restriction=='erased': client.rows['Sessions']=[]
    response = http.get('/auth/verify',headers=headers)
    assert response.status_code==(403 if restriction=='banned' else 401)
    assert 'subscription' not in response.json()


@pytest.mark.parametrize('badFlag',[None,'true','false',2,{},[]])
def test_malformed_onboarding_authority_is_unavailable(verification,badFlag):
    http,headers,client,_,_ = verification
    client.rows['Users'][0]['onboarded']=badFlag
    response = http.get('/auth/verify',headers=headers)
    assert response.status_code==503
    assert response.json()['detail']['errorCode']=='SESSION_AUTHORITY_UNAVAILABLE'


def test_account_lookup_failure_does_not_return_success_or_stale_claims(verification):
    http,headers,_,service,_ = verification
    class Unavailable:
        def table(self,_): raise RuntimeError('synthetic transport outage')
    service.client = Unavailable()
    response = http.get('/auth/verify',headers=headers)
    assert response.status_code==503
    assert response.json()['detail']['errorCode']=='SESSION_AUTHORITY_UNAVAILABLE'


@pytest.mark.parametrize('authority',['account','subscription'])
def test_verification_authority_errors_do_not_expose_private_diagnostics(verification,monkeypatch,authority):
    http,headers,_,service,_ = verification
    diagnostic = 'PRIVATE_DIAGNOSTIC_SENTINEL connection=private-host row=private-email'
    class Unavailable:
        def table(self,_): raise RuntimeError(diagnostic)
        def get(self,_): raise RuntimeError(diagnostic)
    if authority=='account':
        service.client = Unavailable()
    else:
        monkeypatch.setattr('api.services.subscriptions.subscriptionSnapshotService.getSubscriptionSnapshotService',
            lambda:Unavailable())
    response = http.get('/auth/verify',headers=headers)
    assert response.status_code==503
    assert response.json()['detail']['errorCode']==(
        'SESSION_AUTHORITY_UNAVAILABLE' if authority=='account' else 'SUBSCRIPTION_AUTHORITY_UNAVAILABLE')
    assert diagnostic not in response.text
    assert 'backendLogMessage' not in response.json()['detail']
    assert __file__ not in response.text and 'filename:' not in response.text


def test_foreign_account_row_never_changes_the_authenticated_owner(verification):
    from types import SimpleNamespace
    http,headers,_,service,_ = verification
    class ForeignOwner:
        def table(self,_): return self
        def select(self,_): return self
        def eq(self,*_): return self
        def limit(self,_): return self
        def execute(self):
            return SimpleNamespace(data=[{'userId':'other-owner','email':'other@example.test','onboarded':True}])
    service.client = ForeignOwner()
    response = http.get('/auth/verify',headers=headers)
    assert response.status_code==503
    assert 'other@example.test' not in response.text


def test_verification_work_runs_off_the_event_loop(verification,monkeypatch):
    # Session verification makes blocking database calls; on the event loop they stall every other
    # request the worker holds, so a verify that queues behind them takes seconds.
    import asyncio
    from api.services.credits.creditService import creditService
    http,headers,_,service,_ = verification
    seen = []
    def onLoop():
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False
    facts = service.getVerificationFacts
    monkeypatch.setattr(service,'getVerificationFacts',lambda user:(seen.append(('facts',onLoop())),facts(user))[1])
    monkeypatch.setattr(creditService,'getBalanceSnapshot',lambda user:(seen.append(('credits',onLoop())),{'owner':user})[1])
    response = http.get('/auth/verify',headers=headers)
    assert response.status_code==200
    assert seen==[('facts',False),('credits',False)]
