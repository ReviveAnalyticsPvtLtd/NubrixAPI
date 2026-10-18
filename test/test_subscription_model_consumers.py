"""Consumer contracts: server-loaded facts, exact time, and no read mutations."""
from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import ValidationError

from api.services.subscriptions.subscriptionSnapshotService import SubscriptionSnapshotService
from test.subscriptionModelFixtures import NOW, USER, SUBSCRIPTION, factsFor


class NoDatabase:
    def table(self, name):
        pytest.fail(f'Snapshot consumer attempted legacy database access: {name}')


@pytest.fixture
def unified(monkeypatch):
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE', 'v2')
    def install(facts, now=NOW):
        service = SubscriptionSnapshotService(lambda user, requested: (facts, requested or now))
        monkeypatch.setattr('api.services.subscriptions.subscriptionSnapshotService.getSubscriptionSnapshotService', lambda: service, raising=False)
        return service.get(USER, now)
    return install


@pytest.mark.parametrize('condition', ['none', 'trial', 'monthly', 'annual', 'expired'])
def test_login_profile_and_entitlement_share_snapshot_without_legacy_writes(unified, condition):
    from api.services.authenticationService import AuthenticationService
    from api.services.managementService import ManagementService
    from api.services.subscriptions.entitlementService import SubscriptionEntitlementService
    from api.services.billing.manualBillingPresentation import subscriptionDisplayFacts
    snapshot = unified(factsFor(condition))
    for cls in (AuthenticationService, ManagementService):
        service = cls.__new__(cls)
        service.client = NoDatabase()
        row = {'id': SUBSCRIPTION, 'user_id': USER, 'status': 'active', 'billing_mode': 'annual_prepaid',
            'plan_type': 'annual', 'current_period_end': (NOW + timedelta(days=900)).isoformat()}
        service._refreshLifecycleSnapshot(USER, row)
        assert row['status'] == snapshot.status
        public = subscriptionDisplayFacts(row)
        assert public['subscription']['status'] == snapshot.status
        assert public['subscription']['accessAllowed'] == snapshot.accessAllowed
        assert public['subscription']['domainCount'] == snapshot.domainCount
    entitlement = SubscriptionEntitlementService(NoDatabase()).get(USER)
    assert entitlement.status == snapshot.status
    assert entitlement.trialOrAbove == snapshot.accessAllowed
    assert entitlement.paidPlan == (snapshot.accessAllowed and snapshot.accessSource == 'paid')


@pytest.mark.parametrize('guard', ['accountBanned', 'erasurePending'])
def test_account_guard_stays_independent_of_paid_status(unified, guard):
    from api.services.subscriptions.entitlementService import SubscriptionEntitlementService
    snapshot = unified(replace(factsFor('monthly'), **{guard: True}))
    entitlement = SubscriptionEntitlementService(NoDatabase()).get(USER)
    assert entitlement.status == 'active' and not entitlement.paidPlan and not entitlement.trialOrAbove
    assert not snapshot.purchaseAllowed


def test_exact_paid_end_does_not_accept_cached_plan_or_permission_claims(unified):
    from api.services.subscriptions.entitlementService import SubscriptionEntitlementService
    facts = factsFor('monthly')
    unified(facts, facts.periods[0].end)
    result = SubscriptionEntitlementService(NoDatabase()).get(USER)
    assert result.status == 'expired' and not result.paidPlan and not result.topupEligible


def test_topup_eligibility_uses_paid_snapshot_instead_of_retained_labels(unified):
    from api.services.credits.topupService import TopupService
    unified(factsFor('monthly'))
    assert TopupService._isTopupEligible({'user_id': USER, 'status': 'expired', 'plan_type': 'none', 'billing_mode': 'none'})
    unified(factsFor('trial'))
    assert not TopupService._isTopupEligible({'user_id': USER, 'status': 'active', 'plan_type': 'pro', 'billing_mode': 'monthly_prepaid'})


@pytest.mark.parametrize('field,value', [('status','active'), ('domain_count',4), ('subscribed_experts','["banking"]')])
def test_generic_admin_subscription_patch_rejects_derived_fields(unified, field, value):
    from api.adminModels import AdminSubscriptionPatch
    with pytest.raises(ValidationError, match='DERIVED_SUBSCRIPTION_FIELD'):
        AdminSubscriptionPatch.model_validate({field: value})


def test_direct_admin_service_cannot_bypass_patch_validation(unified):
    from api.adminErrors import AdminApiError
    from api.adminModels import AdminSubscriptionPatch
    from api.services.adminManagementService import AdminManagementService
    patch = AdminSubscriptionPatch.model_construct(status='active', _fields_set={'status'})
    with pytest.raises(AdminApiError, match='DERIVED_SUBSCRIPTION_FIELD') as error:
        AdminManagementService(client=NoDatabase()).updateSubscription(SUBSCRIPTION, patch, None)
    assert error.value.statusCode == 422


