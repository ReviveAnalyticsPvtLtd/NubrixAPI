"""Bounded recovery of original identities even while public rollout is disabled."""
from api.services.billing.manualBillingRepository import _now
from api.services.billing.purchaseProvider import purchaseProviderOptions

HANDLER_VERSION = 1


def reconcileCancelledCheckout(repository,provider,attemptId):
    from api.services.billing.manualBillingRecoveryService import ManualBillingRecoveryService
    from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
    attempt=repository.purchaseAttempt(attemptId)
    recovery=ManualBillingRecoveryService(repository,provider,requestOptions=purchaseProviderOptions())
    if not attempt['provider_order_id']:
        recovery.recoverAttempt(attempt)
        attempt=repository.purchaseAttempt(attemptId)
        if not attempt['provider_order_id']:
            return False
    snapshot=recovery.inspectAttempt(attempt)
    # A refunded entity proves a historical debit, never new entitlement. Only
    # an already accepted cancellation may interpret it through correction.
    if snapshot.complete and any(p.get('status')=='refunded' for p in snapshot.payments):
        control=repository.purchaseControl(attemptId)
        if control and control['cancellation_accepted_at']:
            from dataclasses import replace
            snapshot=replace(snapshot,outcome='captured',payments=tuple(
                {**p,'status':'captured'} if p.get('status')=='refunded' else p for p in snapshot.payments))
    if not snapshot.complete or snapshot.outcome=='unknown':
        repository.recordProviderObservation(attemptId,snapshot)
        return False
    recovery.applyObservation(attempt,snapshot)
    for payment in snapshot.payments:
        if payment['status']=='captured':
            repository.reserveCompensation(VerifiedPaymentEvidence(attemptId,str(attempt['invoice_id']),
                attempt['user_id'],attempt['provider_order_id'],payment['id'],'initial_purchase',payment['currency'],
                'captured','server_observation',payment['amount'],snapshot.observedAt,None,None,False))
    return repository.resolvePurchaseCancellation(attemptId,snapshot)


class PurchaseOperationWorker:
    def __init__(self,repository,provider):
        self.repository,self.provider=repository,provider

    def execute(self,now=None):
        now=now or _now()
        summary={'checked':0,'resolved':0,'noProgress':0,'errors':0,'escalated':0}
        if hasattr(self.repository,'recordPurchaseWorkerHeartbeat') and not self.repository.recordPurchaseWorkerHeartbeat():
            return summary
        highWater={source:self.repository.purchaseScanHighWater(source)
            for source in ('cancellation','capture','compensation')}
        for source,upper in highWater.items():
            if upper is None:
                continue
            after=None
            while True:
                page=self.repository.purchaseScanPage(source,upper,after,now,limit=100)
                if not page:
                    break
                for row in page:
                    summary['checked']+=1
                    try:
                        result=self.repository.recoverPurchaseItem(source,row,self.provider,now)
                        summary['resolved']+=int(result['resolved'])
                        summary['noProgress']+=int(not result['resolved'])
                        summary['escalated']+=int(result['escalated'])
                    except Exception:
                        summary['errors']+=1
                        # Preserve retry/escalation even if provider/handler fails.
                        if source=='cancellation':
                            self.repository.scheduleCancellationRetry(str(row['id']),now)
                        else:
                            self.repository.schedulePurchaseRetry(str(row['id']),now)
                after=page[-1]['id']
        return summary
