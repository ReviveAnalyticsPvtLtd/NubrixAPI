import os
import pytest

from api.services.billing.manualBillingContracts import CheckoutRequest, VerifiedPaymentEvidence
from test.subscriptionRuntimeFixtures import (  # noqa: F401
    NOW, account, runtimeDb, postgres, query, selection, renewal, capture,
    expirePurchasedCoverage,
)

pytestmark = pytest.mark.skipif(os.environ.get('RUN_MANUAL_BILLING_INTEGRATION') != '1',
    reason='Guarded disposable PostgreSQL opt-in; skip is UNVERIFIED')


def workerFor(account):
    from api.services.subscriptions.subscriptionWorkerService import SubscriptionWorkerService
    repo, _, initial, url = account
    class Owners:
        def table(self,name):
            assert name == 'subscriptions'
            return self
        def select(self,_):
            return self
        def eq(self,*_):
            return self
        def execute(self):
            from types import SimpleNamespace
            return SimpleNamespace(data=query(url,'SELECT id,user_id,erasure_pending FROM subscriptions WHERE user_id=%s',(initial.userId,)))
    return SubscriptionWorkerService(Owners(),repo)


@pytest.mark.parametrize('account',['monthly_prepaid','annual_prepaid'],indirect=True)
def test_delayed_worker_cannot_refill_or_extend_unpaid_time(account):
    from api.services.subscriptions.subscriptionSnapshotService import SubscriptionSnapshotService
    repo, _, initial, url = account
    snapshot = SubscriptionSnapshotService(repo.runtimeSnapshotFactsLoader).get(initial.userId,NOW)
    before = query(url,'SELECT * FROM credit_balances WHERE user_id=%s',(initial.userId,),one=True)
    query(url,"UPDATE subscriptions SET current_period_end='2099-01-01',status='active' WHERE user_id=%s",(initial.userId,))
    result = workerFor(account).execute(now=snapshot.currentPeriod.end)
    assert result['errors']==0 and result['materialized']==0
    assert not SubscriptionSnapshotService(repo.runtimeSnapshotFactsLoader).get(initial.userId,snapshot.currentPeriod.end).accessAllowed
    assert query(url,'SELECT * FROM credit_balances WHERE user_id=%s',(initial.userId,),one=True)==before


@pytest.mark.parametrize('account',['annual_prepaid'],indirect=True)
def test_annual_worker_t30_and_t7_are_durable_milestones_not_statuses(account,monkeypatch):
    from datetime import timedelta
    from api.services.subscriptions.subscriptionSnapshotService import SubscriptionSnapshotService
    repo, _, initial, url = account
    owned = SubscriptionSnapshotService(repo.runtimeSnapshotFactsLoader).get(initial.userId,NOW)
    before = query(url,'SELECT status,version FROM subscriptions WHERE user_id=%s',(initial.userId,),one=True)
    query(url,"UPDATE subscriptions SET current_period_end='2099-01-01' WHERE user_id=%s",(initial.userId,))
    for days in (20,5,0.5):
        now = owned.currentPeriod.end-timedelta(days=days)
        monkeypatch.setattr('api.services.billing.manualBillingRepository._now',lambda:now)
        result = workerFor(account).execute(interval='year',reminders=True,now=now)
        assert result['errors']==0 and result['enqueued']==1
    events = query(url,"SELECT metadata_json FROM billing_events WHERE user_id=%s AND event_type='email.billing_intent.committed' AND metadata_json->>'notificationType'<>'payment_receipt' ORDER BY occurred_at,id",(initial.userId,))
    assert {e['metadata_json']['notificationType'] for e in events} == {
        'annual_renewal_upcoming','monthly_renewal_ready','monthly_renewal_reminder'}
    assert all(e['metadata_json']['metadata']['billingInterval']=='year' for e in events)
    assert all(e['metadata_json']['metadata'].get('renewalDeadline')==owned.currentPeriod.end.isoformat() for e in events)
    import psycopg2
    from api.services.notifications.notificationDeliveryRepository import NotificationDeliveryRepository
    deliveries = NotificationDeliveryRepository(lambda:psycopg2.connect(url))
    for event in events:
        intent = event['metadata_json']
        deliveries.enqueueBillingNotification(intent['userId'],intent['subscriptionId'],intent['notificationType'],
            intent['dedupeKey'],intent['periodEnd'],intent['metadata'])
    assert query(url,'SELECT status,version FROM subscriptions WHERE user_id=%s',(initial.userId,),one=True)==before
    repo.setRenewalOptOut(initial.userId,True,'synthetic optout','worker-annual-optout')
    assert workerFor(account).execute(interval='year',reminders=True,now=now)['enqueued']==0


def test_worker_and_lazy_read_keep_one_allocation_and_used_balance(account):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repo, _, initial, url = account
    credits = ManualCreditRepository(repo)
    before = credits.balanceSnapshot(initial.userId)
    query(url,'UPDATE credit_balances SET remaining_tokens=remaining_tokens-17,used_tokens=used_tokens+17 WHERE user_id=%s',(initial.userId,))
    for _ in range(2):
        assert workerFor(account).execute(now=NOW)['errors']==0
        credits.balanceSnapshot(initial.userId)
    row = query(url,'SELECT * FROM credit_balances WHERE user_id=%s',(initial.userId,),one=True)
    assert row['remaining_tokens']==before['remaining_tokens']-17
    markers = query(url,"SELECT id FROM billing_events WHERE user_id=%s AND idempotency_key LIKE 'credit-window:%%'",(initial.userId,))
    assert len(markers)==1


def test_erasure_explicitly_blocks_before_mutating_immutable_trial_evidence(account):
    import psycopg2
    from api.adminErrors import AdminApiError
    from api.services.userErasureRepository import UserErasureRepository
    repo, _, initial, url = account
    user, _ = newUnifiedTrial(account)
    erasure = UserErasureRepository(lambda:psycopg2.connect(url))
    erasure.freezeBilling(user)
    with pytest.raises(AdminApiError,match='SUBSCRIPTION_ERASURE_RETENTION_PENDING'):
        erasure.deleteDatabaseData('synthetic-request',user,[])
    assert query(url,'SELECT "userId" FROM "Users" WHERE "userId"=%s',(user,),one=True)
    assert len(query(url,"SELECT id FROM billing_events WHERE user_id=%s AND event_type IN ('subscription.model_initialized','subscription.trial_started')",(user,)))==2


