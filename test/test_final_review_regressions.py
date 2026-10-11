from dataclasses import replace
from datetime import timedelta
import json
from unittest.mock import patch,Mock
import pytest
from test.test_manual_billing_runtime import database,USER,NOW,sqlTransaction,read_row
from test.test_manual_checkout_http import checkout_database,checkout_client,request
from test.test_manual_payment_entrypoints import paid_then_request,evidence,RecoverableProvider


def test_verified_legacy_annual_invoice_supports_admission_without_refill(checkout_database):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repo,path=checkout_database
    manual,checkout=paid_then_request(checkout_database,'annual_prepaid','initial_purchase')
    intent=manual.createCheckout(checkout)
    manual.finalizeCapturedPayment(evidence(intent))
    with sqlTransaction(path) as db:
        db.execute('UPDATE "Invoices" SET metadata_json=\'{}\'')
        db.execute('UPDATE credit_balances SET used_tokens=123,remaining_tokens=monthly_token_quota-123,topup_tokens=456,lifecycle_id=null,credit_period_id=null')
    with patch('api.services.credits.manualCreditRepository.datetime',Mock(now=lambda _:NOW)):
        context=ManualCreditRepository(repo).admit(USER,'reporting_query','legacy-annual')
    assert context.billingMode=='annual_prepaid'
    balance=read_row(path,'credit_balances')
    assert balance['used_tokens']==123 and balance['remaining_tokens']==balance['monthly_token_quota']-123
    assert balance['topup_tokens']==456


def test_early_annual_payment_keeps_current_experts_until_boundary(checkout_database):
    from dateutil.relativedelta import relativedelta
    from api.services.billing.manualPaymentService import ManualPaymentService
    repo,path=checkout_database
    manual=ManualPaymentService.forProduction(RecoverableProvider(path),repo)
    initial=manual.createCheckout(request('initial',domains=('banking','telecom'),mode='annual_prepaid'))
    current=manual.finalizeCapturedPayment(evidence(initial)).currentPeriod
    with sqlTransaction(path) as db:
        db.execute('UPDATE subscriptions SET pending_removals=?',(json.dumps(['telecom']),))
        db.execute('''INSERT INTO "Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,currency,period_start,period_end,metadata_json)
            VALUES('annual-future',?,?,'UPCOMING','renewal',10000,'INR',?,?,?)''',
            (USER,current.subscriptionId,current.end.isoformat(),(current.end+relativedelta(years=1)).isoformat(),
             json.dumps({'manualBilling':{'domains':['banking']}})))
    future=manual.createCheckout(request('early-annual',mode='annual_prepaid',purpose='renewal',invoiceId='annual-future'))
    result=manual.finalizeCapturedPayment(evidence(future,'annual-renewal'))
    assert set(result.currentPeriod.domains)=={'banking','telecom'}
    assert result.nextPeriod.domains==('banking',)
    sub=read_row(path,'subscriptions')
    assert json.loads(sub['pending_removals'])==['telecom']
    assert read_row(path,'credit_balances')['monthly_token_quota']==20000000


def test_annual_past_due_invoice_can_create_customer_free_checkout(checkout_database):
    from dateutil.relativedelta import relativedelta
    repo,path=checkout_database
    manual,checkout=paid_then_request(checkout_database,'annual_prepaid','renewal')
    start=NOW-timedelta(days=1)
    with sqlTransaction(path) as db:
        db.execute('UPDATE subscriptions SET status=\'past_due\',current_period_end=?',(start.isoformat(),))
        db.execute('UPDATE "Invoices" SET period_start=?,period_end=? WHERE id=\'renewal\'',(start.isoformat(),(start+relativedelta(years=1)).isoformat()))
    intent=manual.createCheckout(checkout)
    assert intent.razorpayOrderId and intent.billingMode=='annual_prepaid'
    result=manual.finalizeCapturedPayment(evidence(intent))
    assert result.finalized and result.creditsRefilled
    assert read_row(path,'subscriptions')['status']=='active'


@pytest.mark.parametrize('closed',[False,True])
def test_public_invoice_status_is_the_committed_fact(checkout_database,closed):
    from api.services.billing.manualBillingPresentation import serializeFinalizationResult
    repo,path=checkout_database
    manual,checkout=paid_then_request(checkout_database,'monthly_prepaid','initial_purchase')
    intent=manual.createCheckout(checkout)
    if closed:
        with sqlTransaction(path) as db: db.execute('UPDATE "Invoices" SET status=\'VOID\'')
    else: manual.finalizeCapturedPayment(evidence(intent))
    result=manual.finalizeCapturedPayment(evidence(intent,'another-capture'))
    assert result.state=='requires_reconciliation'
    assert serializeFinalizationResult(result)['invoiceStatus']==('VOID' if closed else 'PAID')


