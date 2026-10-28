"""First contracted-storage boundary: native bootstrap and once-only trial.

Other payment/admin/worker consumers remain separate acceptance gates.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import timedelta
import os
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from api.services.subscriptions.subscriptionModel import SubscriptionAuthorityUnavailable
from api.services.subscriptions.subscriptionModelBackfill import legacyRow,legacyRowHash,jsonValue
from api.services.subscriptions.subscriptionModelRepository import SubscriptionModelRepository
from api.services.subscriptions.subscriptionSnapshotService import SubscriptionSnapshotService
from test.subscriptionContractFixtures import actualSchema,bootstrapSchema,bootstrapStorage,bootstrapAccount,columns  # noqa: F401
from test.subscriptionBackfillFixtures import source

pytestmark=pytest.mark.skipif(os.environ.get('RUN_MANUAL_BILLING_INTEGRATION')!='1'
    or os.environ.get('RUN_LIVE_SCHEMA_INTEGRATION')!='1',reason='Guarded actual-shape PostgreSQL opt-in; skip is UNVERIFIED')


def query(factory,statement,args=(),one=False):
    with closing(factory()) as db,db,db.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(statement,args)
        return cursor.fetchone() if one else list(cursor.fetchall()) if cursor.description else None


def snapshot(repo,user,now=None):
    return SubscriptionSnapshotService(repo.runtimeSnapshotFactsLoader).get(user,now)


def test_native_canonical_bootstrap_replays_without_mutation(bootstrapAccount):
    repo,factory,user,shape=bootstrapAccount
    row=repo.ensureCanonicalSubscription(user)
    assert row['user_id']==user and row['is_canonical'] is True
    assert repo.ensureCanonicalSubscription(user)==row
    result=snapshot(repo,user)
    assert result.status=='none' and result.trialAvailable and not result.accessAllowed
    event=query(factory,"SELECT metadata_json FROM billing_events WHERE user_id=%s AND event_type='subscription.model_initialized'",(user,),True)
    assert event['metadata_json']['storageShape']==shape
    assert len(query(factory,'SELECT id FROM subscriptions WHERE user_id=%s',(user,)))==1


def test_concurrent_bootstrap_keeps_one_canonical_and_one_initialization(bootstrapAccount):
    repo,factory,user,_=bootstrapAccount
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows=list(pool.map(repo.ensureCanonicalSubscription,[user,user]))
    assert rows[0]['id']==rows[1]['id']
    assert len(query(factory,"SELECT id FROM billing_events WHERE user_id=%s AND event_type='subscription.model_initialized'",(user,)))==1


def test_historical_only_account_never_gets_a_new_canonical(bootstrapAccount):
    repo,factory,user,_=bootstrapAccount
    query(factory,'INSERT INTO subscriptions(id,user_id,is_canonical) VALUES(%s,%s,false)',(str(uuid4()),user))
    with pytest.raises(ValueError,match='CANONICAL_BACKFILL_REQUIRED'):
        repo.ensureCanonicalSubscription(user)
    assert not query(factory,'SELECT id FROM subscriptions WHERE user_id=%s AND is_canonical',(user,))


def test_nonexistent_owner_rolls_back_initialization(bootstrapAccount):
    repo,factory,_,_=bootstrapAccount;missing='missing-'+str(uuid4())
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):repo.ensureCanonicalSubscription(missing)
    assert not query(factory,'SELECT id FROM subscriptions WHERE user_id=%s',(missing,))
    assert not query(factory,'SELECT id FROM billing_events WHERE user_id=%s',(missing,))


def test_trial_uses_current_core_facts_and_real_idempotent_credit_allocation(bootstrapAccount):
    from api.services.credits.creditConfig import getTokenQuotaForPlan
    repo,factory,user,_=bootstrapAccount
    row=repo.ensureCanonicalSubscription(user)
    trial=repo.activateTrial(user,('banking','telecom'))
    result=snapshot(repo,user)
    assert result.status=='trial' and result.accessAllowed and not result.trialAvailable
    assert result.trial.endsAt-result.trial.startedAt==timedelta(days=12)
    assert set(result.subscribedExperts)=={'banking','telecom'}
    query(factory,'UPDATE credit_balances SET topup_tokens=2500 WHERE user_id=%s',(user,))
    balance=query(factory,'SELECT * FROM credit_balances WHERE user_id=%s',(user,),True)
    assert balance['subscription_id']==row['id']
    assert balance['monthly_token_quota']==getTokenQuotaForPlan('free',2)
    assert balance['remaining_tokens']==balance['monthly_token_quota'] and balance['used_tokens']==0
    assert balance['topup_tokens']==2500
    with pytest.raises(ValueError,match='TRIAL_NOT_ELIGIBLE'):repo.activateTrial(user,('banking','telecom'))
    assert query(factory,'SELECT * FROM credit_balances WHERE user_id=%s',(user,),True)==balance
    ended=snapshot(repo,user,trial['trial_ends_at'])
    assert ended.status=='expired' and not ended.accessAllowed and not ended.trialAvailable


def test_concurrent_trial_requests_commit_one_trial_and_one_allowance(bootstrapAccount):
    from api.services.credits.creditConfig import getTokenQuotaForPlan
    repo,factory,user,_=bootstrapAccount;repo.ensureCanonicalSubscription(user)
    def activate(_):
        try:return repo.activateTrial(user,('banking',))
        except ValueError as error:return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(activate,range(2)))
    assert sum(isinstance(value,dict) for value in results)==1
    assert results.count('TRIAL_NOT_ELIGIBLE')==1
    assert len(query(factory,"SELECT id FROM billing_events WHERE user_id=%s AND event_type='subscription.trial_started'",(user,)))==1
    balance=query(factory,'SELECT * FROM credit_balances WHERE user_id=%s',(user,),True)
    assert balance['remaining_tokens']==balance['monthly_token_quota']==getTokenQuotaForPlan('free',1)
    assert balance['used_tokens']==0


@pytest.mark.parametrize('restriction',['ban','erasure'])
def test_restrictions_prevent_trial_without_creating_credit_or_trial_history(bootstrapAccount,restriction):
    repo,factory,user,_=bootstrapAccount;repo.ensureCanonicalSubscription(user)
    if restriction=='ban':
        admin=query(factory,"INSERT INTO admin_users(email,name,password_hash) VALUES(%s,'Synthetic','not-a-real-password') RETURNING id",(str(uuid4())+'@example.test',),True)['id']
        query(factory,'UPDATE "Users" SET "isBanned"=true,"bannedAt"=now(),"bannedBy"=%s WHERE "userId"=%s',(admin,user))
    else:query(factory,'UPDATE subscriptions SET erasure_pending=true WHERE user_id=%s',(user,))
    try:
        with pytest.raises(ValueError,match='TRIAL_NOT_ELIGIBLE'):repo.activateTrial(user,('banking',))
        assert not query(factory,'SELECT user_id FROM credit_balances WHERE user_id=%s',(user,))
        assert not query(factory,"SELECT id FROM billing_events WHERE user_id=%s AND event_type='subscription.trial_started'",(user,))
        if restriction=='erasure':
            with closing(factory()) as db,db,db.cursor(cursor_factory=RealDictCursor) as cursor:
                row=query(factory,'SELECT * FROM subscriptions WHERE user_id=%s',(user,),True)
                with pytest.raises(ValueError,match='REVIEW_ERASURE_PENDING'):
                    SubscriptionModelRepository(factory).archiveLegacySnapshot(cursor,row)
    finally:
        # This temporary negative fixture is not a real erasure request. The
        # subsequent module contraction must not archive an active erasure.
        if restriction=='erasure':
            query(factory,'UPDATE subscriptions SET erasure_pending=false WHERE user_id=%s',(user,))


def oldNative(repo,factory,user):
    row=query(factory,'INSERT INTO subscriptions(id,user_id,is_canonical) VALUES(%s,%s,true) RETURNING *',(str(uuid4()),user),True)
    with closing(factory()) as db,db,db.cursor(cursor_factory=RealDictCursor) as cursor:
        SubscriptionModelRepository(factory)._event(cursor,row,'subscription.model_initialized','native-old:'+str(row['id']),{'schemaVersion':2})
    return row


def archive(factory,row,consumed=False,valid=True,signal=None,partial=False,foreign=False):
    original=deepcopy(source('none')[0]);original.update(id=str(row['id']),user_id=row['user_id'])
    if foreign:original['user_id']='foreign-'+str(uuid4())
    original['billing_state']['trialConsumed']=consumed
    if signal=='timestamp':original['billing_state']['trialConsumedAt']='2026-01-01T00:00:00+00:00'
    if signal=='free':original['plan_type']='free'
    if signal=='status':original['status']='expired'
    if signal=='paid':original['plan_type']='pro'
    if signal=='mode':original['billing_mode']='monthly_prepaid'
    if signal=='admin':original['admin_credit_generation']=1
    if partial:original.pop('pending_additions')
    payload={'archive':{'schemaVersion':2,'originalRow':jsonValue(legacyRow(original)),
        'legacyHash':legacyRowHash(original) if valid else 'invalid'}}
    with closing(factory()) as db,db,db.cursor(cursor_factory=RealDictCursor) as cursor:
        SubscriptionModelRepository(factory)._event(cursor,row,'subscription.legacy_snapshot','archive-test:'+str(uuid4()),payload)


@pytest.mark.parametrize('proof',['missing','invalid','foreign','consumed','unused'])
def test_retired_history_needs_owned_original_proof_before_allowing_old_native_trial(bootstrapAccount,proof):
    repo,factory,user,shape=bootstrapAccount
    row=oldNative(repo,factory,user)
    if proof!='missing':archive(factory,row,consumed=proof=='consumed',valid=proof!='invalid',foreign=proof=='foreign')
    if proof=='unused' or shape=='expanded' and proof in ('missing','invalid','foreign'):
        assert snapshot(repo,user).trialAvailable
    else:
        with pytest.raises(SubscriptionAuthorityUnavailable):snapshot(repo,user)


def test_retained_native_marker_cannot_override_an_original_consumed_trial_archive(bootstrapAccount):
    repo,factory,user,shape=bootstrapAccount
    row=repo.ensureCanonicalSubscription(user);archive(factory,row,consumed=True)
    with pytest.raises(SubscriptionAuthorityUnavailable):snapshot(repo,user)


@pytest.mark.parametrize('proof',['checkpoint','partial'])
def test_retained_checkpoint_does_not_prove_missing_precontraction_trial_history(bootstrapAccount,proof):
    repo,factory,user,shape=bootstrapAccount
    row=oldNative(repo,factory,user)
    if proof=='partial':archive(factory,row,partial=True)
    else:
        with closing(factory()) as db,db,db.cursor(cursor_factory=RealDictCursor) as cursor:
            SubscriptionModelRepository(factory).archiveLegacySnapshot(cursor,row)
    if shape=='expanded':
        assert snapshot(repo,user).trialAvailable
    else:
        with pytest.raises(SubscriptionAuthorityUnavailable):snapshot(repo,user)


@pytest.mark.parametrize('signal',['timestamp','free','status','paid','mode','admin'])
@pytest.mark.parametrize('partial',[False,True])
def test_every_existing_consumed_trial_signal_in_original_history_blocks_a_new_trial(bootstrapAccount,signal,partial):
    repo,factory,user,_=bootstrapAccount
    row=repo.ensureCanonicalSubscription(user);archive(factory,row,signal=signal,partial=partial)
    with pytest.raises(SubscriptionAuthorityUnavailable):snapshot(repo,user)


def test_public_trial_activation_returns_committed_core_dates_and_preserves_allocation(bootstrapAccount,monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from jose import jwt
    from api.services.billing import manualBillingRepository
    from api.services.subscriptions.subscriptionService import SubscriptionService
    from utils.exceptionHandler import CustomException
    repo,factory,user,_=bootstrapAccount
    monkeypatch.setattr(manualBillingRepository,'getManualBillingRepository',lambda:repo)
    service=SubscriptionService.__new__(SubscriptionService)
    # Only remote profile/email/audit/session boundaries are synthetic. The
    # activation, immutable trial event and credit allocation use real SQL.
    service.client=MagicMock()
    records=SimpleNamespace(data=[{'fullName':'Synthetic','userId':user,'email':user+'@example.test'}])
    service.client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value=records
    service._sendFreeTrialEmail=MagicMock()
    service._auditLog=MagicMock()
    token=jwt.encode({'userId':user,'email':user+'@example.test'},os.environ['SECRET_KEY'],'HS256')
    result=service.activateFreeTrial(token)
    committed=query(factory,'SELECT * FROM subscriptions WHERE user_id=%s',(user,),True)
    assert datetime.fromisoformat(result['subscriptionStart'])==committed['trial_started_at']
    assert datetime.fromisoformat(result['subscriptionExpiry'])==committed['trial_ends_at']
    assert result['subscriptionStatus']=='TRIAL' and result['subscriptionDaysLeft']==12
    assert jwt.decode(result['accessToken'],os.environ['SECRET_KEY'],algorithms=['HS256'])['userId']==user
    balance=query(factory,'SELECT * FROM credit_balances WHERE user_id=%s',(user,),True)
    assert balance['subscription_id']==committed['id'] and balance['domain_count']==4
    assert snapshot(repo,user).accessAllowed
    with pytest.raises(CustomException) as failure:service.activateFreeTrial(token)
    assert failure.value.statusCode==409
    assert query(factory,'SELECT * FROM credit_balances WHERE user_id=%s',(user,),True)==balance


@pytest.mark.parametrize('failure',['raises','retryable'])
def test_freeTrialActivationSurvivesEmailFailure(bootstrapAccount,monkeypatch,failure):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    import requests
    from jose import jwt
    from loguru import logger
    from api.services.billing import manualBillingRepository
    from api.services.notifications.brevoEmailSender import BrevoEmailSender,EmailSendResult
    from api.services.subscriptions.subscriptionService import SubscriptionService
    repo,factory,user,_=bootstrapAccount
    monkeypatch.setattr(manualBillingRepository,'getManualBillingRepository',lambda:repo)
    email=user+'@example.test'
    service=SubscriptionService.__new__(SubscriptionService)
    service.client=MagicMock()
    records=SimpleNamespace(data=[{'fullName':'Synthetic','userId':user,'email':email}])
    service.client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value=records
    service._auditLog=MagicMock()
    token=jwt.encode({'userId':user,'email':email},os.environ['SECRET_KEY'],'HS256')
    sender=MagicMock(side_effect=requests.ConnectionError('down')) if failure=='raises' else MagicMock(
        return_value=EmailSendResult('RETRYABLE',errorCode='BREVO_HTTP_503'))
    monkeypatch.setattr(BrevoEmailSender,'sendFreeTrialWelcome',sender)
    captured=[];sinkId=logger.add(captured.append,level='DEBUG')
    try:
        result=service.activateFreeTrial(token)
    finally:
        logger.remove(sinkId)
    logged=''.join(str(record) for record in captured)
    assert result['subscriptionStatus']=='TRIAL'
    assert email not in logged and user+'@' not in logged
    assert ('BREVO_HTTP_503' in logged) if failure=='retryable' else ('ConnectionError' in logged)
    committed=query(factory,'SELECT * FROM subscriptions WHERE user_id=%s',(user,),True)
    args=sender.call_args.args
    assert args[0]==email and args[2]==committed['trial_ends_at'] and isinstance(args[3],list)

@pytest.mark.parametrize('condition',['none','trial','paid'])
def test_reviewed_existing_canonical_and_financial_records_are_read_without_rewrite(bootstrapStorage,monkeypatch,condition):
    from api.services.billing.manualBillingRepository import ManualBillingRepository
    from test.subscriptionBackfillFixtures import NOW
    shape,factory,reviewed=bootstrapStorage
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE','contracted' if shape=='retained' else 'v2')
    user=reviewed[condition];repo=ManualBillingRepository(factory)
    before=query(factory,'SELECT * FROM subscriptions WHERE user_id=%s',(user,),True)
    balances=query(factory,'SELECT * FROM credit_balances WHERE user_id=%s',(user,))
    events=query(factory,'SELECT * FROM billing_events WHERE user_id=%s ORDER BY id',(user,))
    invoices=query(factory,'SELECT * FROM "Invoices" WHERE "userId"=%s ORDER BY id',(user,))
    assert repo.ensureCanonicalSubscription(user)['id']==before['id']
    value=snapshot(repo,user,NOW)
    assert value.status=={'none':'none','trial':'trial','paid':'active'}[condition]
    assert query(factory,'SELECT * FROM subscriptions WHERE user_id=%s',(user,),True)==before
    assert query(factory,'SELECT * FROM credit_balances WHERE user_id=%s',(user,))==balances
    assert query(factory,'SELECT * FROM billing_events WHERE user_id=%s ORDER BY id',(user,))==events
    assert query(factory,'SELECT * FROM "Invoices" WHERE "userId"=%s ORDER BY id',(user,))==invoices