@pytest.mark.parametrize('change,expected',[('unchanged','AUTHORIZED'),('revision','REJECTED'),('money_hold','HELD'),('closed_with_money','REJECTED')])
def test_dispatch_fences_invoice_revision_and_holds_unknown_money(account,monkeypatch,change,expected):
    from datetime import timedelta
    from api.services.notifications.notificationDeliveryRepository import NotificationDeliveryRepository
    import psycopg2
    repo, _, initial, url = account
    from dateutil.relativedelta import relativedelta
    # Keep the actual database dispatch clock inside T-7. This also proves
    # rejection isn't an accidental result of a simulated future-only window.
    end = NOW+timedelta(days=5)
    query(url,'UPDATE "Invoices" SET period_start=%s,period_end=%s WHERE id=%s',
        (end-relativedelta(months=1),end,initial.invoiceId))
    query(url,'UPDATE credit_balances SET period_start=%s,period_end=%s WHERE user_id=%s',
        (end-relativedelta(months=1),end,initial.userId))
    when = NOW
    assert workerFor(account).execute(reminders=True,now=when)['enqueued']==1
    intent = query(url,"SELECT metadata_json FROM billing_events WHERE user_id=%s AND event_type='email.billing_intent.committed' AND metadata_json->>'notificationType'='monthly_renewal_ready'",(initial.userId,),one=True)['metadata_json']
    deliveries = NotificationDeliveryRepository(lambda:psycopg2.connect(url))
    row,_ = deliveries.enqueueBillingNotification(intent['userId'],intent['subscriptionId'],intent['notificationType'],intent['dedupeKey'],intent['periodEnd'],intent['metadata'])
    query(url,"UPDATE notification_deliveries SET status='SENDING',lease_owner='worker',claimed_payload_version=payload_version WHERE id=%s",(row['id'],))
    if change=='revision':
        query(url,"UPDATE \"Invoices\" SET metadata_json=jsonb_set(metadata_json,'{manualBilling,revision}','2') WHERE id=%s",(intent['metadata']['invoiceId'],))
    if change in ('money_hold','closed_with_money'):
        query(url,"""INSERT INTO billing_events(user_id,subscription_id,invoice_id,event_category,event_type,event_status)
            VALUES(%s,%s,%s,'reconciliation','payment.capture','REQUIRES_RECONCILIATION')""",
            (initial.userId,intent['subscriptionId'],intent['metadata']['invoiceId']))
    if change=='closed_with_money':
        query(url,'UPDATE "Invoices" SET status=\'VOID\' WHERE id=%s',(intent['metadata']['invoiceId'],))
    assert deliveries.authorizeBillingSubmissionResult(str(row['id']),'worker',row['payload_version'])==expected
    submitted = query(url,'SELECT submission_started_at FROM notification_deliveries WHERE id=%s',(row['id'],),one=True)['submission_started_at']
    assert (submitted is not None)==(expected=='AUTHORIZED')


@pytest.mark.parametrize('account',['annual_prepaid'],indirect=True)
def test_annual_expert_removal_service_uses_owned_selection(account,monkeypatch):
    import os
    import jwt
    from api.services.subscriptions.subscriptionService import SubscriptionService
    repo, _, initial, url = account
    class Owner:
        data = [{'userId':initial.userId}]
        def table(self,*_): return self
        def select(self,*_): return self
        def eq(self,*_): return self
        def execute(self): return self
    service = SubscriptionService.__new__(SubscriptionService)
    service.VALID_DOMAINS = {'banking','manufacturing','supplychain','telecom'}
    service.client = Owner()
    service._getCanonicalSubscription = lambda **_:repo.getRenewalContext(initial.userId,NOW)
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repo)
    token = jwt.encode({'userId':initial.userId,'email':'synthetic@example.test'},os.environ['SECRET_KEY'],algorithm='HS256')
    assert service.removeDomain(['manufacturing'],token)['pendingRemovals']==['manufacturing']
    rows = query(url,"SELECT metadata_json FROM billing_events WHERE user_id=%s AND event_type='subscription.next_selection'",(initial.userId,))
    assert len(rows)==1


@pytest.mark.parametrize('account',['annual_prepaid'],indirect=True)
def test_opted_out_annual_worker_preserves_purchased_next_year(account,monkeypatch):
    from api.services.subscriptions.subscriptionSnapshotService import SubscriptionSnapshotService
    repo, _, initial, url = account
    invoice = repo.createAnnualRenewalInvoice(initial.userId)
    checkout = repo.reserveCheckout(CheckoutRequest(initial.userId,'renewal','annual_prepaid',{'invoiceId':str(invoice['id'])},expectedCatalogRevision=1))
    capture(repo,checkout)
    service = SubscriptionSnapshotService(repo.runtimeSnapshotFactsLoader)
    before = service.get(initial.userId,NOW)
    repo.setRenewalOptOut(initial.userId,True,'synthetic optout','worker-paid-future-optout')
    assert workerFor(account).execute(reminders=True,now=NOW)['enqueued']==0
    after = service.get(initial.userId,NOW)
    assert after.nextPeriod==before.nextPeriod and after.paidUntil==before.paidUntil
    assert after.accessAllowed and after.renewalOptOut


@pytest.mark.parametrize('account', ['monthly_prepaid', 'annual_prepaid'], indirect=True)
def test_runtime_snapshot_follows_financial_writes_without_backfill_stamp_rewrites(account):
    from api.services.subscriptions.subscriptionSnapshotService import SubscriptionSnapshotService
    repo, _, initial, url = account
    service = SubscriptionSnapshotService(repo.runtimeSnapshotFactsLoader)
    before = service.get(initial.userId, NOW)
    assert before.status == 'active' and before.domainCount == 2
    repo.setRenewalOptOut(initial.userId, True, 'Synthetic preference', 'runtime-snapshot-optout')
    assert service.get(initial.userId, NOW).renewalOptOut
    addition = repo.reserveCheckout(CheckoutRequest(initial.userId, 'expert_addition', before.currentPeriod and ('annual_prepaid' if before.billingInterval == 'year' else 'monthly_prepaid'), {'domains':['telecom']}, expectedCatalogRevision=1))
    capture(repo, addition)
    assert service.get(initial.userId, NOW).subscribedExperts == ('banking','manufacturing','telecom')
    expired = service.get(initial.userId, before.currentPeriod.end)
    assert expired.status == 'expired' and expired.purchaseAllowed and not expired.accessAllowed


def newUnifiedTrial(account):
    from uuid import uuid4
    repo, _, _, url = account
    user = 'trial-consumer-' + str(uuid4())
    query(url, 'INSERT INTO "Users"("userId") VALUES(%s)', (user,))
    row = repo.ensureCanonicalSubscription(user)
    activated = repo.activateTrial(user, ('banking','telecom'))
    return user, activated


def test_new_trial_explicit_dates_are_once_only_and_ignore_legacy_cache(account):
    from api.services.subscriptions.subscriptionSnapshotService import SubscriptionSnapshotService
    repo, _, _, url = account
    user, row = newUnifiedTrial(account)
    service = SubscriptionSnapshotService(repo.runtimeSnapshotFactsLoader)
    trial = service.get(user)
    assert trial.status == 'trial' and trial.trial.startedAt and trial.trial.endsAt
    assert trial.subscribedExperts == ('banking','telecom')
    query(url, "UPDATE subscriptions SET current_period_end='2099-01-01',status='active',subscribed_experts='[\"supplychain\"]' WHERE user_id=%s", (user,))
    assert service.get(user, trial.trial.endsAt).status == 'expired'
    with pytest.raises(ValueError, match='TRIAL_NOT_ELIGIBLE'):
        repo.activateTrial(user, ('banking',))


def test_trial_authority_events_cannot_be_rewritten(account):
    import psycopg2
    repo, _, _, url = account
    user, _ = newUnifiedTrial(account)
    for statement in ("UPDATE billing_events SET metadata_json='{}' WHERE user_id=%s AND event_type='subscription.trial_started'",
                      "DELETE FROM billing_events WHERE user_id=%s AND event_type='subscription.model_initialized'"):
        with pytest.raises(psycopg2.Error, match='immutable'):
            query(url, statement, (user,))