@pytest.mark.parametrize('stage',['canonical','reservation','verification'])
def test_checkout_database_outage_is_retryable_503(checkout_client,checkout_database,monkeypatch,stage):
    import hmac,hashlib,os
    import psycopg2
    client,provider,path=checkout_client
    repo,_=checkout_database
    def unavailable(*args,**kwargs):
        raise psycopg2.OperationalError('disposable database unavailable')
    if stage=='verification':
        created=client.post('/createSubscription',json={'domains':['banking'],'contact':''}).json()
        order=created['orderId'];payment='outage-payment'
        signature=hmac.new(os.environ['RAZORPAY_KEY_SECRET'].encode(),(order+'|'+payment).encode(),hashlib.sha256).hexdigest()
        monkeypatch.setattr(repo,'connectionFactory',unavailable)
        response=client.post('/verifySubscription',json={'razorpayOrderId':order,'razorpayPaymentId':payment,'razorpaySignature':signature})
    else:
        monkeypatch.setattr(repo,'ensureCanonicalSubscription' if stage=='canonical' else 'reserveCheckout',unavailable)
        response=client.post('/createSubscription',json={'domains':['banking'],'contact':''})
    assert response.status_code==503


class AnnualSqlQuery:
    """REST transport substitute: filters execute in SQL, task logic is real."""
    def __init__(self,path,table):
        self.path=path;self.table=table;self.clauses=[];self.params=[];self.payload=None;self.isInsert=False;self.limitCount=None
    def select(self,*args): return self
    def eq(self,key,value):
        self.clauses.append('"'+key+'"=?');self.params.append(value);return self
    def in_(self,key,values):
        self.clauses.append('"'+key+'" in ('+','.join('?' for _ in values)+')');self.params.extend(values);return self
    def lte(self,key,value):
        self.clauses.append('"'+key+'"<=?');self.params.append(value);return self
    @property
    def not_(self): return self
    def is_(self,key,value):
        assert value=='null';self.clauses.append('"'+key+'" is not null');return self
    def limit(self,value): self.limitCount=value;return self
    def update(self,payload): self.payload=payload;return self
    def insert(self,payload):
        import uuid
        self.payload={'id':str(uuid.uuid4()),**payload};self.isInsert=True;return self
    def execute(self):
        import sqlite3
        from types import SimpleNamespace
        where=' and '.join(self.clauses) or '1=1'
        with sqlTransaction(self.path) as db:
            db.row_factory=sqlite3.Row
            if self.payload is not None:
                values=[json.dumps(v) if isinstance(v,(dict,list)) else v for v in self.payload.values()]
                keys=list(self.payload)
                if self.isInsert:
                    db.execute('insert into "'+self.table+'"('+','.join('"'+k+'"' for k in keys)+') values('+','.join('?' for k in keys)+')',values)
                    where='id=?';self.params=[self.payload['id']]
                else:
                    db.execute('update "'+self.table+'" set '+','.join('"'+k+'"=?' for k in keys)+' where '+where,values+self.params)
            rows=[dict(row) for row in db.execute('select * from "'+self.table+'" where '+where+(' limit '+str(self.limitCount) if self.limitCount else ''),self.params)]
        for row in rows:
            for key in ('metadata_json','billing_state','subscribed_experts','pending_removals','pending_additions'):
                if isinstance(row.get(key),str): row[key]=json.loads(row[key])
        return SimpleNamespace(data=rows)

class AnnualSqlClient:
    def __init__(self,path): self.path=path
    def table(self,name): return AnnualSqlQuery(self.path,name)

@pytest.fixture
def annual_task_database(checkout_database):
    repo,path=checkout_database
    with sqlTransaction(path) as db:
        for key in ('fullName','phoneNumber'): db.execute('alter table "Users" add column '+key+' text')
        for key in ('webhook_event_id',): db.execute('alter table billing_events add column '+key+' text')
    return repo,path,AnnualSqlClient(path)

