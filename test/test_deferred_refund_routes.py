"""The approved deferred-refund policy denies HTTP entry before financial I/O."""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import api.routers.billingAdmin as routes


@pytest.mark.parametrize('path', ['/refunds/quote','/refunds/initiate'])
@pytest.mark.parametrize('actor', ['ordinary','allowlisted_staff'])
def test_deferred_refunds_are_denied_before_service_or_provider_access(path, actor, monkeypatch):
    # Removing the production deny dependency would resume financial I/O here.
    def financialAccessMustNotRun(*args, **kwargs):
        pytest.fail('Disabled refund route reached financial service or invoice I/O')
    monkeypatch.setattr(routes,'_loadPaidIntervalsForInvoices',financialAccessMustNotRun)
    monkeypatch.setattr(routes.SubscriptionRefundService,'forProduction',financialAccessMustNotRun)
    app=FastAPI()
    app.include_router(routes.router)
    @app.exception_handler(HTTPException)
    async def flat(_, error):
        return JSONResponse(status_code=error.status_code,
            content=error.detail if isinstance(error.detail,dict) else {'message':error.detail})
    def staff():
        if actor=='ordinary':
            raise HTTPException(status_code=403,detail='Not a billing administrator')
        return 'internal-staff'
    app.dependency_overrides[routes.verifyBillingAdmin]=staff
    payload={'userId':'test-target','invoiceIds':['test-invoice'],'caseReference':'test-case','reason':'Test policy'}
    if path.endswith('initiate'):
        payload.update(quoteId='test-quote',expectedTotalAmount=100)
    response=TestClient(app).post(path,json=payload,headers={'Idempotency-Key':'test-policy'})
    assert response.status_code==503
    assert response.json()['errorCode']=='REFUND_OPERATIONS_DISABLED'
