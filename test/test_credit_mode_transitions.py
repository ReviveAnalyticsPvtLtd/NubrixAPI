"""SQL allocation identities survive mode changes and delayed completion."""
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch
import pytest
from test.test_manual_billing_runtime import database, USER, NOW, read_row, sqlTransaction
from test.test_manual_checkout_http import checkout_database
from test.test_manual_payment_entrypoints import paid_then_request
from test.test_manual_payment_entrypoints import evidence
from api.services.credits.manualCreditRepository import ManualCreditRepository


@pytest.mark.parametrize('mode',['monthly_prepaid','annual_prepaid'])
def test_admission_captures_complete_mode_and_subscription_identity(checkout_database,mode):
    repository,path=checkout_database
    paid_then_request(checkout_database,mode,'topup')
    credits=ManualCreditRepository(repository)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        context=credits.admit(USER,'reporting_query','complete-identity')
    assert context.subscriptionId==read_row(path,'subscriptions')['id']
    assert context.billingMode==mode and context.quotaWatermark>0


def test_old_monthly_context_cannot_debit_new_annual_quota(checkout_database):
    repository,path=checkout_database
    paid_then_request(checkout_database,'monthly_prepaid','topup')
    credits=ManualCreditRepository(repository)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        context=credits.admit(USER,'reporting_query','old-month')
        with sqlTransaction(path) as connection:
            connection.execute("UPDATE subscriptions SET billing_mode='annual_prepaid',plan_type='annual'")
            connection.execute("UPDATE credit_balances SET plan_tier='annual',remaining_tokens=9999")
        result=credits.settle(context,200,'delayed-call')
    assert result['historicalPeriod'] and read_row(path,'credit_balances')['remaining_tokens']==9999


def test_incomplete_context_cannot_acquire_current_allocation(checkout_database):
    repository,path=checkout_database
    paid_then_request(checkout_database,'monthly_prepaid','topup')
    credits=ManualCreditRepository(repository)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        context=credits.admit(USER,'reporting_query','incomplete')
        with pytest.raises(ValueError,match='CREDIT_CONTEXT'):
            credits.settle(replace(context,subscriptionId=None),100,'call')


def test_trial_context_cannot_debit_new_monthly_quota(checkout_database):
    repository,path=checkout_database
    repository.activateTrial(USER,('banking',))
    credits=ManualCreditRepository(repository)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        context=credits.admit(USER,'reporting_query','trial-work')
        before=read_row(path,'credit_balances')['credit_period_id']
        paid_then_request(checkout_database,'monthly_prepaid','topup')
        fresh=read_row(path,'credit_balances')
        result=credits.settle(context,100,'old-trial-call')
    assert result['historicalPeriod'] and fresh['credit_period_id']!=before
    assert read_row(path,'credit_balances')['remaining_tokens']==fresh['remaining_tokens']


def test_retry_rechecks_eligibility_after_expiry(checkout_database):
    repository,path=checkout_database
    paid_then_request(checkout_database,'monthly_prepaid','topup')
    credits=ManualCreditRepository(repository)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        credits.admit(USER,'reporting_query','retry')
        clock.now.return_value=NOW+timedelta(days=32)
        with pytest.raises(ValueError,match='PAID_COVERAGE'):
            credits.admit(USER,'reporting_query','retry')


def test_topup_clawback_and_settlement_preserve_shared_bucket(checkout_database):
    repository,path=checkout_database
    manual,request=paid_then_request(checkout_database,'monthly_prepaid','topup')
    intent=manual.createCheckout(request)
    manual.finalizeCapturedPayment(evidence(intent,'topup-paid'))
    credits=ManualCreditRepository(repository)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        context=credits.admit(USER,'reporting_query','usage')
        with sqlTransaction(path) as connection:
            connection.execute('UPDATE credit_balances SET remaining_tokens=2000')
        credits.reportUsage(context,2050,'call')
        credits.settle(context,2050,'call')
        refund=credits.clawbackTopup(USER,'refund-one','topup-paid',intent.amount//2)
        replay=credits.clawbackTopup(USER,'refund-one','topup-paid',intent.amount//2)
    expected=5000000-(5000000*(intent.amount//2)//intent.amount)-50
    assert refund['clawed'] and not replay['clawed']
    assert read_row(path,'credit_balances')['topup_tokens']==expected


def test_annual_allowance_roll_has_new_identity_and_preserves_topups(checkout_database):
    repository,path=checkout_database
    paid_then_request(checkout_database,'annual_prepaid','topup')
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE credit_balances SET used_tokens=500,remaining_tokens=9999500,topup_tokens=123')
    before=read_row(path,'credit_balances')
    credits=ManualCreditRepository(repository)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW+timedelta(days=32)
        rolled=credits.balanceSnapshot(USER)
    assert rolled['credit_period_id']!=before['credit_period_id'] and rolled['used_tokens']==0
    assert rolled['topup_tokens']==123 and rolled['plan_tier']=='annual'
