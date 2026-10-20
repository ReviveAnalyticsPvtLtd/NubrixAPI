"""Complete erasure uses synthetic guarded PostgreSQL, never a live account."""
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extras import Json

from api.services.userErasureRepository import UserErasureRepository
from nubrix.triggers.tasks.userErasureTask import UserErasureTask
from test.subscriptionRuntimeFixtures import NOW, postgres, runtimeDb, account, query, capture, renewal, expirePurchasedCoverage  # noqa:F401
from test.test_user_erasure_task import FakeExternalCleanup, FakeAudit

pytestmark = pytest.mark.skipif(os.environ.get('RUN_MANUAL_BILLING_INTEGRATION')!='1',reason='Guarded disposable PostgreSQL opt-in; skip is UNVERIFIED')


@pytest.fixture(scope='module',autouse=True)
def erasureDb(runtimeDb):
    root = Path(__file__).resolve().parents[1]
    with psycopg2.connect(runtimeDb) as db, db.cursor() as cursor:
        cursor.execute('''CREATE TABLE "Sessions" ("userId" text REFERENCES "Users"("userId"));
            CREATE TABLE "Projects" ("projectId" text PRIMARY KEY,"ownerUserId" text REFERENCES "Users"("userId"));
            CREATE TABLE "Workspaces" (id uuid PRIMARY KEY,"ownerId" text REFERENCES "Users"("userId"));
            ALTER TABLE "WebhookEvents" ADD COLUMN payload jsonb DEFAULT '{}'::jsonb''')
        cursor.execute((root/'supabase/migrations/20260823190224_create_user_erasure_workflow.sql').read_text())
        for path in (root/'supabase/migrations').glob('*_complete_subscription_erasure_retention.sql'):
            cursor.execute(path.read_text())
        # The legacy fixture allowlist installs additive storage before this
        # fixture's retention schema. Reapply its idempotent archive extension.
        for path in (root/'supabase/migrations').glob('*_add_editable_purchase_checkout.sql'):
            cursor.execute(path.read_text())
    return runtimeDb


def startErasure(url,user):
    admin = query(url,'''INSERT INTO admin_users(email,name,password_hash) VALUES(%s,'Synthetic','not-a-real-password') RETURNING id''',
        (str(uuid4())+'@example.test',),one=True)['id']
    repository = UserErasureRepository(lambda:psycopg2.connect(url))
    request = repository.createRequest(user,'a'*64,str(admin),str(uuid4()),None)
    return repository,str(request['id'])


def runErasure(url,user):
    repository,request = startErasure(url,user)
    external = FakeExternalCleanup()
    outcome = UserErasureTask(repository,external,FakeAudit()).execute(request)
    assert outcome['status']=='COMPLETED'
    assert repository.verifyDatabaseErasure(user)=={}
    status = repository.getRequest(request)
    assert status['target_user_id'] is None
    assert status['retention_manifest']['originalIdentifiersRetained'] is True
    assert status['retention_manifest']['policyCode']=='ORIGINAL_AUDITS_RETAINED'
    assert status['resource_manifest'].get('projectIds') is None
    assert UserErasureTask(repository,external,FakeAudit()).execute(request)['status']=='SKIPPED'
    return repository,request


