"""Dormant refund mechanics against isolated SQL, beyond the deferred HTTP gate.

The fixture explicitly overrides that gate to retain financial transaction
regressions. test_deferred_refund_routes verifies production HTTP denial.
"""
from dataclasses import replace
from datetime import timedelta
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from api.services.billing.manualBillingContracts import RefundQuote
from test.test_manual_billing_runtime import database, USER, NOW, seed_payment, sqlTransaction


class Provider:
    def __init__(self): self.calls = []
    def verifyUnreturnedCapture(self,item): pass
    def refund(self,*args):
        self.calls.append(args)
        return {'id':'refund-one','payment_id':args[0],'amount':args[1],'status':'processed'}


@pytest.fixture
def refund_client(database,monkeypatch):
    from api.services.billing.subscriptionRefundService import SubscriptionRefundService, _ProductionRefundStore
    import api.routers.billingAdmin as routes
    repo,path=database
    evidence=seed_payment(database)
    repo.finalizeCapturedPayment(evidence)
    quote=RefundQuote('quote-http',USER,'email-case','INR',NOW,NOW+timedelta(minutes=5),3000,
        ({'invoiceId':evidence.invoiceId,'paymentId':evidence.providerPaymentId,'amount':3000,'currency':'INR'},),True,False)
    repo.saveRefundQuote('staff',quote,'Approved unused time')
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repo)
    provider=Provider()
    store=_ProductionRefundStore()
    # The transport supplies stored rows; all financial business methods stay real.
    from test.test_manual_auth_coverage import SqlRestClient
    store.client=SqlRestClient(path)
    service=SubscriptionRefundService(store=store,provider=provider,now=lambda:NOW)
    monkeypatch.setattr(SubscriptionRefundService,'forProduction',classmethod(lambda cls:service))
    monkeypatch.setattr(routes,'_loadPaidIntervalsForInvoices',lambda *_:[])
    app=FastAPI()
    app.include_router(routes.router)
    from fastapi import HTTPException
    @app.exception_handler(HTTPException)
    async def flat(_,error):
        return JSONResponse(status_code=error.status_code,content=error.detail if isinstance(error.detail,dict) else {'message':error.detail})
    app.dependency_overrides[routes.verifyBillingAdmin]=lambda:'staff'
    app.dependency_overrides[routes.refuseDeferredRefundOperations]=lambda:None
    return TestClient(app),repo,path,provider,quote


def payload(quote,**changes):
    return {'userId':USER,'invoiceIds':['invoice-one'],'caseReference':'email-case','reason':'Approved unused time',
        'quoteId':quote.quoteId,'expectedTotalAmount':quote.amount,**changes}


@pytest.mark.parametrize('expired',[False,True])
def test_staff_changed_or_expired_quote_returns_conflict_without_side_effects(refund_client,expired):
    client,repo,path,provider,quote=refund_client
    if expired:
        import json
        stored=repo.findRefundQuote(quote.quoteId)
        stored['expiresAt']=(NOW-timedelta(seconds=1)).isoformat()
        with sqlTransaction(path) as db:
            db.execute('UPDATE billing_events SET metadata_json=? WHERE idempotency_key=?',(json.dumps(stored),'refund-quote:'+quote.quoteId))
    response=client.post('/refunds/initiate',json=payload(quote,expectedTotalAmount=quote.amount if expired else 2999),headers={'Idempotency-Key':'approval'})
    assert response.status_code==409
    assert response.json()['quote']['amount']==3000
    assert not provider.calls
    with sqlTransaction(path) as db:
        assert db.execute("SELECT count(*) FROM billing_events WHERE event_type='refund.intent'").fetchone()[0]==0
        assert db.execute('SELECT status FROM subscriptions').fetchone()[0]=='active'


def test_duplicate_refund_returns_original_reservation(refund_client):
    client,_,path,provider,quote=refund_client
    first=client.post('/refunds/initiate',json=payload(quote),headers={'Idempotency-Key':'approval'})
    second=client.post('/refunds/initiate',json=payload(quote),headers={'Idempotency-Key':'approval'})
    assert first.status_code==second.status_code==200
    assert first.json()['data']['refundIntentId']==second.json()['data']['refundIntentId']
    assert len(provider.calls)==1


@pytest.mark.parametrize('changes',[{'reason':'x'*2001},{'caseReference':'x'*201},{'expectedTotalAmount':-1}])
def test_staff_approval_limits_are_validated(refund_client,changes):
    client,_,_,provider,quote=refund_client
    assert client.post('/refunds/initiate',json=payload(quote,**changes),headers={'Idempotency-Key':'approval'}).status_code==422
    assert not provider.calls


def test_changed_approval_payload_conflicts(refund_client):
    client,_,_,provider,quote=refund_client
    first=client.post('/refunds/initiate',json=payload(quote),headers={'Idempotency-Key':'approval'})
    assert first.status_code==200
    changed=client.post('/refunds/initiate',json=payload(quote,reason='Different support approval'),headers={'Idempotency-Key':'approval'})
    assert changed.status_code==409
    assert len(provider.calls)==1
