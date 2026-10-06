from datetime import datetime
from unittest.mock import patch
import pytest
from test.test_manual_billing_runtime import database,USER,NOW,read_row,sqlTransaction
from test.test_manual_checkout_http import checkout_database
from test.test_manual_payment_entrypoints import paid_then_request
from api.services.credits.manualCreditRepository import ManualCreditRepository


@pytest.mark.parametrize('reset',[False,True])
def test_staff_quota_mutation_preserves_topups_and_fences_old_work(checkout_database,reset):
    repo,path=checkout_database
    paid_then_request(checkout_database,'monthly_prepaid','topup')
    credits=ManualCreditRepository(repo)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        context=credits.admit(USER,'reporting_query','before-config-reset')
        with sqlTransaction(path) as db:
            db.execute('UPDATE credit_balances SET monthly_token_quota=10000,used_tokens=100,remaining_tokens=9900,topup_tokens=123')
        with patch('api.services.credits.creditConfig.getTokenQuotaForPlan',return_value=20000):
            result=credits.resizeQuota(USER,1,False,resetUsage=reset)
        before=read_row(path,'credit_balances')
        settled=credits.settle(context,50,'old-call')
    assert result['applied']
    assert before['topup_tokens']==123 and before['monthly_token_quota']==20000
    if reset:
        assert before['remaining_tokens']==20000 and before['used_tokens']==0
        assert before['credit_period_id']!=context.creditPeriodId
        assert settled['historicalPeriod'] and read_row(path,'credit_balances')['remaining_tokens']==20000
    else:
        assert before['remaining_tokens']==9900 and before['used_tokens']==100


def test_no_quota_mutation_reactivates_expired_access(checkout_database):
    repo,path=checkout_database
    paid_then_request(checkout_database,'monthly_prepaid','topup')
    with sqlTransaction(path) as db:
        db.execute("UPDATE subscriptions SET status='expired'")
    credits=ManualCreditRepository(repo)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        from datetime import timedelta
        clock.now.return_value=NOW+timedelta(days=32)
        result=credits.resizeQuota(USER,1,True,resetUsage=True)
    assert not result['applied']


def test_current_coverage_includes_committed_expert_additions(checkout_database):
    from test.test_manual_payment_entrypoints import evidence
    repo,_=checkout_database
    manual,checkout=paid_then_request(checkout_database,'monthly_prepaid','expert_addition')
    intent=manual.createCheckout(checkout)
    result=manual.finalizeCapturedPayment(evidence(intent))
    assert set(result.currentPeriod.domains)=={'banking','telecom'}


def test_credit_ready_cannot_use_another_mode_allocation(checkout_database):
    from test.test_manual_payment_entrypoints import evidence
    repo,path=checkout_database
    manual,checkout=paid_then_request(checkout_database,'monthly_prepaid','initial_purchase')
    intent=manual.createCheckout(checkout)
    manual.finalizeCapturedPayment(evidence(intent))
    with sqlTransaction(path) as db:
        db.execute("UPDATE credit_balances SET plan_tier='annual',lifecycle_id='other-life'")
    result=manual.finalizeCapturedPayment(evidence(intent))
    assert result.creditState=='pending_materialization'


def test_actual_staff_bulk_reset_updates_durable_balance(checkout_database,monkeypatch):
    repo,path=checkout_database
    paid_then_request(checkout_database,'monthly_prepaid','topup')
    with sqlTransaction(path) as db:
        db.execute('UPDATE credit_balances SET used_tokens=100,remaining_tokens=remaining_tokens-100,topup_tokens=123')
    before=read_row(path,'credit_balances')
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repo)
    from api.services.credits.creditService import CreditService
    from test.test_manual_auth_coverage import SqlRestClient
    service=CreditService()
    service.supabase=SqlRestClient(path)
    service._redis=lambda: (_ for _ in ()).throw(RuntimeError('test cache unavailable'))
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        result=service.forceResetAllQuotas(resetUsage=True)
    after=read_row(path,'credit_balances')
    assert result['updatedCount']==1
    assert after['credit_period_id']!=before['credit_period_id'] and after['used_tokens']==0
    assert after['topup_tokens']==123


def test_durable_credit_database_failure_is_unreadable_never_cache_fallback(monkeypatch):
    from api.services.credits.creditService import CreditService
    class BrokenRepository:
        def activateDueCoverage(self,*args): raise RuntimeError('test database unavailable')
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:BrokenRepository())
    service=CreditService()
    service._redis=lambda:pytest.fail('Redis cannot authorize a SQL failure')
    assert service.getRemainingTokens(USER)==-1