@pytest.mark.parametrize('condition', ['trial','monthly','expired'])
def test_actual_password_login_and_profile_keep_success_and_v2_envelope(unified, condition, monkeypatch):
    import os
    import hashlib
    from types import SimpleNamespace
    from api.models import Login
    from api.services.authenticationService import AuthenticationService
    from api.services.managementService import ManagementService
    from test.test_user_access_enforcement import FakeClient, FakeQuery
    originalSelect = FakeQuery.select
    monkeypatch.setattr(FakeQuery, 'select', lambda self, *fields: originalSelect(self, ','.join(fields)))
    snapshot = unified(factsFor(condition))
    email, password = 'consumer@example.test', 'synthetic-password'
    user = {'userId':USER,'email':email,'fullName':'Synthetic', 'currentWorkspaceId':'workspace',
        'isBanned':False,'password':hashlib.md5((password+os.environ['SECRET_KEY']).encode()).hexdigest()}
    auth = AuthenticationService.__new__(AuthenticationService)
    auth.client = FakeClient([user], SimpleNamespace(email=email,id=USER,email_confirmed_at='synthetic'))
    auth._getCreditSnapshot = lambda _: {}
    # This fixture tests real login/profile response construction and session
    # insert. Default-account creation and financial evidence have PG tests.
    auth._ensureSubscriptionSnapshot = auth._getSubscriptionSnapshot
    result = auth.login(Login(email=email, password=password))
    assert result['status'] == 'SUCCESS'
    assert result['subscription']['status'] == snapshot.status
    assert result['subscriptionStatus'] == snapshot.status.upper()
    from api.services.credits.creditService import creditService
    monkeypatch.setattr(creditService, 'getBalanceSnapshot', lambda _: {})
    profile = ManagementService.__new__(ManagementService)
    profile.client = auth.client
    response = profile.getUserProfile(result['accessToken'])
    assert response['subscription'] == result['subscription']
    assert response['plan']['status'] == result['subscriptionStatus']


def test_new_create_contract_rejects_client_permission_claims(unified):
    from api.models import CreateSubscriptionRequest
    with pytest.raises(ValidationError):
        CreateSubscriptionRequest.model_validate({'domains':['banking'],'contact':'', 'accessAllowed':True})


def test_profile_keeps_owned_next_selection_instead_of_dropping_scheduled_removals(unified):
    from api.services.authenticationService import AuthenticationService
    from api.services.subscriptions.subscriptionFieldUtils import toApiPlanFields
    snapshot = unified(replace(factsFor('monthly'), nextSelectedExperts=('banking',)))
    service = AuthenticationService.__new__(AuthenticationService)
    service.client = NoDatabase()
    row = service._getSubscriptionSnapshot(USER)
    assert toApiPlanFields(row)['pendingRemovals'] == ['telecom']


@pytest.mark.parametrize('condition', ['trial','monthly','expired'])
def test_admin_view_exposes_verified_snapshot_and_operation_eligibility(unified, condition):
    from api.services.adminManagementService import AdminManagementService
    from api.adminModels import AdminSubscriptionView
    from test.test_admin_management_service import subscriptionRow
    snapshot = unified(factsFor(condition))
    service = AdminManagementService(client=NoDatabase())
    service._fetchAll = lambda *args: [subscriptionRow(subscriptionId=SUBSCRIPTION, user_id=USER, is_canonical=True)]
    view = service.listSubscriptions()[0]
    assert view['subscription']['status'] == snapshot.status
    assert not view['operationEligibility']['editSubscription']
    assert view['operationEligibility']['extendTrial'] == (condition == 'trial')
    assert view['operationEligibility']['resetCredits'] == snapshot.accessAllowed
    AdminSubscriptionView.model_validate(view)


def test_topup_authority_failure_is_retryable_503(unified, monkeypatch):
    from api.services.credits.topupService import TopupService
    from api.services.subscriptions.subscriptionModel import SubscriptionAuthorityUnavailable
    from utils.exceptionHandler import CustomException
    class Unavailable:
        def get(self, _):
            raise SubscriptionAuthorityUnavailable('Synthetic unavailable evidence')
    monkeypatch.setattr('api.services.subscriptions.subscriptionSnapshotService.getSubscriptionSnapshotService', lambda:Unavailable())
    with pytest.raises(CustomException) as error:
        TopupService._isTopupEligible({'user_id':USER})
    assert error.value.statusCode == 503


