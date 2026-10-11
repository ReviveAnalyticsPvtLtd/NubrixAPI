from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
import pytest


def test_lost_order_ack_is_bound_before_shared_capture_finalization():
    repository = Mock()
    provider = Mock()
    attempt = {'id':'attempt','invoice_id':'invoice','user_id':'user','amount':100,
        'currency':'INR','metadata_json':{'manualBilling':{'purpose':'renewal'}}}
    order = {'id':'order','receipt':'attempt','amount':100,'currency':'INR',
        'notes':{'attemptId':'attempt','invoiceId':'invoice','userId':'user'}}
    provider.order.all.return_value = {'items':[order]}
    provider.order.payments.return_value = {'items':[{'id':'payment','order_id':'order',
        'amount':100,'currency':'INR','status':'captured','created_at':1}]}
    repository.finalizeCapturedPayment.return_value = SimpleNamespace(state='paid_scheduled')
    service = ManualBillingRecoveryService(repository,provider)
    assert service.recoverAttempt(attempt) == 1
    repository.bindProviderOrder.assert_called_once_with('attempt',order)
    evidence = repository.finalizeCapturedPayment.call_args.args[0]
    assert evidence.providerPaymentId == 'payment'
    assert evidence.provenCaptureAt is None and not evidence.timingVerified
    provider.order.create.assert_not_called()


def test_wrong_order_receipt_owner_never_binds_or_grants():
    repository,provider = Mock(),Mock()
    provider.order.all.return_value = {'items':[{'id':'other','receipt':'attempt',
        'amount':100,'currency':'INR','notes':{'attemptId':'attempt','invoiceId':'invoice','userId':'other'}}]}
    attempt={'id':'attempt','invoice_id':'invoice','user_id':'user','amount':100,'currency':'INR'}
    assert ManualBillingRecoveryService(repository,provider).recoverAttempt(attempt) == 0
    repository.bindProviderOrder.assert_not_called()
    repository.finalizeCapturedPayment.assert_not_called()


def test_unknown_refund_recovers_by_intent_without_resubmission():
    repository,provider=Mock(),Mock()
    provider.payment.fetch_multiple_refund.return_value={'items':[{'id':'refund','payment_id':'pay',
        'amount':200,'status':'processed','notes':{'refundIntentId':'intent'}}]}
    intent={'id':'intent','metadata_json':{'items':[{'paymentId':'pay','amount':200}]}}
    assert ManualBillingRecoveryService(repository,provider).recoverRefund(intent) == 1
    repository.settleRefundEvidence.assert_called_once_with('intent',{'refunds':provider.payment.fetch_multiple_refund.return_value['items']})
    provider.payment.refund.assert_not_called()


def test_external_pending_refund_blocks_new_unused_time_return_before_closure():
    from api.services.billing.subscriptionRefundService import _ProductionRefundProvider, RefundConflictError
    provider=Mock()
    provider.payment.fetch.return_value={'id':'pay','amount':3000,'currency':'INR','status':'captured','amount_refunded':0}
    provider.payment.fetch_multiple_refund.return_value={'items':[{'id':'external','status':'pending','amount':2000}]}
    with pytest.raises(RefundConflictError,match='EXTERNAL_REFUND'):
        _ProductionRefundProvider(provider).verifyUnreturnedCapture({'paymentId':'pay','originalAmount':3000,'currency':'INR'})
    provider.payment.refund.assert_not_called()