def test_annual_invoice_generation_preparation_and_checkout_agree(annual_task_database,monkeypatch):
    import api.services.billing.invoiceService as invoices
    repo,path,client=annual_task_database
    manual,checkout=paid_then_request((repo,path),'annual_prepaid','initial_purchase')
    manual.finalizeCapturedPayment(evidence(manual.createCheckout(checkout)))
    monkeypatch.setattr(invoices,'client',client)
    monkeypatch.setattr(invoices,'getBillingRedisClient',lambda:Mock(set=lambda *args,**kw:True))
    sub=read_row(path,'subscriptions')
    for key in ('subscribed_experts','pending_removals','billing_state'): sub[key]=json.loads(sub[key])
    upcoming=invoices.createUpcomingRenewalInvoice(sub,{'userId':USER})
    assert upcoming is not None
    prepared=invoices.prepareDashboardRenewalInvoice(upcoming)
    assert prepared is not None
    intent=manual.createCheckout(request('generated-renewal',mode='annual_prepaid',purpose='renewal',invoiceId=prepared['id']))
    assert intent.razorpayOrderId
    assert prepared['status']=='PAYMENT_PENDING'

@pytest.mark.parametrize('sweep',['t7','reminders'])
@pytest.mark.parametrize('mode',['annual_prepaid','monthly_prepaid'])
@pytest.mark.parametrize('casing',['upper','lower'])
def test_annual_sweeps_use_stored_casing_and_exclude_monthly(annual_task_database,monkeypatch,sweep,mode,casing):
    from datetime import datetime,timezone
    from nubrix.triggers.tasks.annualRenewalTask import AnnualRenewalTask
    from nubrix.triggers.tasks.renewalLifecycleTask import RenewalLifecycleTask
    import nubrix.triggers.tasks.annualRenewalTask as annualModule
    repo,path,client=annual_task_database
    manual,checkout=paid_then_request((repo,path),'annual_prepaid','renewal')
    due=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    with sqlTransaction(path) as db:
        db.execute('update subscriptions set billing_mode=?',(mode,))
        db.execute('update "Invoices" set status=?,due_date=? where id=?',(getattr(('UPCOMING' if sweep=='t7' else 'PAYMENT_PENDING'),casing)(),due,'renewal'))
    sent=[]
    if sweep=='t7':
        task=AnnualRenewalTask.__new__(AnnualRenewalTask);task.client=client
        monkeypatch.setattr(annualModule,'prepareDashboardRenewalInvoice',lambda invoice:invoice)
        task._sendT7Email=lambda *args:sent.append(args)
        result=task._sweepT7();assert result['errors']==0
    else:
        task=RenewalLifecycleTask.__new__(RenewalLifecycleTask);task.client=client
        task.redisClient=Mock(set=lambda *args,**kw:True)
        task._sendReminderEmail=lambda **kw:sent.append(kw) or 'SENT'
        result=task._sweepReminders();assert result['errors']==0
    assert len(sent)==(1 if mode=='annual_prepaid' else 0)


@pytest.mark.parametrize('stage',['create','verify'])
@pytest.mark.parametrize('failure',['server','transport'])
def test_provider_outage_is_retryable_503(checkout_client,monkeypatch,stage,failure):
    import razorpay,requests,hmac,hashlib,os
    client,provider,path=checkout_client
    def unavailable(*args,**kwargs):
        raise razorpay.errors.ServerError('provider unavailable') if failure=='server' else requests.ConnectionError('provider transport unavailable')
    if stage=='create':
        monkeypatch.setattr(provider.order,'create',unavailable)
        response=client.post('/createSubscription',json={'domains':['banking'],'contact':''})
    else:
        created=client.post('/createSubscription',json={'domains':['banking'],'contact':''}).json()
        order=created['orderId'];payment='provider-outage-payment'
        signature=hmac.new(os.environ['RAZORPAY_KEY_SECRET'].encode(),(order+'|'+payment).encode(),hashlib.sha256).hexdigest()
        monkeypatch.setattr(provider.order,'fetch',unavailable)
        response=client.post('/verifySubscription',json={'razorpayOrderId':order,'razorpayPaymentId':payment,'razorpaySignature':signature})
    assert response.status_code==503


@pytest.mark.parametrize('route,payload',[('/addDomains',{'domains':['telecom']}),('/createRenewalPaymentSession',{'invoiceId':'renewal'})])
def test_canonical_rest_lookup_outage_is_retryable_503(checkout_client,monkeypatch,route,payload):
    import httpx
    import api.routers.subscriptions as routes
    client,provider,path=checkout_client
    def unavailable(*args,**kwargs): raise httpx.ConnectError('database API unavailable')
    monkeypatch.setattr(routes.subscriptionService.client,'table',unavailable)
    assert client.post(route,json=payload).status_code==503
