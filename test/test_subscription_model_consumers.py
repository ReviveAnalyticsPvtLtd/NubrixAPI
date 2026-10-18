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
