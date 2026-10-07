"""Actual speech and LangChain callback admission precedes provider execution."""
from datetime import datetime,timedelta
from types import SimpleNamespace
from unittest.mock import Mock,patch
import pytest
from test.test_manual_billing_runtime import database, USER, NOW, read_row, sqlTransaction
from test.test_manual_checkout_http import checkout_database
from test.test_manual_payment_entrypoints import paid_then_request


def test_speech_admission_failure_prevents_provider_call(monkeypatch):
    from api.services.utilityService import UtilityService
    from api.services.credits.creditService import creditService
    service=UtilityService.__new__(UtilityService)
    service.speechToTextModule=Mock()
    monkeypatch.setattr(creditService,'admitCreditOperation',Mock(side_effect=ValueError('NO_ACCESS')),raising=False)
    with pytest.raises(Exception,match='NO_ACCESS'):
        service.getSpeechTranscript(SimpleNamespace(b64String='audio'),USER)
    service.speechToTextModule.getTranscript.assert_not_called()


def test_speech_crosses_boundary_without_charging_new_quota(checkout_database,monkeypatch):
    from api.services.utilityService import UtilityService
    repository,path=checkout_database
    paid_then_request(checkout_database,'monthly_prepaid','topup')
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repository)
    service=UtilityService.__new__(UtilityService)
    def transcribe(**kwargs):
        with sqlTransaction(path) as connection:
            connection.execute("UPDATE credit_balances SET credit_period_id='new-period',remaining_tokens=10000")
        return {'text':'spoken words','duration':60}
    service.speechToTextModule=SimpleNamespace(getTranscript=transcribe)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        assert service.getSpeechTranscript(SimpleNamespace(b64String='audio'),USER)=='spoken words'
    assert read_row(path,'credit_balances')['remaining_tokens']==10000
    with sqlTransaction(path) as connection:
        assert connection.execute("SELECT count(*) FROM billing_events WHERE event_type='credit.operation_settled'").fetchone()[0]==1


def test_usage_survives_settlement_outage(checkout_database,monkeypatch):
    from api.services.credits.creditService import creditService
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    repository,path=checkout_database
    paid_then_request(checkout_database,'monthly_prepaid','topup')
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repository)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        context=creditService.admitCreditOperation(USER,'reporting_query','outage')
        with patch.object(ManualCreditRepository,'settle',side_effect=RuntimeError('settlement unavailable')):
            with pytest.raises(RuntimeError):
                creditService.settleCreditOperation(context,1234,'call')
    with sqlTransaction(path) as connection:
        row=connection.execute("SELECT metadata_json FROM billing_events WHERE event_type='credit.usage_reported' AND event_status='PENDING'").fetchone()
    import json
    assert json.loads(row[0])['tokensUsed']==1234


def test_callback_trial_to_monthly_keeps_original_context(checkout_database,monkeypatch):
    from api.services.credits.creditTrackingCallback import CreditTrackingCallback
    repository,path=checkout_database
    repository.activateTrial(USER,('banking',))
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repository)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        callback=CreditTrackingCallback(USER,'reporting_query','trial-call')
        paid_then_request(checkout_database,'monthly_prepaid','topup')
        before=read_row(path,'credit_balances')['remaining_tokens']
        callback.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=SimpleNamespace(usage_metadata={'total_tokens':1000}))]]),run_id='completed')
    assert read_row(path,'credit_balances')['remaining_tokens']==before


def test_queued_retry_rechecks_access_at_actual_execution(checkout_database,monkeypatch):
    from api.services.credits.creditTrackingCallback import CreditTrackingCallback
    repository,_=checkout_database
    paid_then_request(checkout_database,'monthly_prepaid','topup')
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repository)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        callback=CreditTrackingCallback(USER,'reporting_query','queued')
        clock.now.return_value=NOW+timedelta(days=32)
        with pytest.raises(ValueError,match='PAID_COVERAGE'):
            callback.on_llm_start({},['prompt'],run_id='late-run')


def test_report_outage_retains_measured_usage_for_recovery(checkout_database,monkeypatch,tmp_path):
    from api.services.credits.creditService import creditService
    from api.services.credits.manualCreditRepository import ManualCreditRepository
    from api.services.credits.creditUsageSpool import recoverSpooledUsage
    repository,path=checkout_database
    paid_then_request(checkout_database,'monthly_prepaid','topup')
    monkeypatch.setenv('CREDIT_USAGE_SPOOL_DIR',str(tmp_path/'usage-spool'))
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repository)
    with patch('api.services.credits.manualCreditRepository.datetime',wraps=datetime) as clock:
        clock.now.return_value=NOW
        context=creditService.admitCreditOperation(USER,'reporting_query','report-outage')
        before=read_row(path,'credit_balances')['remaining_tokens']
        with patch.object(ManualCreditRepository,'reportUsage',side_effect=RuntimeError('SQL unavailable')):
            with pytest.raises(RuntimeError):
                creditService.settleCreditOperation(context,4321,'provider-run')
        assert len(list((tmp_path/'usage-spool').glob('*.json')))==1
        assert recoverSpooledUsage(ManualCreditRepository(repository))=={'settled':1,'errors':0}
        assert recoverSpooledUsage(ManualCreditRepository(repository))=={'settled':0,'errors':0}
    assert read_row(path,'credit_balances')['remaining_tokens']==before-4321