@pytest.mark.parametrize('account',['monthly_prepaid','annual_prepaid'],indirect=True)
@pytest.mark.parametrize('condition',['active','expired','future','opted_out'])
def test_entire_paid_erasure_retains_exact_audits_without_product_access(account,condition):
    from api.services.billing.manualBillingContracts import CheckoutRequest
    repo,_,initial,url = account
    if condition=='expired': expirePurchasedCoverage(account)
    if condition=='future':
        invoice = renewal(account) if initial.billingMode=='monthly_prepaid' else repo.createAnnualRenewalInvoice(initial.userId)
        capture(repo,repo.reserveCheckout(CheckoutRequest(initial.userId,'renewal',initial.billingMode,{'invoiceId':str(invoice['id'])},expectedCatalogRevision=1)))
    if condition=='opted_out': repo.setRenewalOptOut(initial.userId,True,None,'synthetic-erasure-optout')
    original = {'archive':{'originalRow':{'user_id':initial.userId,'unstructured':{'private':'retain by explicit policy'}}}}
    event = query(url,"""INSERT INTO billing_events(user_id,subscription_id,event_category,event_type,event_status,metadata_json)
        SELECT user_id,id,'audit','subscription.legacy_snapshot','COMMITTED',%s FROM subscriptions WHERE user_id=%s RETURNING id""",
        (Json(original),initial.userId),one=True)['id']
    before = query(url,'SELECT to_jsonb(e) AS original FROM billing_events e WHERE id=%s',(event,),one=True)['original']
    _,request = runErasure(url,initial.userId)
    retained = query(url,"SELECT original_record,record_hash=encode(sha256(convert_to(original_record::text,'UTF8')),'hex') AS valid FROM private.retained_user_records WHERE source_table='billing_events' AND source_id=%s",(event,),one=True)
    assert retained['original_record']==before and retained['valid']
    assert query(url,'SELECT id FROM billing_events WHERE id=%s',(event,),one=True) is None
    assert query(url,'SELECT id FROM "Invoices" WHERE "userId"=%s',(initial.userId,))==[]
    assert query(url,'SELECT id FROM subscriptions WHERE user_id=%s',(initial.userId,))==[]


@pytest.mark.parametrize('trial',[False,True])
def test_entire_unused_or_trial_erasure_is_once_only_and_discloses_retention(account,trial):
    repo,_,_,url = account
    user = 'erasure-new-'+str(uuid4())
    query(url,'INSERT INTO "Users"("userId") VALUES(%s)',(user,))
    repo.ensureCanonicalSubscription(user)
    if trial: repo.activateTrial(user,('banking','telecom'))
    runErasure(url,user)


def pendingCheckout(account):
    from api.services.billing.manualBillingContracts import CheckoutRequest
    repo,_,initial,_ = account
    invoice = renewal(account)
    return repo.reserveCheckout(CheckoutRequest(initial.userId,'renewal','monthly_prepaid',
        {'invoiceId':str(invoice['id'])},expectedCatalogRevision=1))


def test_erasure_freeze_prevents_a_new_provider_order(account):
    repo,_,initial,url = account
    intent = pendingCheckout(account)
    startErasure(url,initial.userId)
    assert repo.claimProviderOrderCreation(intent.attemptId) is False


def test_delayed_capture_with_original_owner_is_financial_only_and_idempotent(account):
    from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
    repo,_,initial,url = account
    intent = pendingCheckout(account)
    repo.bindProviderOrder(intent.attemptId,{'id':'order-'+intent.attemptId})
    runErasure(url,initial.userId)
    evidence = VerifiedPaymentEvidence(intent.attemptId,intent.invoiceId,initial.userId,
        'order-'+intent.attemptId,'payment-'+intent.attemptId,'renewal','INR','captured',
        'server_observation',intent.amount,NOW,None,None,False)
    for observed in (evidence,replace(evidence,userId=None)):
        result = repo.finalizeCapturedPayment(observed)
        assert result.state=='requires_reconciliation'
        assert not result.finalized and not result.creditsRefilled
    assert len(query(url,'SELECT id FROM billing_events WHERE provider_payment_id=%s',(evidence.providerPaymentId,)))==1
    assert query(url,'SELECT id FROM subscriptions WHERE user_id=%s',(initial.userId,))==[]
    with pytest.raises(ValueError):
        repo.finalizeCapturedPayment(replace(evidence,userId='another-account'))
    with pytest.raises(ValueError):
        repo.finalizeCapturedPayment(replace(evidence,amount=evidence.amount+1))


@pytest.mark.parametrize('financialStatus',['authorized','failed'])
def test_non_capture_webhook_after_erasure_never_errors_or_grants(account,financialStatus):
    from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
    repo,_,initial,url = account
    intent = pendingCheckout(account)
    order = 'order-'+intent.attemptId
    repo.bindProviderOrder(intent.attemptId,{'id':order})
    runErasure(url,initial.userId)
    result = repo.finalizeCapturedPayment(VerifiedPaymentEvidence(intent.attemptId,intent.invoiceId,None,
        order,'pay-'+intent.attemptId,'renewal','INR',financialStatus,'server_observation',intent.amount,NOW,None,None,False))
    assert result.state=='awaiting_capture' and not result.finalized and not result.creditsRefilled


