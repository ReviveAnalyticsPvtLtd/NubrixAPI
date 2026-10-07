"""IR-01/05/09 races; disposable local PostgreSQL is mandatory."""
import os
import uuid
from dataclasses import replace
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from types import SimpleNamespace
from unittest.mock import patch
import psycopg2
from psycopg2.extras import Json
import pytest
from test.test_manual_billing_postgres_integration import postgres, payment, NOW
from api.services.billing.manualBillingContracts import CheckoutRequest, VerifiedPaymentEvidence, RefundQuote
from api.services.billing.manualPaymentService import ManualPaymentService
from api.services.billing.manualBillingRepository import ManualBillingRepository

pytestmark=pytest.mark.skipif(os.environ.get('RUN_MANUAL_BILLING_INTEGRATION')!='1',reason='Disposable PostgreSQL opt-in required; skipped is UNVERIFIED')
REFERENCE={'amount':10000,'currency':'INR','source':'razorpay_plan_fetch'}


class OrderProvider:
    def __init__(self):
        self.orders={};self.lock=Lock();self.fail=True
        self.order=SimpleNamespace(create=self.create,all=self.list,payments=lambda *args:{'items':[]})
    def create(self,payload):
        with self.lock:
            if self.fail:
                self.fail=False
                raise TimeoutError('not submitted')
            order={**payload,'id':'order-'+str(uuid.uuid4())}
            self.orders[order['id']]=order
            return order
    def list(self,params):
        with self.lock: return {'items':[row.copy() for row in self.orders.values() if row['receipt']==params['receipt']]}


def test_two_sessions_retry_zero_order_failure_creates_one_replacement(postgres):
    user='zero-order-'+str(uuid.uuid4())
    with psycopg2.connect(postgres) as db:
        with db.cursor() as sql: sql.execute('insert into public."Users"("userId") values(%s)',(user,))
    repo=ManualBillingRepository(lambda:psycopg2.connect(postgres))
    provider=OrderProvider();service=ManualPaymentService.forProduction(provider,repo)
    request=CheckoutRequest(user,'initial_purchase','monthly_prepaid',{'domains':['banking']},None)
    with patch('api.services.billing.billingEngine._getMonthlyBasePrice',return_value=REFERENCE):
        with pytest.raises(RuntimeError): service.createCheckout(request)
        with psycopg2.connect(postgres) as db:
            with db.cursor() as sql:
                sql.execute("update public.billing_events set metadata_json=jsonb_set(metadata_json,'{manualBilling,orderCreationClaimedAt}',to_jsonb((clock_timestamp()-interval '1 hour')::text)) where user_id=%s and event_category='payment_attempt'",(user,))
        def retry(_):
            try:return service.createCheckout(request)
            except RuntimeError:return None  # concurrent acknowledgement still in flight
        with ThreadPoolExecutor(max_workers=2) as pool: list(pool.map(retry,range(2)))
        final=service.createCheckout(request)
    assert final.razorpayOrderId and len(provider.orders)==1
    with psycopg2.connect(postgres) as db:
        with db.cursor() as sql:
            sql.execute("select payment_status,count(*) from public.billing_events where user_id=%s and event_category='payment_attempt' group by payment_status",(user,))
            assert dict(sql.fetchall())=={'failed':1,'pending_provider_ack':1}


def test_two_sessions_cancel_addition_does_not_lose_other_reservation(payment):
    repo,initial,url=payment;repo.finalizeCapturedPayment(initial)
    def reserve(domain,key):
        return repo.reserveCheckout(CheckoutRequest(initial.userId,'expert_addition','monthly_prepaid',{'domains':[domain]},key))
    with patch('api.services.billing.billingEngine._getMonthlyBasePrice',return_value=REFERENCE):
        first=reserve('telecom','first')
        with ThreadPoolExecutor(max_workers=2) as pool:
            cancel=pool.submit(repo.cancelExpertAddition,initial.userId,'telecom')
            other=pool.submit(reserve,'manufacturing','other')
            cancel.result();second=other.result()
    row=repo.ensureCanonicalSubscription(initial.userId)
    states={item['attemptId']:item['state'] for item in row['pending_additions']}
    assert states[first.attemptId]=='cancelled' and states[second.attemptId]=='awaiting_payment'


def test_two_sessions_cancel_vs_capture_cannot_overwrite_activated_addition(payment):
    repo,initial,url=payment;repo.finalizeCapturedPayment(initial)
    with patch('api.services.billing.billingEngine._getMonthlyBasePrice',return_value=REFERENCE):
        intent=repo.reserveCheckout(CheckoutRequest(initial.userId,'expert_addition','monthly_prepaid',{'domains':['telecom']},'addition'))
    order='order-'+str(uuid.uuid4());repo.bindProviderOrder(intent.attemptId,{'id':order})
    money=VerifiedPaymentEvidence(intent.attemptId,intent.invoiceId,initial.userId,order,'pay-'+str(uuid.uuid4()),'expert_addition','INR','captured','server_observation',intent.amount,NOW,None,None,False)
    def cancel():
        try:return repo.cancelExpertAddition(initial.userId,'telecom')
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        closed=pool.submit(cancel);captured=pool.submit(repo.finalizeCapturedPayment,money)
        closed.result();result=captured.result()
    row=repo.ensureCanonicalSubscription(initial.userId)
    state=next(item['state'] for item in row['pending_additions'] if item['attemptId']==intent.attemptId)
    if result.finalized:
        assert state=='activated' and 'telecom' in row['subscribed_experts']
    else:
        assert result.state=='requires_reconciliation' and state=='cancelled' and 'telecom' not in row['subscribed_experts']


def test_two_sessions_refund_replay_after_reserve_submits_once(payment,monkeypatch):
    from api.services.billing.subscriptionRefundService import SubscriptionRefundService,_ProductionRefundStore
    repo,money,url=payment;period=repo.finalizeCapturedPayment(money).currentPeriod
    quote=RefundQuote('quote-'+str(uuid.uuid4()),money.userId,'email-case','INR',NOW,NOW+timedelta(minutes=5),3000,
        ({'invoiceId':money.invoiceId},),True,False)
    repo.saveRefundQuote('staff',quote,'Approved unused time')
    with patch('api.services.billing.manualBillingRepository._now',return_value=NOW):
        reserved=repo.reserveUnusedTimeRefund(quote.quoteId,'staff','email-case','Approved unused time',3000,'approval')
    calls=[];lock=Lock()
    class RefundProvider:
        def verifyUnreturnedCapture(self,item):raise AssertionError('Replay must reuse frozen reservation')
        def refund(self,payment,amount,intent):
            with lock:calls.append((payment,amount,intent))
            return {'id':'refund-'+intent,'payment_id':payment,'amount':amount,'status':'processed'}
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repo)
    service=SubscriptionRefundService(store=_ProductionRefundStore(object()),provider=RefundProvider())
    payload={'userId':money.userId,'invoiceIds':[money.invoiceId],'quoteId':quote.quoteId,'expectedTotalAmount':3000,'caseReference':'email-case','reason':'Approved unused time'}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:service.initiateUnusedTimeRefund('staff',payload,'approval'),range(2)))
    assert len(calls)==1
    assert all(row.refundIntentId==reserved.refundIntentId for row in results)
    assert service.initiateUnusedTimeRefund('staff',payload,'approval').refundState=='processed'