def test_legacy_trial_without_review_is_unavailable_instead_of_guessed(account):
    from api.services.subscriptions.subscriptionSnapshotService import SubscriptionSnapshotService
    from api.services.subscriptions.subscriptionModel import SubscriptionAuthorityUnavailable
    repo, _, initial, url = account
    query(url, "UPDATE subscriptions SET plan_code=NULL,lifecycle_id=NULL,trial_started_at=NULL,trial_ends_at=NULL WHERE user_id=%s", (initial.userId,))
    with pytest.raises(SubscriptionAuthorityUnavailable):
        SubscriptionSnapshotService(repo.runtimeSnapshotFactsLoader).get(initial.userId)


def test_trial_admin_changes_explicit_deadline_preserve_history_and_allocation_fences(account):
    from uuid import uuid4
    import psycopg2
    from api.services.adminTrialExtensionRepository import AdminTrialExtensionRepository
    from api.services.adminTrialReductionRepository import AdminTrialReductionRepository
    from api.services.subscriptions.subscriptionSnapshotService import SubscriptionSnapshotService
    repo, _, _, url = account
    user, row = newUnifiedTrial(account)
    service = SubscriptionSnapshotService(repo.runtimeSnapshotFactsLoader)
    before = service.get(user)
    repo.activateDueCoverage(user, NOW)
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    ManualCreditRepository(repo).balanceSnapshot(user)
    query(url, 'UPDATE credit_balances SET used_tokens=700,remaining_tokens=monthly_token_quota-700,topup_tokens=500 WHERE user_id=%s', (user,))
    admin = str(uuid4())
    query(url, "INSERT INTO admin_users(id,email,name,password_hash) VALUES(%s,%s,'Synthetic','synthetic')", (admin, admin+'@example.test'))
    extensions = AdminTrialExtensionRepository(lambda:psycopg2.connect(url))
    extension = extensions.createOrGetExtension(str(uuid4()), 'a'*64, user, 3, 'Synthetic extension', admin)
    result = extensions.extendUser(str(extension['id']), user, 3, before.evaluatedAt)
    assert result['outcome'] == 'EXTENDED'
    after = service.get(user)
    assert after.trial.startedAt == before.trial.startedAt and after.trial.endsAt > before.trial.endsAt
    balance = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (user,), one=True)
    assert (balance['used_tokens'], balance['remaining_tokens'], balance['topup_tokens']) == (0, 3_000_000, 500)
    reductions = AdminTrialReductionRepository(lambda:psycopg2.connect(url))
    reduction = reductions.createOrGetReduction(str(uuid4()), 'b'*64, user, 1, 'Synthetic reduction', admin)
    assert reductions.reduceUser(str(reduction['id']), user, 1)['outcome'] == 'REDUCED'
    reduced = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (user,), one=True)
    assert reduced['credit_period_id'] == balance['credit_period_id'] and reduced['remaining_tokens'] == balance['remaining_tokens']
    assert service.get(user).trial.endsAt < after.trial.endsAt


def test_trial_credit_reset_uses_explicit_trial_despite_poisoned_legacy_mode(account):
    import psycopg2
    from datetime import datetime, timezone
    from psycopg2.extras import RealDictCursor
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repo, _, _, url = account
    user, row = newUnifiedTrial(account)
    query(url, "UPDATE subscriptions SET billing_mode='annual_prepaid',status='active',plan_type='annual' WHERE user_id=%s", (user,))
    credits = ManualCreditRepository(repo)
    assert credits.admit(user, 'reporting_query', 'trial-current').billingMode == 'none'
    with psycopg2.connect(url) as db, db.cursor(cursor_factory=RealDictCursor) as cursor:
        repo._lockUser(cursor, user)
        reset = credits.resetQuotaLocked(cursor, repo._canonical(cursor, user), datetime.now(timezone.utc))
    assert reset['after']['monthlyTokenQuota'] == 3_000_000 and reset['after']['planTier'] == 'free'


def test_delayed_trial_credit_sync_cannot_overwrite_a_later_paid_allocation(account, monkeypatch):
    import psycopg2
    from uuid import uuid4
    from datetime import datetime, timezone
    from api.services.adminTrialExtensionRepository import AdminTrialExtensionRepository
    repo, _, _, url = account
    monkeypatch.setattr('api.services.billing.manualBillingRepository._now', lambda:datetime.now(timezone.utc))
    user, row = newUnifiedTrial(account)
    admin = str(uuid4())
    query(url, "INSERT INTO admin_users(id,email,name,password_hash) VALUES(%s,%s,'Synthetic','synthetic')", (admin, admin+'@example.test'))
    extensions = AdminTrialExtensionRepository(lambda:psycopg2.connect(url))
    operation = extensions.createOrGetExtension(str(uuid4()), 'c'*64, user, 2, 'Synthetic sync', admin)
    result = extensions.extendUser(str(operation['id']), user, 2, datetime.now(timezone.utc))
    intent = repo.reserveCheckout(CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid', {'domains':['banking']}, expectedCatalogRevision=1))
    capture(repo, intent, now=datetime.now(timezone.utc))
    before = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (user,), one=True)
    query(url, "UPDATE subscriptions SET billing_mode='none',plan_type='free',status='trial' WHERE user_id=%s", (user,))
    def forbidden(_):
        pytest.fail('Delayed trial cache reset cannot publish over purchased credits')
    synced = extensions.synchronizeCreditExtension(str(operation['id']), forbidden)
    assert synced['credit_sync_status'] == 'CANCELLED'
    assert query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (user,), one=True) == before


def test_trial_settlement_charges_the_current_trial_bucket(account):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repo, _, _, url = account
    user, _ = newUnifiedTrial(account)
    credits = ManualCreditRepository(repo)
    context = credits.admit(user, 'reporting_query', 'trial-spend')
    result = credits.settle(context, 700, 'synthetic-run')
    assert not result['historicalPeriod']
    row = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (user,), one=True)
    assert row['remaining_tokens'] == 3_000_000 - 700 and row['used_tokens'] == 700


@pytest.mark.parametrize('account', ['monthly_prepaid', 'annual_prepaid'], indirect=True)
def test_future_purchase_preserves_current_allocation_until_exact_boundary(account):
    repo, _, initial, url = account
    # Adopt a reviewed existing allocation without resetting already spent tokens.
    repo.activateDueCoverage(initial.userId, NOW)
    query(url, 'UPDATE credit_balances SET used_tokens=700,remaining_tokens=monthly_token_quota-700,topup_tokens=500 WHERE user_id=%s', (initial.userId,))
    before = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True)
    mode = repo.getCoverageSnapshot(initial.userId, NOW).billingMode
    invoice = repo.createAnnualRenewalInvoice(initial.userId) if mode == 'annual_prepaid' else renewal(account)
    attempt = repo.reserveCheckout(CheckoutRequest(initial.userId, 'renewal', mode,
        {'invoiceId': str(invoice['id'])}, expectedCatalogRevision=1))
    result, evidence = capture(repo, attempt)
    assert result.state == 'paid_scheduled' and not result.creditsRefilled
    assert query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True) == before
    boundary = repo.getCoverageSnapshot(initial.userId, NOW).nextPeriod.start
    activated = repo.activateDueCoverage(initial.userId, boundary)
    after = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True)
    assert activated.creditState == 'ready'
    assert after['credit_period_id'] != before['credit_period_id']
    assert (after['used_tokens'], after['remaining_tokens'], after['topup_tokens'], after['plan_tier']) == (0, 20_000_000, 500, 'pro')
    repo.activateDueCoverage(initial.userId, boundary)
    repo.finalizeCapturedPayment(evidence)
    assert query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True) == after