def test_recovery_of_stale_original_snapshot_is_financial_only(account):
    from types import SimpleNamespace
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    repo,_,initial,url = account
    intent = pendingCheckout(account)
    order = 'order-'+intent.attemptId
    repo.bindProviderOrder(intent.attemptId,{'id':order})
    before = repo.attemptForOrder(order)
    runErasure(url,initial.userId)
    payment = {'id':'pay-'+intent.attemptId,'order_id':order,'amount':intent.amount,'currency':'INR','status':'captured'}
    provider = SimpleNamespace(order=SimpleNamespace(
        fetch=lambda _:dict(id=order,amount=intent.amount,currency='INR',status='paid'),
        payments=lambda *_:{'items':[payment]}))
    assert ManualBillingRecoveryService(repo,provider).recoverAttempt(before)==1
    assert repo.attemptForOrder(order)['user_id'] is None
    assert query(url,'SELECT user_id FROM credit_balances WHERE user_id=%s',(initial.userId,))==[]


def test_orderless_erased_attempt_can_finish_zero_match_recovery(account):
    repo,_,initial,url = account
    intent = pendingCheckout(account)
    repo.claimProviderOrderCreation(intent.attemptId)
    query(url,"UPDATE billing_events SET attempted_at=now()-interval '2 hours',metadata_json=jsonb_set(metadata_json,'{manualBilling,orderCreationClaimedAt}',to_jsonb((now()-interval '2 hours')::text)) WHERE id=%s",(intent.attemptId,))
    repository,request = runErasure(url,initial.userId)
    assert repository.getRequest(request)['retention_manifest']['financialObligationsAtDeletion']['paymentAttempts']==1
    assert repo.closeUncreatedOrder(intent.attemptId) is True
    assert query(url,'SELECT payment_status FROM billing_events WHERE id=%s',(intent.attemptId,),one=True)['payment_status']=='failed'


def test_approved_refund_survives_erasure_and_settles_without_identity_or_access(account):
    repo,_,initial,url = account
    intentId = str(uuid4())
    paymentId = 'pay-refund-'+intentId
    metadata = {'refundIntentId':intentId,'userId':initial.userId,'amount':100,'cutoff':NOW.isoformat(),
        'accessExpired':True,'currentAccessPreserved':False,'refundState':'reserved','submissions':{},
        'items':[{'paymentId':paymentId,'invoiceId':initial.invoiceId,'amount':100,'originalAmount':initial.amount,'currency':'INR'}],
        'providerRefunds':{},'privateAccountNotes':{'email':'private@example.test'}}
    query(url,"""INSERT INTO billing_events(id,user_id,invoice_id,event_category,event_type,event_status,metadata_json)
        VALUES(%s,%s,%s,'reconciliation','refund.intent','reserved',%s)""",(intentId,initial.userId,initial.invoiceId,Json(metadata)))
    runErasure(url,initial.userId)
    assert repo.claimRefundSubmission(intentId,paymentId) is True
    assert repo.claimRefundSubmission(intentId,paymentId) is False
    result = repo.settleRefundEvidence(intentId,{'refunds':[{'id':'rfnd-'+intentId,'payment_id':paymentId,
        'amount':100,'currency':'INR','status':'processed','notes':{'userId':initial.userId}}]})
    assert result['refundState']=='processed' and result['accessRestored'] is False
    active = query(url,'SELECT user_id,metadata_json FROM billing_events WHERE id=%s',(intentId,),one=True)
    assert active['user_id'] is None and initial.userId not in str(active['metadata_json'])
    assert 'private@example.test' not in str(active['metadata_json'])


