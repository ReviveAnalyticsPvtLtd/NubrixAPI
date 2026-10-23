"""Full refund of a captured payment that was never granted (any purpose), never proration."""
from api.services.billing.purchaseProvider import purchaseProviderOptions


def compensationReady():
    # Pure configuration/installation check: called under the owner transaction.
    # Schema and a recent actual worker heartbeat are checked by the repository.
    from api.services.billing.purchaseOperationWorker import HANDLER_VERSION
    try:
        purchaseProviderOptions()
    except (ValueError,TypeError):
        return False
    return HANDLER_VERSION == 1


class PurchaseCompensationService:
    def __init__(self, repository, provider):
        self.repository, self.provider = repository, provider

    def _inspect(self, operation):
        payment = self.provider.payment.fetch(operation['provider_payment_id'],**purchaseProviderOptions())
        if (not isinstance(payment,dict) or payment.get('id') != operation['provider_payment_id']
                or payment.get('order_id') != operation['provider_order_id']
                or payment.get('status') not in ('captured','refunded')
                or type(payment.get('amount')) is not int or payment['amount'] != operation['amount']
                or payment.get('currency') != operation['currency']
                or type(payment.get('amount_refunded')) is not int):
            raise ValueError('PURCHASE_REFUND_EVIDENCE_INVALID')
        from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
        refunds = list(ManualBillingRecoveryService._pages(lambda params:
            self.provider.payment.fetch_multiple_refund(operation['provider_payment_id'],params,**purchaseProviderOptions())))
        seen = set()
        for refund in refunds:
            if (not isinstance(refund.get('id'),str) or not refund['id'] or refund['id'] in seen
                    or refund.get('payment_id') != operation['provider_payment_id']
                    or refund.get('currency') != operation['currency']
                    or type(refund.get('amount')) is not int or refund['amount'] <= 0
                    or refund.get('status') not in ('pending','processed','failed')):
                raise ValueError('PURCHASE_REFUND_HISTORY_INVALID')
            seen.add(refund['id'])
        processed = sum(r['amount'] for r in refunds if r['status']=='processed')
        pending = sum(r['amount'] for r in refunds if r['status']=='pending')
        # A known pending refund may already be reflected in the payment's
        # aggregate. It never establishes completed return or permits another POST.
        if not processed<=payment['amount_refunded']<=processed+pending or processed+pending > operation['amount']:
            raise ValueError('PURCHASE_REFUND_HISTORY_INCONSISTENT')
        # Store only allowlisted facts, not raw provider notes/contact/acquirer data.
        return {'processed':processed,'pending':pending,'refunds':[
            {k:r[k] for k in ('id','amount','status')} | {
                'ours': isinstance(r.get('notes'),dict) and r['notes'].get('purchaseOperationId')==str(operation['id']),
                'submissionKey':r.get('notes',{}).get('purchaseSubmissionKey') if isinstance(r.get('notes'),dict) else None}
            for r in refunds]}

    def execute(self, operationId):
        operation = self.repository.operationById(operationId)
        if operation['state'] in ('succeeded','cancelled'):
            return operation
        if operation['state']=='queued' and getattr(self.provider,'retry_enabled',False) is True:
            self.repository.failPurchaseOperation(operationId,'PURCHASE_PROVIDER_RETRIES_ENABLED')
            self.repository.escalateOperationOnce(operationId,'PURCHASE_PROVIDER_RETRIES_ENABLED')
            return self.repository.operationById(operationId)
        try:
            facts = self._inspect(operation)
            result = self.repository.recordRefundEvidence(operationId,facts)
            if result['state']=='succeeded':
                return result
            if operation['state'] != 'queued' or facts['pending']:
                return result
            claim = self.repository.beginCompensationSubmission(operationId, operation['operation_revision'])
            if not claim:
                return self.repository.operationById(operationId)
        except ValueError:
            self.repository.failPurchaseOperation(operationId,'PURCHASE_REFUND_HISTORY_INVALID')
            self.repository.escalateOperationOnce(operationId,'PURCHASE_REFUND_HISTORY_INVALID')
            return self.repository.operationById(operationId)
        except Exception:
            return self.repository.operationById(operationId)
        try:
            # A provider key supplements, rather than replaces, the local fence.
            # No ambiguous request is resent even if lookup temporarily finds none.
            self.provider.payment.refund(claim['provider_payment_id'], {
                'amount':claim['submission_evidence']['amount'], 'speed':'normal',
                'notes':{'purchaseOperationId':operationId,'purchaseSubmissionKey':claim['submission_evidence']['key']},
                'receipt':operationId},
                headers={'X-Refund-Idempotency':claim['submission_evidence']['key']},**purchaseProviderOptions())
        except Exception:
            self.repository.markCompensationUnknown(operationId)
            return self.repository.operationById(operationId)
        return self.recover(operationId)

    def recover(self, operationId):
        operation = self.repository.operationById(operationId)
        if operation['state'] in ('succeeded','cancelled'):
            return operation
        try:
            result = self.repository.recordRefundEvidence(operationId,self._inspect(operation))
            if result['state']=='needs_attention' and result.get('error_code')=='PURCHASE_REFUND_FAILED':
                self.repository.escalateOperationOnce(operationId,'PURCHASE_REFUND_FAILED')
                result=self.repository.operationById(operationId)
            return result
        except ValueError:
            self.repository.failPurchaseOperation(operationId,'PURCHASE_REFUND_HISTORY_INVALID')
            self.repository.escalateOperationOnce(operationId,'PURCHASE_REFUND_HISTORY_INVALID')
        except Exception:
            self.repository.markCompensationUnknown(operationId)
        return self.repository.operationById(operationId)