@pytest.mark.parametrize('account', ['annual_prepaid'], indirect=True)
def test_annual_runtime_uses_original_anchor_not_chained_february_end(account):
    from datetime import datetime, timezone
    repo, _, initial, url = account
    start = datetime(2027, 1, 31, 10, tzinfo=timezone.utc)
    query(url, 'UPDATE "Invoices" SET period_start=%s,period_end=%s WHERE id=%s',
          (start, start.replace(year=2028), initial.invoiceId))
    for day, expectedEnd in [(datetime(2027, 2, 28, 10, tzinfo=timezone.utc), datetime(2027, 3, 31, 10, tzinfo=timezone.utc)),
                             (datetime(2027, 3, 31, 10, tzinfo=timezone.utc), datetime(2027, 4, 30, 10, tzinfo=timezone.utc))]:
        repo.activateDueCoverage(initial.userId, day)
        balance = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True)
        assert (balance['period_start'], balance['period_end']) == (day, expectedEnd)
    before = balance
    assert repo.activateDueCoverage(initial.userId, start.replace(year=2028)).creditState != 'ready'
    assert query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True) == before


@pytest.mark.parametrize('account', ['monthly_prepaid', 'annual_prepaid'], indirect=True)
def test_credit_admission_uses_owned_interval_and_exact_expiry(account):
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repo, _, initial, url = account
    credits = ManualCreditRepository(repo)
    mode = repo.getCoverageSnapshot(initial.userId, NOW).billingMode
    query(url, "UPDATE subscriptions SET billing_mode='none',domain_count=0,plan_type='none',status='expired' WHERE user_id=%s", (initial.userId,))
    admitted = credits.admit(initial.userId, 'reporting_query', 'owned-mode')
    assert admitted.billingMode == mode
    assert credits.balanceSnapshot(initial.userId)['plan_tier'] == 'pro'
    expirePurchasedCoverage(account)
    before = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True)
    with pytest.raises(ValueError, match='CREDIT_ADMISSION_REQUIRES_PAID_COVERAGE'):
        credits.admit(initial.userId, 'reporting_query', 'expired')
    assert credits.balanceSnapshot(initial.userId)['remaining_tokens'] == 0
    assert query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True) == before


@pytest.mark.parametrize('account', ['annual_prepaid'], indirect=True)
def test_annual_expert_delta_and_next_credit_month_preserve_frozen_future(account):
    from dateutil.relativedelta import relativedelta
    repo, _, initial, url = account
    repo.activateDueCoverage(initial.userId, NOW)
    query(url, 'UPDATE credit_balances SET used_tokens=700,remaining_tokens=monthly_token_quota-700,topup_tokens=500 WHERE user_id=%s', (initial.userId,))
    nextInvoice = repo.createAnnualRenewalInvoice(initial.userId)
    nextAttempt = repo.reserveCheckout(CheckoutRequest(initial.userId, 'renewal', 'annual_prepaid', {'invoiceId': str(nextInvoice['id'])}, expectedCatalogRevision=1))
    capture(repo, nextAttempt)
    addition = repo.reserveCheckout(CheckoutRequest(initial.userId, 'expert_addition', 'annual_prepaid', {'domains': ['telecom']}, expectedCatalogRevision=1))
    _, evidence = capture(repo, addition)
    balance = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True)
    assert (balance['used_tokens'], balance['remaining_tokens'], balance['topup_tokens']) == (700, 30_000_000 - 700, 500)
    repo.finalizeCapturedPayment(evidence)
    assert query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True) == balance
    coverage = repo.getCoverageSnapshot(initial.userId, NOW)
    repo.activateDueCoverage(initial.userId, coverage.currentPeriod.start + relativedelta(months=1))
    balance = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True)
    assert (balance['domain_count'], balance['remaining_tokens'], balance['topup_tokens']) == (3, 30_000_000, 500)
    repo.activateDueCoverage(initial.userId, coverage.nextPeriod.start)
    balance = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True)
    assert (balance['domain_count'], balance['remaining_tokens'], balance['topup_tokens']) == (2, 20_000_000, 500)


@pytest.mark.parametrize('account', ['monthly_prepaid', 'annual_prepaid'], indirect=True)
def test_admin_generation_survives_late_settlement_and_worker_replay(account):
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repo, _, initial, url = account
    credits = ManualCreditRepository(repo)
    context = credits.admit(initial.userId, 'reporting_query', 'before-admin')
    with psycopg2.connect(url) as db, db.cursor(cursor_factory=RealDictCursor) as cursor:
        repo._lockUser(cursor, initial.userId)
        result = credits.resetQuotaLocked(cursor, repo._canonical(cursor, initial.userId), NOW)
    assert result['after']['creditPeriodId'] != context.creditPeriodId
    assert credits.settle(context, 700, 'late')['historicalPeriod']
    after = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True)
    repo.activateDueCoverage(initial.userId, NOW)
    credits.balanceSnapshot(initial.userId)
    assert query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True) == after
    assert (after['used_tokens'], after['remaining_tokens']) == (0, 20_000_000)


@pytest.mark.parametrize('account', ['annual_prepaid'], indirect=True)
def test_misaligned_existing_annual_window_requires_review_instead_of_new_grant(account):
    from datetime import timedelta
    repo, _, initial, url = account
    # A legacy chained end may straddle the proper anchored window. Treat it
    # as unresolved rather than secretly refill during an authority cutover.
    query(url, 'UPDATE credit_balances SET period_end=period_end-interval \'1 day\',used_tokens=700,remaining_tokens=monthly_token_quota-700 WHERE user_id=%s', (initial.userId,))
    before = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True)
    with pytest.raises(ValueError, match='CREDIT_WINDOW_REQUIRES_RECONCILIATION'):
        repo.activateDueCoverage(initial.userId, NOW + timedelta(seconds=1))
    assert query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (initial.userId,), one=True) == before


@pytest.mark.parametrize('field,value', [('monthly_token_quota', 0), ('domain_count', 1)])
def test_finalization_cannot_report_corrupt_allocation_as_ready(account, field, value):
    repo, _, initial, url = account
    repo.activateDueCoverage(initial.userId, NOW)
    # Pick only the two literal test columns; no user input enters this SQL.
    query(url, f'UPDATE credit_balances SET {field}=%s WHERE user_id=%s', (value, initial.userId))
    def transaction(connection):
        from psycopg2.extras import RealDictCursor
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            repo._lockUser(cursor, initial.userId)
            return repo._result(cursor, {}, repo._canonical(cursor, initial.userId), 'unchanged', None, now=NOW)
    assert repo._run(transaction).creditState == 'pending_materialization'