def test_unified_annual_overdue_workers_never_read_or_mutate_legacy_states(unified):
    from nubrix.triggers.tasks.pastDueSuspensionTask import PastDueSuspensionTask
    from nubrix.triggers.tasks.renewalLifecycleTask import RenewalLifecycleTask
    unified(factsFor('expired'))
    overdue = PastDueSuspensionTask.__new__(PastDueSuspensionTask)
    overdue.client = NoDatabase()
    assert overdue.execute()['past_due']['transitioned'] == 0
    reminders = RenewalLifecycleTask.__new__(RenewalLifecycleTask)
    reminders.client = NoDatabase()
    assert reminders.execute()['reminders']['sent'] == 0


def test_unified_solicitations_ignore_poisoned_cached_deadline(unified):
    from api.services.notifications.billingNotificationService import buildBillingNotificationIntent
    unified(factsFor('monthly'))
    row = {'id':SUBSCRIPTION,'user_id':USER,'status':'active','billing_mode':'monthly_prepaid',
        'current_period_end':(NOW-timedelta(seconds=1)).isoformat(),
        'billing_state':{'manualBilling':{'lifecycleId':'wrong'}}}
    # Actual coverage ends next month: a stale cache must not create an expiry.
    assert buildBillingNotificationIntent({'type':'monthly_subscription_expired'},
        {'subscription':row,'cycleId':row['current_period_end']},NOW) is None


def test_trial_warning_uses_explicit_trial_instead_of_annual_cache(unified):
    from api.services.notifications.trialExpiryNotificationService import buildTrialExpiryIntent
    facts = factsFor('trial')
    now = facts.trial.endsAt-timedelta(days=2)
    unified(facts, now)
    intent = buildTrialExpiryIntent({'id':SUBSCRIPTION,'user_id':USER,'status':'active',
        'billing_mode':'annual_prepaid','current_period_end':'2099-01-01T00:00:00Z'},now)
    assert intent is not None
    assert intent['periodEnd'] == '2026-10-22T10:00:00+00:00'


def test_optout_suppresses_solicitation_without_losing_paid_future(unified):
    from api.services.notifications.billingNotificationService import buildBillingNotificationIntent
    snapshot = unified(replace(factsFor('paid_future'), renewalOptOut=True))
    row = {'id':SUBSCRIPTION,'user_id':USER,'billing_mode':'monthly_prepaid',
        'renewal_opt_out':False,'current_period_end':NOW.isoformat()}
    assert snapshot.paidUntil.isoformat() == '2026-12-17T10:00:00+00:00'
    assert buildBillingNotificationIntent({'type':'monthly_subscription_expired'},
        {'subscription':row,'cycleId':NOW.isoformat()},NOW) is None


def test_unified_erasure_freeze_does_not_reference_removed_fields(unified):
    from api.services.userErasureRepository import UserErasureRepository
    from test.test_user_erasure_repository import FakeConnection
    connection = FakeConnection()
    UserErasureRepository(lambda:connection).freezeBilling(USER)
    sql = ' '.join(q for q,_ in connection.state['executed'])
    assert 'auto_renew_enabled' not in sql and 'renewal_due_at' not in sql


@pytest.mark.parametrize('taskName', ['MonthlyRenewalTask','AnnualRenewalTask','EntitlementBoundaryTask'])
def test_unified_workers_scan_canonical_owners_without_legacy_filters(unified, taskName):
    from importlib import import_module
    module = import_module('nubrix.triggers.tasks.'+taskName[0].lower()+taskName[1:])
    cls = getattr(module,taskName)
    class EmptyOwners:
        data = []
        def table(self,name):
            assert name == 'subscriptions'
            return self
        def select(self,fields):
            assert set(fields.replace(' ','').split(',')) <= {'id','user_id','erasure_pending'}
            return self
        def eq(self,field,value):
            assert field == 'is_canonical' and value is True
            return self
        def execute(self):
            return self
    worker = cls.__new__(cls)
    worker.client = EmptyOwners()
    worker.now = worker._now = lambda:NOW
    worker.execute()


def test_stale_cancellation_cannot_describe_a_new_lifecycle(unified):
    from api.services.notifications.billingNotificationService import isBillingNotificationEligible
    unified(replace(factsFor('monthly'),renewalOptOut=True))
    row = {'id':SUBSCRIPTION,'user_id':USER,'cancellation_requested_at':NOW.isoformat()}
    delivery = {'notification_type':'monthly_cancellation_confirmation','metadata_json':{
        'lifecycleId':'previous-lifecycle','cancellationRequestedAt':NOW.isoformat()}}
    assert not isBillingNotificationEligible(delivery,{'subscription':row},NOW)