@pytest.mark.parametrize('history',['absent','null','null_claim'])
def test_legacy_refund_unknown_send_history_stays_lookup_only_after_erasure(account,history):
    from types import SimpleNamespace
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    repo,_,initial,url = account
    intentId,paymentId = str(uuid4()),'pay-'+str(uuid4())
    metadata = {'refundIntentId':intentId,'userId':initial.userId,'amount':100,'cutoff':NOW.isoformat(),
        'accessExpired':True,'currentAccessPreserved':False,'refundState':'unknown','providerRefunds':{},
        'items':[{'paymentId':paymentId,'invoiceId':initial.invoiceId,'amount':100,'originalAmount':initial.amount,'currency':'INR'}]}
    if history=='null': metadata['submissions']=None
    if history=='null_claim': metadata['submissions']={paymentId:None}
    query(url,"""INSERT INTO billing_events(id,user_id,invoice_id,event_category,event_type,event_status,metadata_json)
        VALUES(%s,%s,%s,'reconciliation','refund.intent','unknown',%s)""",(intentId,initial.userId,initial.invoiceId,Json(metadata)))
    runErasure(url,initial.userId)
    row = query(url,'SELECT * FROM billing_events WHERE id=%s',(intentId,),one=True)
    calls = []
    refund = {'id':'refund-'+intentId,'payment_id':paymentId,'amount':100,'currency':'INR','status':'processed',
        'notes':{'refundIntentId':intentId,'userId':initial.userId}}
    def unexpected(*args):
        calls.append(args)
        raise AssertionError('Unknown send history must never initiate another provider refund')
    provider = SimpleNamespace(payment=SimpleNamespace(refund=unexpected,fetch=unexpected,
        fetch_multiple_refund=lambda *_:{'items':[refund]}))
    assert ManualBillingRecoveryService(repo,provider).recoverRefund(row)==1
    assert calls==[]


def test_retained_evidence_is_private_immutable_and_original_identity_cannot_return(account):
    _,_,initial,url = account
    _,request = runErasure(url,initial.userId)
    statements = [
        "SET LOCAL ROLE anon; SELECT * FROM private.retained_user_records",
        "SET LOCAL ROLE authenticated; SELECT * FROM private.retained_user_subjects",
        "SET LOCAL ROLE service_role; UPDATE private.retained_user_records SET retained_at=now()",
        "DELETE FROM private.retained_user_records WHERE request_id='"+request+"'",
        'TRUNCATE private.retained_user_records',
    ]
    for sql in statements:
        with pytest.raises(psycopg2.Error): query(url,sql)
    with pytest.raises(psycopg2.Error,match='ERASED_ACCOUNT_IDENTITY'):
        query(url,'INSERT INTO "Users"("userId") VALUES(%s)',(initial.userId,))


def test_stale_worker_cannot_mutate_or_finalize_a_reclaimed_request(account):
    from api.services.userErasureRetention import ErasureLeaseLost
    _,_,initial,url = account
    repository,request = startErasure(url,initial.userId)
    old,new = str(uuid4()),str(uuid4())
    repository.claimRequest(request,old,300)
    query(url,"UPDATE user_erasure_requests SET lease_expires_at=now()-interval '1 second' WHERE id=%s",(request,))
    assert repository.claimRequest(request,new,300)
    assert repository.renewLease(request,old,300) is False
    for call in (
        lambda:repository.startStep(request,'delete_storage',workerId=old),
        lambda:repository.failStep(request,'delete_storage','STORAGE_DELETE_FAILED',5,workerId=old),
        lambda:repository.saveInventory(request,['secret'],[],workerId=old),
        lambda:repository.deleteDatabaseData(request,initial.userId,[],workerId=old),
        lambda:repository.finalizeRequest(request,{},workerId=old),
        lambda:repository.startStep(request,'delete_storage'),
        lambda:repository.finalizeRequest(request,{}),
    ):
        with pytest.raises(ErasureLeaseLost): call()
    assert repository.getRequest(request)['worker_id']==new


@pytest.mark.parametrize('identity',['user','invoice','order_only'])
def test_late_webhook_keeps_financial_envelope_without_original_payload(account,identity):
    _,_,initial,url = account
    _,request = runErasure(url,initial.userId)
    order = query(url,'SELECT razorpay_order_id FROM "Invoices" WHERE id=%s',(initial.invoiceId,),one=True)['razorpay_order_id']
    payload = ({'payload':{'invoice':{'entity':{'notes':{'user_id':' '+initial.userId+' '}}}}} if identity=='invoice' else
        {'payload':{'payment':{'entity':{'order_id':order,'notes':{'userId':initial.userId} if identity=='user' else {},'email':'private@example.test'}}}})
    event = query(url,'''INSERT INTO "WebhookEvents"(id,status,user_id,payload) VALUES(%s,'RECEIVED',%s,%s) RETURNING *''',
        (str(uuid4()),initial.userId if identity=='user' else None,Json(payload)),one=True)
    assert event['user_id'] is None and event['payload']=={}
    assert str(event['erasure_request_id'])==request