def test_captured_addition_uses_owned_attempt_and_original_period_preserving_usage(account):
    from api.services.credits.creditConfig import getTokenQuotaForPlan
    repo, model, intent, url = account
    query(url, 'UPDATE credit_balances SET used_tokens=700,remaining_tokens=%s,topup_tokens=500 WHERE user_id=%s',
        (getTokenQuotaForPlan('pro', 2) - 700, intent.userId))
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'monthly_prepaid',
        {'domains': ['telecom', 'supplychain']}, expectedCatalogRevision=1))
    # The pending array is a compatibility projection, never the grant authority.
    query(url, "UPDATE subscriptions SET subscribed_experts='[\"telecom\"]',pending_additions='[]' WHERE user_id=%s", (intent.userId,))
    result, evidence = capture(repo, addition)
    assert result.state == 'expert_activated'
    assert set(repo.getCoverageSnapshot(intent.userId, NOW).currentPeriod.domains) == {'banking', 'manufacturing', 'telecom', 'supplychain'}
    credit = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (intent.userId,), one=True)
    assert (credit['used_tokens'], credit['remaining_tokens'], credit['topup_tokens']) == (700, getTokenQuotaForPlan('pro', 4) - 700, 500)
    repo.finalizeCapturedPayment(evidence)
    assert query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (intent.userId,), one=True) == credit
    grant = query(url, "SELECT metadata_json FROM billing_events WHERE user_id=%s AND event_type='subscription.expert_grant'", (intent.userId,), one=True)
    assert grant['metadata_json']['expertGrant']['periodId'] == intent.invoiceId


def test_closing_one_addition_closes_owned_unpaid_bundle_without_pending_array(account):
    repo, model, intent, url = account
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'monthly_prepaid',
        {'domains': ['telecom', 'supplychain']}, expectedCatalogRevision=1))
    repo.bindProviderOrder(addition.attemptId, {'id': 'order-close-' + addition.attemptId})
    query(url, "UPDATE subscriptions SET pending_additions='[]' WHERE user_id=%s", (intent.userId,))
    result = repo.cancelExpertAddition(intent.userId, 'telecom')
    assert set(result['closedAttemptDomains']) == {'telecom', 'supplychain'}
    assert query(url, 'SELECT status FROM "Invoices" WHERE id=%s', (addition.invoiceId,), one=True)['status'] == 'VOID'
    assert repo.getCoverageSnapshot(intent.userId, NOW).currentPeriod.domains == ('banking', 'manufacturing')


def test_paid_future_experts_do_not_inherit_current_addition(account):
    repo, model, intent, url = account
    invoice = renewal(account)
    nextAttempt = repo.reserveCheckout(CheckoutRequest(intent.userId, 'renewal', 'monthly_prepaid',
        {'invoiceId': str(invoice['id'])}, expectedCatalogRevision=1))
    capture(repo, nextAttempt)
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'monthly_prepaid',
        {'domains': ['telecom']}, expectedCatalogRevision=1))
    capture(repo, addition)
    snapshot = repo.getCoverageSnapshot(intent.userId, NOW)
    assert 'telecom' in snapshot.currentPeriod.domains
    assert snapshot.nextPeriod.domains == ('banking', 'manufacturing')


def test_addition_cannot_move_to_a_later_service_period(account):
    repo, model, intent, url = account
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'monthly_prepaid',
        {'domains': ['telecom']}, expectedCatalogRevision=1))
    end = query(url, 'SELECT current_period_end FROM subscriptions WHERE user_id=%s', (intent.userId,), one=True)['current_period_end']
    result, evidence = capture(repo, addition, now=end)
    assert result.state == 'requires_reconciliation'
    assert not query(url, "SELECT id FROM billing_events WHERE user_id=%s AND event_type='subscription.expert_grant'", (intent.userId,))


def test_accepted_addition_preserves_scheduled_removal_and_updates_only_unpaid_selection(account):
    repo, model, intent, url = account
    selection(account, ['banking'])
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'monthly_prepaid',
        {'domains': ['telecom']}, expectedCatalogRevision=1))
    capture(repo, addition)
    assert repo.getRenewalContext(intent.userId, NOW)['_verifiedCoverage']['nextExperts'] == ['banking', 'telecom']


@pytest.mark.parametrize('field,value', [('servicePeriodId', '10000000-0000-4000-8000-000000000009'),
    ('lifecycleId', '10000000-0000-4000-8000-000000000009'), ('revision', 2)])
def test_addition_capture_rejects_changed_owned_quote_identity(account, field, value):
    import json
    repo, model, intent, url = account
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'monthly_prepaid',
        {'domains': ['telecom']}, expectedCatalogRevision=1))
    query(url, 'UPDATE "Invoices" SET metadata_json=jsonb_set(metadata_json,%s,%s::jsonb) WHERE id=%s',
        (['manualBilling', field], json.dumps(value), addition.invoiceId))
    result, evidence = capture(repo, addition)
    assert result.state == 'requires_reconciliation'
    assert not query(url, "SELECT id FROM billing_events WHERE user_id=%s AND event_type='subscription.expert_grant'", (intent.userId,))


def test_capture_and_selection_race_cannot_edit_purchased_future_experts(account):
    from concurrent.futures import ThreadPoolExecutor
    repo, model, intent, url = account
    invoice = renewal(account)
    attempt = repo.reserveCheckout(CheckoutRequest(intent.userId, 'renewal', 'monthly_prepaid',
        {'invoiceId': str(invoice['id'])}, expectedCatalogRevision=1))
    # The capture callback already owns an Order; race finalization against the
    # preference write, independently of provider Order creation/binding.
    order = 'order-race-' + attempt.attemptId
    repo.bindProviderOrder(attempt.attemptId, {'id': order})
    evidence = VerifiedPaymentEvidence(attempt.attemptId, attempt.invoiceId, attempt.userId, order,
        'pay-race-' + attempt.attemptId, attempt.purpose, attempt.currency, 'captured',
        'server_observation', attempt.amount, NOW, None, None, False)
    def change():
        try:
            return selection(account, ['banking'])
        except ValueError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        captured, selected = pool.submit(repo.finalizeCapturedPayment, evidence), pool.submit(change)
        result = captured.result()
        selected.result()
    snapshot = repo.getCoverageSnapshot(intent.userId, NOW)
    if result.state == 'paid_scheduled':
        assert snapshot.nextPeriod.domains == ('banking', 'manufacturing')
    else:
        assert result.state == 'requires_reconciliation' and snapshot.nextPeriod is None


@pytest.mark.parametrize('account', ['annual_prepaid'], indirect=True)
def test_annual_addition_does_not_rewrite_original_service_quote(account):
    repo, model, intent, url = account
    before = query(url, 'SELECT metadata_json,total_amount,razorpay_order_id,"razorpayPaymentId" FROM "Invoices" WHERE id=%s', (intent.invoiceId,), one=True)
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'annual_prepaid',
        {'domains': ['telecom']}, expectedCatalogRevision=1))
    assert capture(repo, addition)[0].state == 'expert_activated'
    assert query(url, 'SELECT metadata_json,total_amount,razorpay_order_id,"razorpayPaymentId" FROM "Invoices" WHERE id=%s', (intent.invoiceId,), one=True) == before
    assert repo.getCoverageSnapshot(intent.userId, NOW).currentPeriod.domains == ('banking', 'manufacturing', 'telecom')


