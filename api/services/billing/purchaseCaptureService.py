"""One durable capture submission; ambiguous outcomes are lookup-only."""
from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
from api.services.billing.manualBillingRepository import _now


class PurchaseCaptureService:
    def __init__(self, repository, provider):
        self.repository, self.provider = repository, provider

    @staticmethod
    def _validate(operation, payment):
        if (not isinstance(payment, dict) or payment.get('id') != operation['provider_payment_id']
                or payment.get('order_id') != operation['provider_order_id']
                or type(payment.get('amount')) is not int or payment['amount'] != operation['amount']
                or payment.get('currency') != operation['currency']
                or payment.get('status') not in ('created', 'authorized', 'captured', 'failed')):
            raise ValueError('CAPTURE_PROVIDER_EVIDENCE_MISMATCH')

    def _observed(self, operation, payment):
        self._validate(operation, payment)
        if payment['status'] == 'captured':
            evidence = VerifiedPaymentEvidence(str(operation['attempt_id']), str(operation['invoice_id']),
                operation['user_id'], operation['provider_order_id'], operation['provider_payment_id'],
                'initial_purchase', operation['currency'], 'captured', 'server_observation', operation['amount'],
                _now(), None, None, False)
            return self.repository.recordCaptureOutcome(str(operation['id']), evidence)
        if payment['status'] == 'failed':
            return self.repository.recordReleasedCapture(str(operation['id']))
        return self.repository.captureOperation(str(operation['id']))

    def execute(self, operationId):
        operation = self.repository.captureOperation(operationId)
        if operation['state'] in ('succeeded', 'cancelled'):
            return operation
        if operation['state'] != 'queued':
            return self.recover(operationId)
        payment = self.provider.payment.fetch(operation['provider_payment_id'])
        self._validate(operation, payment)
        if payment['status'] != 'authorized':
            return self._observed(operation, payment)
        from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
        attempt = self.repository.attemptById(operation['user_id'], str(operation['attempt_id']))
        recovery = ManualBillingRecoveryService(self.repository, self.provider)
        observation = recovery.inspectAttempt(attempt)
        if not observation.complete or observation.outcome == 'unknown':
            self.repository.recordProviderObservation(str(operation['attempt_id']), observation)
            return self.repository.captureOperation(operationId)
        recovery.applyObservation(attempt, observation)
        if observation.outcome == 'captured':
            own = next((item for item in observation.payments if item['id'] == operation['provider_payment_id']
                        and item['status'] == 'captured'), None)
            return self._observed(operation, own) if own else self.repository.stopCaptureForSettledOrder(operationId)
        active = [item for item in observation.payments if item['status'] in ('created', 'authorized')]
        if len(active) != 1 or active[0]['id'] != operation['provider_payment_id'] or active[0]['status'] != 'authorized':
            return self.repository.captureNeedsAttention(operationId)
        from api.services.billing.purchaseCheckoutService import requireManualCaptureCurrency
        requireManualCaptureCurrency(operation['currency'])
        claimed = self.repository.beginCaptureSubmission(operationId, operation['operation_revision'])
        if claimed is None:
            return self.repository.captureOperation(operationId)
        try:
            response = self.provider.payment.capture(operation['provider_payment_id'], operation['amount'],
                {'currency': operation['currency']})
            self._validate(operation, response)
            if response['status'] != 'captured':
                return self.repository.markCaptureUnknown(operationId)
            return self._observed(operation, response)
        except Exception:
            # The request may have been accepted or financial finalization may
            # already have committed. The durable fence prevents another POST.
            return self.repository.markCaptureUnknown(operationId)

    def recover(self, operationId):
        operation = self.repository.captureOperation(operationId)
        if operation['state'] in ('succeeded', 'cancelled'):
            return operation
        try:
            payment = self.provider.payment.fetch(operation['provider_payment_id'])
            return self._observed(operation, payment)
        except Exception:
            return self.repository.markCaptureUnknown(operationId)