def test_active_financial_keys_do_not_keep_original_user_identifier(account):
    _,_,initial,url = account
    runErasure(url,initial.userId)
    assert query(url,'SELECT id FROM billing_events WHERE idempotency_key LIKE %s',('%'+initial.userId+'%',))==[]


def test_arbitrary_invoice_snapshot_json_moves_to_private_original_only(account):
    _,_,initial,url = account
    snapshot = {'oldUnstructured':{'email':'private@example.test'}}
    query(url,'UPDATE "Invoices" SET pricing_reference_snapshot_json=%s,tax_breakdown_json=%s WHERE id=%s',
        (Json(snapshot),Json(snapshot),initial.invoiceId))
    runErasure(url,initial.userId)
    current = query(url,'SELECT pricing_reference_snapshot_json,tax_breakdown_json FROM "Invoices" WHERE id=%s',(initial.invoiceId,),one=True)
    assert 'private@example.test' not in str(current)
    original = query(url,"SELECT original_record FROM private.retained_user_records WHERE source_table='Invoices' AND source_id=%s",(initial.invoiceId,),one=True)
    assert original['original_record']['pricing_reference_snapshot_json']==snapshot


def test_historical_unlinked_webhook_is_scrubbed_by_owned_financial_identity(account):
    _,_,initial,url = account
    order = query(url,'SELECT razorpay_order_id FROM "Invoices" WHERE id=%s',(initial.invoiceId,),one=True)['razorpay_order_id']
    payload = {'payload':{'payment':{'entity':{'order_id':order,'email':'private@example.test'}}}}
    event = query(url,'''INSERT INTO "WebhookEvents"(payload,status) VALUES(%s,'completed') RETURNING id''',
        (Json(payload),),one=True)['id']
    runErasure(url,initial.userId)
    assert query(url,'SELECT payload FROM "WebhookEvents" WHERE id=%s',(event,),one=True)['payload']=={}


@pytest.mark.parametrize('operation',['bind','observe','claim','close'])
def test_erasure_between_financial_owner_read_and_lock_is_safe(account,monkeypatch,operation):
    from api.services.billing.manualBillingContracts import CheckoutProviderSnapshot
    repo,_,initial,url = account
    intent = pendingCheckout(account)
    order = 'order-'+intent.attemptId
    if operation=='observe': repo.bindProviderOrder(intent.attemptId,{'id':order})
    locked = repo._lockUser
    triggered = False
    def eraseBeforeLock(cursor,user):
        nonlocal triggered
        if not triggered:
            triggered=True
            runErasure(url,initial.userId)
        return locked(cursor,user)
    monkeypatch.setattr(repo,'_lockUser',eraseBeforeLock)
    if operation=='bind':
        result = repo.bindProviderOrder(intent.attemptId,{'id':order})
        assert result.razorpayOrderId==order
    elif operation=='observe':
        repo.recordProviderObservation(intent.attemptId,CheckoutProviderSnapshot(intent.attemptId,order,
            'unpaid',NOW,True,intent.amount,'INR',()))
    elif operation=='claim':
        assert repo.claimProviderOrderCreation(intent.attemptId) is False
    else:
        assert repo.closeUncreatedOrder(intent.attemptId) is False  # too recent to prove absence
    assert query(url,'SELECT id FROM subscriptions WHERE user_id=%s',(initial.userId,))==[]


def test_completed_database_deletion_resumes_verification_using_durable_inventory(account):
    _,_,initial,url = account
    project = str(uuid4())
    query(url,'INSERT INTO "Projects"("projectId","ownerUserId") VALUES(%s,%s)',(project,initial.userId))
    repository,request = startErasure(url,initial.userId)
    first = FakeExternalCleanup(failStep='verify_external')
    assert UserErasureTask(repository,first,FakeAudit()).execute(request)['status']=='PARTIALLY_FAILED'
    assert repository.verifyDatabaseErasure(initial.userId)=={}
    assert repository.retryRequest(request)
    second = FakeExternalCleanup()
    assert UserErasureTask(repository,second,FakeAudit()).execute(request)['status']=='COMPLETED'
    assert ('verify_external',initial.userId,(project,)) in second.calls
    assert not any(call[0]=='delete_auth_identity' for call in second.calls)