def test_explicit_addition_revocation_is_respected_without_touching_frozen_quote(account):
    repo, model, intent, url = account
    addition = repo.reserveCheckout(CheckoutRequest(intent.userId, 'expert_addition', 'monthly_prepaid',
        {'domains': ['telecom']}, expectedCatalogRevision=1))
    capture(repo, addition)
    import json
    query(url, 'UPDATE "Invoices" SET metadata_json=jsonb_set(metadata_json,\'{manualBilling,revokedAt}\',%s::jsonb) WHERE id=%s',
        (json.dumps(NOW.isoformat()), addition.invoiceId))
    assert repo.getCoverageSnapshot(intent.userId, NOW).currentPeriod.domains == ('banking', 'manufacturing')


@pytest.mark.parametrize('newMode', ['monthly_prepaid', 'annual_prepaid'])
def test_expired_annual_history_can_buy_either_interval_under_same_canonical_account(account, newMode):
    repo, model, original, url = account
    before = expirePurchasedCoverage(account)
    assert not repo.getCoverageSnapshot(original.userId, NOW).accessAllowed
    intent = repo.reserveCheckout(CheckoutRequest(original.userId, 'initial_purchase', newMode,
        {'domains': ['telecom']}, 'fresh-purchase', expectedCatalogRevision=1))
    pending = query(url, 'SELECT * FROM subscriptions WHERE user_id=%s', (original.userId,), one=True)
    assert pending['renewal_opt_out'] and str(pending['lifecycle_id']) == original.lifecycleId
    assert not repo.getCoverageSnapshot(original.userId, NOW).accessAllowed
    result, evidence = capture(repo, intent)
    assert result.state == 'activated'
    after = query(url, 'SELECT * FROM subscriptions WHERE user_id=%s', (original.userId,), one=True)
    assert after['id'] == before['id'] and after['is_canonical']
    assert str(after['lifecycle_id']) == intent.lifecycleId != original.lifecycleId
    assert after['plan_code'] == 'pro' and not after['renewal_opt_out']
    assert after['cancellation_requested_at'] is None and after['cancellation_reason'] is None
    history = query(url, "SELECT metadata_json FROM billing_events WHERE user_id=%s AND event_type='subscription.lifecycle.started' AND invoice_id=%s",
        (original.userId, intent.invoiceId), one=True)['metadata_json']
    assert history['previousCancellationRequestedAt'] == before['cancellation_requested_at'].isoformat()
    assert repo.getCoverageSnapshot(original.userId, NOW).currentPeriod.start == evidence.observedAt
    assert len(query(url, 'SELECT id FROM subscriptions WHERE user_id=%s', (original.userId,))) == 1


@pytest.mark.parametrize('account', ['monthly_prepaid', 'annual_prepaid'], indirect=True)
def test_opt_out_and_resume_preserve_paid_future_time_and_credits_idempotently(account):
    repo, model, original, url = account
    invoice = renewal(account) if original.billingMode == 'monthly_prepaid' else repo.createAnnualRenewalInvoice(original.userId)
    attempt = repo.reserveCheckout(CheckoutRequest(original.userId, 'renewal', original.billingMode,
        {'invoiceId': str(invoice['id'])}, expectedCatalogRevision=1))
    capture(repo, attempt)
    snapshot = repo.getCoverageSnapshot(original.userId, NOW)
    credit = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (original.userId,), one=True)
    first = repo.setRenewalOptOut(original.userId, True, None, 'cancel')
    again = repo.setRenewalOptOut(original.userId, True, None, 'cancel')
    assert first['version'] == again['version']
    assert first['cancellation_requested_at'] == NOW
    assert first['preferenceResult']['effectiveAt'] == snapshot.finalPaidEnd.isoformat()
    resumed = repo.setRenewalOptOut(original.userId, False, None, 'resume')
    repeated = repo.setRenewalOptOut(original.userId, False, None, 'resume')
    assert resumed['version'] == repeated['version'] and resumed['cancellation_requested_at'] is None
    assert query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (original.userId,), one=True) == credit
    assert repo.getCoverageSnapshot(original.userId, NOW).nextPeriod == snapshot.nextPeriod


def test_expired_opt_out_cannot_resume_using_an_old_cached_future_end(account):
    repo, model, original, url = account
    expirePurchasedCoverage(account)
    with pytest.raises(ValueError, match='NO_PAID_ACCESS'):
        repo.setRenewalOptOut(original.userId, False, None, 'resume-expired')


def test_failed_new_payment_keeps_expired_access_and_previous_lifecycle(account):
    repo, model, original, url = account
    before = expirePurchasedCoverage(account)
    intent = repo.reserveCheckout(CheckoutRequest(original.userId, 'initial_purchase', 'monthly_prepaid',
        {'domains': ['telecom']}, expectedCatalogRevision=1))
    order = 'order-failed-repurchase-' + intent.attemptId
    repo.bindProviderOrder(intent.attemptId, {'id': order})
    evidence = VerifiedPaymentEvidence(intent.attemptId, intent.invoiceId, original.userId, order,
        'pay-failed-repurchase-' + intent.attemptId, 'initial_purchase', intent.currency, 'failed',
        'server_observation', intent.amount, NOW, None, None, False)
    assert not repo.finalizeCapturedPayment(evidence).finalized
    after = query(url, 'SELECT * FROM subscriptions WHERE id=%s', (before['id'],), one=True)
    assert after['lifecycle_id'] == before['lifecycle_id'] and after['renewal_opt_out']
    assert not repo.getCoverageSnapshot(original.userId, NOW).accessAllowed


def test_delayed_legacy_boundary_worker_cannot_clear_credits_from_stale_expiry_cache(account):
    from datetime import timedelta
    repo, model, original, url = account
    before = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (original.userId,), one=True)
    query(url, 'UPDATE subscriptions SET current_period_end=%s WHERE user_id=%s', (NOW - timedelta(seconds=1), original.userId))
    repo.activateDueCoverage(original.userId, NOW)
    assert repo.getCoverageSnapshot(original.userId, NOW).accessAllowed
    assert query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (original.userId,), one=True) == before


def test_failed_old_renewal_without_reconciled_closure_blocks_another_provider_order(account):
    from api.services.billing.manualPaymentService import ManualPaymentService
    from test.test_customer_free_payments import StrictFakeRazorpayClient
    repo, model, original, url = account
    invoice = renewal(account)
    provider = StrictFakeRazorpayClient()
    service = ManualPaymentService.forProduction(provider, repo)
    old = service.createCheckout(CheckoutRequest(original.userId, 'renewal', 'monthly_prepaid',
        {'invoiceId': str(invoice['id'])}, expectedCatalogRevision=1))
    query(url, "UPDATE billing_events SET payment_status='failed',event_status='failed' WHERE id=%s", (old.attemptId,))
    expirePurchasedCoverage(account, staleCache=False)
    before = len(provider.order.createdPayloads)
    with pytest.raises(ValueError, match='RECOVERY_REQUIRED'):
        service.createCheckout(CheckoutRequest(original.userId, 'initial_purchase', 'annual_prepaid',
            {'domains': ['telecom']}, expectedCatalogRevision=1))
    assert len(provider.order.createdPayloads) == before


