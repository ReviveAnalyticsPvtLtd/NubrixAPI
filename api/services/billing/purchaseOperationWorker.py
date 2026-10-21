"""Bounded recovery of original identities even while public rollout is disabled."""
from api.services.billing.manualBillingRepository import _now

HANDLER_VERSION = 1


class PurchaseOperationWorker:
    def __init__(self,repository,provider):
        self.repository,self.provider=repository,provider

    def execute(self,now=None):
        now=now or _now()
        summary={'checked':0,'resolved':0,'noProgress':0,'errors':0,'escalated':0}
        if hasattr(self.repository,'recordPurchaseWorkerHeartbeat') and not self.repository.recordPurchaseWorkerHeartbeat():
            return summary
        upper=self.repository.purchaseScanHighWater('refund')
        if upper is None:
            return summary
        after=None
        while True:
            page=self.repository.purchaseScanPage('refund',upper,after,now,limit=100)
            if not page:
                break
            for row in page:
                summary['checked']+=1
                try:
                    result=self.repository.recoverPurchaseItem('refund',row,self.provider,now)
                    summary['resolved']+=int(result['resolved'])
                    summary['noProgress']+=int(not result['resolved'])
                    summary['escalated']+=int(result['escalated'])
                except Exception:
                    summary['errors']+=1
                    # Preserve retry/escalation even if provider/handler fails.
                    self.repository.schedulePurchaseRetry(str(row['id']),now)
            after=page[-1]['id']
        return summary
