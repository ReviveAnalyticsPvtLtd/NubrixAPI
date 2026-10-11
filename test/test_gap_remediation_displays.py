import os
from datetime import timedelta
from unittest.mock import Mock, patch
from jose import jwt
import pytest
from test.test_manual_billing_runtime import database, USER, NOW, sqlTransaction
from test.test_manual_checkout_http import checkout_database
from test.test_manual_payment_entrypoints import paid_then_request, evidence


def test_credit_display_preserves_stored_topups_without_claiming_spendability(checkout_database,monkeypatch):
    from api.services.credits.creditService import CreditService
    from test.test_final_review_regressions import AnnualSqlClient
    repo,path=checkout_database
    manual,checkout=paid_then_request(checkout_database,'monthly_prepaid','initial_purchase')
    period=manual.finalizeCapturedPayment(evidence(manual.createCheckout(checkout))).currentPeriod
    with sqlTransaction(path) as db: db.execute('update credit_balances set topup_tokens=12345')
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repo)
    service=CreditService();service._supabase=AnnualSqlClient(path)
    later=period.end+timedelta(seconds=1)
    with patch('api.services.credits.manualCreditRepository.datetime',Mock(now=lambda _:later)), patch('api.services.billing.manualBillingRepository._now',return_value=later):
        snapshot=service.getBalanceSnapshot(USER)
    assert snapshot['storedTopupTokens']==12345
    assert snapshot['spendableTopupTokens']==0
    assert snapshot['nextRefillAt'] is None


def test_profile_reports_opt_out_and_paid_future_period(checkout_database,monkeypatch):
    from api.services.managementService import ManagementService
    from test.test_manual_auth_coverage import SqlRestClient
    repo,path=checkout_database
    manual,checkout=paid_then_request(checkout_database,'monthly_prepaid','renewal')
    future=manual.createCheckout(checkout)
    manual.finalizeCapturedPayment(evidence(future,'future-payment'))
    repo.setRenewalOptOut(USER,True,'Leaving','cancel')
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repo)
    service=ManagementService.__new__(ManagementService);service.client=SqlRestClient(path)
    from api.services.credits.creditService import CreditService
    credit=CreditService();credit._supabase=service.client
    monkeypatch.setattr('api.services.credits.creditService.creditService',credit)
    token=jwt.encode({'userId':USER,'email':'profile@example.test','sub_status':'active','plan_type':'pro'},os.environ['SECRET_KEY'],algorithm='HS256')
    with patch('api.services.credits.manualCreditRepository.datetime',Mock(now=lambda _:NOW)):
        result=service.getUserProfile(token)
    assert result['plan']['renewalOptOut'] is True
    assert result['plan']['nextPeriod']['domains']==['banking']
    assert result['plan']['cancellationEffectiveEnd']==result['plan']['nextPeriod']['end']