def test_accepted_first_purchase_closes_explicit_trial_at_capture(account):
    from datetime import timedelta
    from uuid import uuid4
    repo, model, original, url = account
    user = 'trial-purchase-' + str(uuid4())
    query(url, 'INSERT INTO "Users"("userId") VALUES(%s)', (user,))
    row = repo.ensureCanonicalSubscription(user)
    query(url, "UPDATE subscriptions SET plan_code='pro',trial_started_at=%s,trial_ends_at=%s WHERE id=%s",
        (NOW - timedelta(days=1), NOW + timedelta(days=11), row['id']))
    intent = repo.reserveCheckout(CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid',
        {'domains': ['banking']}, expectedCatalogRevision=1))
    assert query(url, 'SELECT trial_closed_at FROM subscriptions WHERE id=%s', (row['id'],), one=True)['trial_closed_at'] is None
    result, evidence = capture(repo, intent)
    assert result.finalized
    assert query(url, 'SELECT trial_closed_at FROM subscriptions WHERE id=%s', (row['id'],), one=True)['trial_closed_at'] == evidence.observedAt


@pytest.mark.parametrize('account', ['monthly_prepaid', 'annual_prepaid'], indirect=True)
def test_public_cancel_and_resume_use_one_owned_preference_operation(account, monkeypatch):
    import os
    from jose import jwt
    from api.services.subscriptions.subscriptionService import SubscriptionService
    repo, model, original, url = account
    service = SubscriptionService.__new__(SubscriptionService)
    service._getCanonicalSubscription = lambda userId, required=False: query(url, 'SELECT * FROM subscriptions WHERE user_id=%s', (userId,), one=True)
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository', lambda: repo)
    token = jwt.encode({'userId': original.userId, 'email': 'synthetic@example.test'}, os.environ['SECRET_KEY'], algorithm='HS256')
    credit = query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (original.userId,), one=True)
    cancelled = service.cancelSubscription(None, token)
    assert cancelled['renewalOptOut'] and cancelled['effectiveAt'] != 'cycle_end'
    assert service.cancelSubscription(None, token)['effectiveAt'] == cancelled['effectiveAt']
    resumed = service.resumeRenewal(token)
    assert not resumed['renewalOptOut'] and not resumed['creditsRefilled']
    assert service.resumeRenewal(token)['repeated']
    assert query(url, 'SELECT * FROM credit_balances WHERE user_id=%s', (original.userId,), one=True) == credit


@pytest.mark.parametrize('account',['monthly_prepaid','annual_prepaid'],indirect=True)
def test_preference_http_results_include_current_v2_snapshot_on_repeated_actions(account,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from jose import jwt
    from api.commons import verifyToken
    from api.routers import subscriptions as routes
    from api.services.subscriptions.subscriptionService import SubscriptionService
    repo,_,initial,url = account
    service = SubscriptionService.__new__(SubscriptionService)
    service._getCanonicalSubscription = lambda userId,required=False: query(url,
        'SELECT * FROM subscriptions WHERE user_id=%s',(userId,),one=True)
    monkeypatch.setattr(routes,'subscriptionService',service)
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repo)
    token = jwt.encode({'userId':initial.userId,'email':'synthetic@example.test'},os.environ['SECRET_KEY'],algorithm='HS256')
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[verifyToken]=lambda:token
    with TestClient(app) as client:
        for endpoint,optOut in [('cancelSubscription',True),('cancelSubscription',True),('resumeRenewal',False),('resumeRenewal',False)]:
            response = client.post('/'+endpoint,json={})
            assert response.status_code==200
            body = response.json()
            assert body['status']=='SUCCESS'
            assert body['data']['subscription']['schemaVersion']==2
            assert body['data']['subscription']['renewalOptOut']==optOut
            assert body['data']['subscription']['accessAllowed'] is True
            assert body['data']['subscription']['billingInterval']==('year' if initial.billingMode=='annual_prepaid' else 'month')


@pytest.mark.parametrize('account', ['monthly_prepaid', 'annual_prepaid'], indirect=True)
def test_opt_out_preserves_a_renewal_captured_before_closure_with_provider_proof(account, monkeypatch):
    from datetime import timedelta
    repo, model, original, url = account
    invoice = renewal(account) if original.billingMode == 'monthly_prepaid' else repo.createAnnualRenewalInvoice(original.userId)
    attempt = repo.reserveCheckout(CheckoutRequest(original.userId, 'renewal', original.billingMode,
        {'invoiceId': str(invoice['id'])}, expectedCatalogRevision=1))
    order = 'order-before-cancel-' + attempt.attemptId
    repo.bindProviderOrder(attempt.attemptId, {'id': order})
    monkeypatch.setattr('api.services.billing.manualBillingRepository._now', lambda: NOW + timedelta(seconds=2))
    repo.setRenewalOptOut(original.userId, True, None, 'cancel-before-observation')
    observed, captured = NOW + timedelta(seconds=5), NOW + timedelta(seconds=1)
    evidence = VerifiedPaymentEvidence(attempt.attemptId, attempt.invoiceId, original.userId, order,
        'pay-before-cancel-' + attempt.attemptId, 'renewal', attempt.currency, 'captured',
        'provider_captured_at', attempt.amount, observed, captured, 'synthetic-attestation', True)
    result = repo.finalizeCapturedPayment(evidence)
    assert result.state == 'paid_scheduled' and result.renewalOptOut and result.nextPeriod is not None
    notice = query(url,"SELECT metadata_json FROM billing_events WHERE user_id=%s AND event_type='email.billing_intent.committed' AND metadata_json->>'notificationType'='monthly_cancellation_confirmation'",(original.userId,),one=True)['metadata_json']
    assert notice['metadata']['finalPaidEnd'] == result.nextPeriod.end.isoformat()


@pytest.mark.parametrize('account', ['monthly_prepaid', 'annual_prepaid'], indirect=True)
def test_one_paid_future_slot_blocks_an_extra_payable_quote_before_order_creation(account):
    from uuid import uuid4
    repo, model, original, url = account
    invoice = renewal(account) if original.billingMode == 'monthly_prepaid' else repo.createAnnualRenewalInvoice(original.userId)
    intent = repo.reserveCheckout(CheckoutRequest(original.userId, 'renewal', original.billingMode,
        {'invoiceId': str(invoice['id'])}, expectedCatalogRevision=1))
    capture(repo, intent)
    extra = str(uuid4())
    from dateutil.relativedelta import relativedelta
    start = repo.getCoverageSnapshot(original.userId, NOW).nextPeriod.end
    end = start + (relativedelta(years=1) if original.billingMode == 'annual_prepaid' else relativedelta(months=1))
    query(url, '''INSERT INTO "Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,currency,
        period_start,period_end,metadata_json) SELECT %s,"userId",subscription_id,'UPCOMING',billing_reason,total_amount,
        currency,%s,%s,metadata_json FROM "Invoices" WHERE id=%s''', (extra, start, end, invoice['id']))
    with pytest.raises(ValueError, match='RENEWAL_ALREADY_PAID'):
        repo.reserveCheckout(CheckoutRequest(original.userId, 'renewal', original.billingMode,
            {'invoiceId': extra}, expectedCatalogRevision=1))