def test_protected_relocation_requires_exact_copy_and_current_frozen_lease(account):
    from api.services.userErasureRetention import archiveOriginals
    _,_,initial,url = account
    repository,request = startErasure(url,initial.userId)
    worker = str(uuid4())
    repository.claimRequest(request,worker,300)
    repository.prepareRetention(request,initial.userId,worker)
    event = query(url,"""INSERT INTO billing_events(user_id,event_category,event_type,event_status,metadata_json)
        VALUES(%s,'audit','subscription.legacy_snapshot','COMMITTED',%s) RETURNING id""",
        (initial.userId,Json({'original':'unchanged'})),one=True)['id']
    # No copy is never a valid relocation.
    with pytest.raises(psycopg2.Error,match='immutable'):
        query(url,'DELETE FROM billing_events WHERE id=%s',(event,))
    with psycopg2.connect(url) as db, db.cursor() as cursor:
        archiveOriginals(cursor,request,initial.userId)
    # Exact copies do not authorise ordinary updates or deletion after lease loss.
    with pytest.raises(psycopg2.Error,match='immutable'):
        query(url,"UPDATE billing_events SET metadata_json='{}' WHERE id=%s",(event,))
    query(url,"UPDATE user_erasure_requests SET lease_expires_at=now()-interval '1 second' WHERE id=%s",(request,))
    with pytest.raises(psycopg2.Error,match='immutable'):
        query(url,'DELETE FROM billing_events WHERE id=%s',(event,))


@pytest.mark.parametrize('status,ambiguous,expected',[
    ('PENDING',False,'CANCELLED'),('RETRY_PENDING',False,'CANCELLED'),
    ('SENDING',True,'RETRY_PENDING'),('RETRY_PENDING',True,'RETRY_PENDING'),
    ('ACCEPTED',False,'ACCEPTED'),('DELIVERED',False,'DELIVERED')])
def test_erasure_scrubs_notices_and_keeps_ambiguous_reconciliation_only(account,status,ambiguous,expected):
    _,_,initial,url = account
    subscription = query(url,'SELECT id FROM subscriptions WHERE user_id=%s',(initial.userId,),one=True)['id']
    notice = query(url,'''INSERT INTO notification_deliveries(notification_type,template_version,dedupe_key,
        user_id,subscription_id,period_end,status,metadata_json,last_error_code,submission_started_at,
        provider_message_id,next_reconcile_at) VALUES('payment_receipt','1',%s,%s,%s,now(),%s,%s,%s,%s,%s,now()) RETURNING id''',
        ('test:'+str(uuid4()),initial.userId,subscription,status,Json({'email':'private@example.test'}),
         'AMBIGUOUS_SEND' if ambiguous else None,NOW if ambiguous else None,
         'message-'+str(uuid4()) if status in ('ACCEPTED','DELIVERED') else None),one=True)['id']
    runErasure(url,initial.userId)
    row = query(url,'SELECT * FROM notification_deliveries WHERE id=%s',(notice,),one=True)
    assert row['status']==expected and row['user_id'] is None and row['metadata_json']=={}
    if expected=='RETRY_PENDING':
        assert row['last_error_code']=='AMBIGUOUS_SEND' and row['next_reconcile_at'] is not None
        assert not query(url,'SELECT id FROM claim_notification_deliveries(%s,500,300) WHERE id=%s',('synthetic-worker',notice))


def test_failed_database_delete_rolls_back_original_copy_and_scrub(account):
    _,_,initial,url = account
    repository,request = startErasure(url,initial.userId)
    query(url,'''CREATE FUNCTION public.synthetic_erasure_failure() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'synthetic final deletion failure'; END $$;
        CREATE TRIGGER synthetic_erasure_failure BEFORE DELETE ON "Users" FOR EACH ROW EXECUTE FUNCTION public.synthetic_erasure_failure()''')
    try:
        result = UserErasureTask(repository,FakeExternalCleanup(),FakeAudit()).execute(request)
        assert result['status']=='PARTIALLY_FAILED'
        assert query(url,'SELECT id FROM "Invoices" WHERE "userId"=%s',(initial.userId,))
        assert query(url,'SELECT source_id FROM private.retained_user_records WHERE request_id=%s',(request,))==[]
    finally:
        query(url,'DROP TRIGGER synthetic_erasure_failure ON "Users"; DROP FUNCTION public.synthetic_erasure_failure()')