@pytest.mark.parametrize('unresolved,eligible',[(False,True),(True,False)])
def test_unknown_money_holds_even_a_current_cancellation_notice(unified,unresolved,eligible):
    from api.services.notifications.billingNotificationService import isBillingNotificationEligible
    from test.subscriptionModelFixtures import LIFECYCLE
    facts = factsFor('monthly')
    unified(replace(facts,renewalOptOut=True,moneyUnresolved=unresolved))
    end = facts.periods[0].end
    row = {'id':SUBSCRIPTION,'user_id':USER,'cancellation_requested_at':NOW.isoformat()}
    delivery = {'notification_type':'monthly_cancellation_confirmation','period_end':end,'metadata_json':{
        'lifecycleId':LIFECYCLE,'cancellationRequestedAt':NOW.isoformat(),'finalPaidEnd':end}}
    assert isBillingNotificationEligible(delivery,{'subscription':row},NOW) is eligible


def test_unified_renewal_notice_requires_original_lifecycle_metadata(unified):
    from api.services.notifications.billingNotificationService import isBillingNotificationEligible
    from test.subscriptionModelFixtures import LIFECYCLE
    facts = factsFor('monthly')
    now = facts.periods[0].end-timedelta(days=5)
    unified(facts,now)
    invoice = {'id':'renewal','userId':USER,'subscription_id':SUBSCRIPTION,'status':'UPCOMING',
        'period_start':facts.periods[0].end,'metadata_json':{'manualBilling':{'lifecycleId':LIFECYCLE,'revision':1}}}
    delivery = {'notification_type':'monthly_renewal_ready','period_end':facts.periods[0].end,
        'metadata_json':{'invoiceId':'renewal','invoiceRevision':1}}
    assert not isBillingNotificationEligible(delivery,{'subscription':{'id':SUBSCRIPTION,'user_id':USER},'invoice':invoice},now)


def test_unified_erasure_stages_take_financial_lock_before_legacy_lock(unified):
    from api.services.userErasureRepository import UserErasureRepository
    from api.services.billing.manualBillingRepository import _advisoryKey
    from test.test_user_erasure_repository import FakeConnection
    for operation in ('create','delete'):
        connection = FakeConnection()
        repository = UserErasureRepository(lambda:connection)
        if operation=='create':
            repository.createRequest(USER,'a'*64,'admin','synthetic-request',None)
        else:
            repository.deleteDatabaseData('synthetic-request',USER,[])
        locks = [params for sql,params in connection.state['executed'] if 'pg_advisory_xact_lock' in sql]
        assert locks[:2] == [(_advisoryKey(USER),),(USER,)]
        assert not any('auto_renew_enabled' in sql or 'renewal_due_at' in sql for sql,_ in connection.state['executed'])


def test_annual_expiry_intent_retains_year_interval_for_email(unified):
    from api.services.notifications.billingNotificationService import buildBillingNotificationIntent
    facts = factsFor('annual')
    end = facts.periods[0].end
    unified(facts,end)
    intent = buildBillingNotificationIntent({'type':'monthly_subscription_expired'},
        {'subscription':{'id':SUBSCRIPTION,'user_id':USER},'cycleId':end.isoformat()},end)
    assert intent['metadata']['billingInterval']=='year'


def test_reconciliation_still_recovers_money_before_closing_checkouts(monkeypatch):
    from types import SimpleNamespace
    from nubrix.triggers.tasks.reconciliationTask import ReconciliationTask
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    calls = []
    repository = SimpleNamespace(expireStaleCheckouts=lambda:calls.append('expire') or {})
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repository)
    monkeypatch.setattr(ManualBillingRecoveryService,'execute',lambda self:calls.append('recover') or {})
    monkeypatch.setattr(ManualCreditRepository,'recoverUsage',lambda self:{})
    task = ReconciliationTask.__new__(ReconciliationTask)
    task.razorpayClient = object()
    task._reconcileStaleAttempts = lambda:{'resolved':0,'errors':0}
    task._generateAnomalyReport = lambda:{'totalAnomalies':0}
    task.execute()
    assert calls == ['recover','expire']


def test_cancellation_notice_with_old_paid_end_cannot_dispatch(unified):
    from api.services.notifications.billingNotificationService import isBillingNotificationEligible
    from test.subscriptionModelFixtures import LIFECYCLE
    facts = factsFor('paid_future')
    unified(replace(facts,renewalOptOut=True))
    oldEnd = facts.periods[0].end
    row = {'id':SUBSCRIPTION,'user_id':USER,'cancellation_requested_at':NOW}
    delivery = {'notification_type':'monthly_cancellation_confirmation','period_end':oldEnd,
        'metadata_json':{'lifecycleId':LIFECYCLE,'cancellationRequestedAt':NOW,'finalPaidEnd':oldEnd}}
    assert not isBillingNotificationEligible(delivery,{'subscription':row},NOW)