@pytest.mark.parametrize('newMode', ['monthly_prepaid', 'annual_prepaid'])
def test_duplicate_post_lapse_capture_creates_one_new_lifecycle_and_period(account, newMode):
    from concurrent.futures import ThreadPoolExecutor
    repo, model, original, url = account
    before = expirePurchasedCoverage(account, staleCache=False)
    intent = repo.reserveCheckout(CheckoutRequest(original.userId, 'initial_purchase', newMode,
        {'domains': ['telecom']}, expectedCatalogRevision=1))
    order = 'order-repurchase-race-' + intent.attemptId
    repo.bindProviderOrder(intent.attemptId, {'id': order})
    evidence = VerifiedPaymentEvidence(intent.attemptId, intent.invoiceId, original.userId, order,
        'pay-repurchase-race-' + intent.attemptId, 'initial_purchase', intent.currency, 'captured',
        'server_observation', intent.amount, NOW, None, None, False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(repo.finalizeCapturedPayment, [evidence, evidence]))
    assert sorted(r.state for r in results) == ['activated', 'already_finalized']
    assert len(query(url, 'SELECT id FROM subscriptions WHERE user_id=%s', (original.userId,))) == 1
    assert query(url, 'SELECT lifecycle_id FROM subscriptions WHERE id=%s', (before['id'],), one=True)['lifecycle_id'] == intent.lifecycleId


@pytest.mark.parametrize('account', ['monthly_prepaid', 'annual_prepaid'], indirect=True)
def test_capture_and_opt_out_race_retains_paid_evidence_and_current_access(account):
    from concurrent.futures import ThreadPoolExecutor
    repo, model, original, url = account
    invoice = renewal(account) if original.billingMode == 'monthly_prepaid' else repo.createAnnualRenewalInvoice(original.userId)
    intent = repo.reserveCheckout(CheckoutRequest(original.userId, 'renewal', original.billingMode,
        {'invoiceId': str(invoice['id'])}, expectedCatalogRevision=1))
    order = 'order-opt-out-race-' + intent.attemptId
    repo.bindProviderOrder(intent.attemptId, {'id': order})
    evidence = VerifiedPaymentEvidence(intent.attemptId, intent.invoiceId, original.userId, order,
        'pay-opt-out-race-' + intent.attemptId, 'renewal', intent.currency, 'captured',
        'server_observation', intent.amount, NOW, None, None, False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        finalized = pool.submit(repo.finalizeCapturedPayment, evidence)
        cancelled = pool.submit(repo.setRenewalOptOut, original.userId, True, None, 'race-cancel')
        result, preference = finalized.result(), cancelled.result()
    snapshot = repo.getCoverageSnapshot(original.userId, NOW)
    assert snapshot.accessAllowed and snapshot.renewalOptOut
    assert result.state in ('paid_scheduled', 'requires_reconciliation')
    if result.state == 'paid_scheduled':
        assert snapshot.nextPeriod is not None
    assert len(query(url, 'SELECT id FROM billing_events WHERE provider_payment_id=%s', (evidence.providerPaymentId,))) == 1


@pytest.mark.parametrize('interval', ['month', 'year'])
def test_expired_session_can_create_and_verify_fresh_purchase_through_http(account, monkeypatch, interval):
    import hashlib
    import hmac
    import os
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from jose import jwt
    from api.commons import verifyToken
    import api.routers.subscriptions as routes
    from api.services.subscriptions.subscriptionService import SubscriptionService
    from api.services.billing.pricingContext import issuePricingContext
    from test.test_customer_free_payments import StrictFakeRazorpayClient
    repo, model, original, url = account
    expirePurchasedCoverage(account)
    provider = StrictFakeRazorpayClient()
    service = SubscriptionService.__new__(SubscriptionService)
    createOrder = provider.order.create
    def uniqueOrder(payload):
        order = createOrder(payload)
        provider.order.orders.pop(order['id'])
        order['id'] = 'order-http-' + original.userId + '-' + order['id']
        provider.order.orders[order['id']] = order
        return order
    provider.order.create = uniqueOrder
    service.razorpayClient = provider
    service.VALID_DOMAINS = {'banking', 'manufacturing', 'supplychain', 'telecom'}
    service._resolveCheckoutIdentity = lambda *_: {'email': 'synthetic@example.test', 'name': 'Synthetic', 'contact': ''}
    # Profile/session persistence is outside this financial integration fixture.
    # The production create/verify services and their repository stay real.
    service._reissueTokenWithUpdatedClaims = lambda old, status, plan: jwt.encode(
        {'userId': original.userId, 'sub_status': status, 'plan_type': plan}, os.environ['SECRET_KEY'], algorithm='HS256')
    monkeypatch.setattr(routes, 'subscriptionService', service)
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository', lambda: repo)
    monkeypatch.setattr('api.services.subscriptions.subscriptionService.utcNow', lambda: NOW)
    monkeypatch.setenv('PRICING_PROXY_SECRET', 'synthetic-local-context')
    token = jwt.encode({'userId': original.userId, 'email': 'synthetic@example.test',
        'sub_status': 'expired', 'plan_type': 'annual'}, os.environ['SECRET_KEY'], algorithm='HS256')
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[verifyToken] = lambda: token
    client = TestClient(app)
    created = client.post('/createSubscription', json={'domains': ['telecom'], 'contact': '',
        'planCode': 'pro', 'billingInterval': interval, 'expectedCatalogRevision': 1,
        'pricingContext': issuePricingContext(original.userId, 'INR', 'hosting', catalogRevision=1)})
    assert created.status_code == 200, created.text
    checkout = created.json()
    order, payment = checkout['razorpayOrderId'], 'pay-http-' + checkout['attemptId']
    payload = provider.order.createdPayloads[0]
    assert payload['notes']['billingMode'] == ('annual_prepaid' if interval == 'year' else 'monthly_prepaid')
    provider.payments[payment] = {'id': payment, 'order_id': order, 'status': 'captured',
        'amount': payload['amount'], 'currency': payload['currency'], 'notes': payload['notes']}
    signature = hmac.new(os.environ['RAZORPAY_KEY_SECRET'].encode(), (order + '|' + payment).encode(), hashlib.sha256).hexdigest()
    verified = client.post('/verifySubscription', json={'razorpayOrderId': order, 'razorpayPaymentId': payment,
        'razorpaySignature': signature})
    assert verified.status_code == 200, verified.text
    result = verified.json()
    assert result['finalized'] and result['state'] == 'activated'
    assert result['subscription']['schemaVersion'] == 2
    assert result['subscription']['status'] == 'active' and result['subscription']['billingInterval'] == interval
    assert jwt.decode(result['accessToken'], os.environ['SECRET_KEY'], algorithms=['HS256'])['sub_status'] == 'active'
    assert len(provider.order.createdPayloads) == 1 and repo.getCoverageSnapshot(original.userId).accessAllowed
