"""Owned lifecycle maintenance for both manual Pro billing intervals.

No worker grants time or persists a computed subscription status. The owner-
locked repositories used by lazy credit reads also materialize allocations here.
"""
from datetime import timedelta

from api.services.subscriptions.paymentValidationService import utcNow
from utils.logger import logger


class SubscriptionWorkerService:
    def __init__(self, client, repository=None, snapshotService=None):
        self.client = client
        if repository is None:
            from api.services.billing.manualBillingRepository import getManualBillingRepository
            repository = getManualBillingRepository()
        self.repository = repository
        if snapshotService is None:
            from api.services.subscriptions.subscriptionSnapshotService import SubscriptionSnapshotService
            snapshotService = SubscriptionSnapshotService(repository.runtimeSnapshotFactsLoader)
        self.snapshots = snapshotService

    def _owners(self):
        after = None
        while True:
            page = self.client.table('subscriptions').select('id, user_id, erasure_pending').eq('is_canonical',True).order('id').limit(500)
            if after is not None:
                page = page.gt('id', after)
            rows = page.execute().data or []
            if not rows:
                return
            yield from rows
            after = rows[-1]['id']

    def execute(self, *, interval=None, reminders=False, trialWarnings=False, now=None):
        now = now or utcNow()
        result = dict(scanned=0, materialized=0, prepared=0, enqueued=0,
            expired=0, duplicates=0, skipped=0, errors=0)
        for row in self._owners():
            result['scanned'] += 1
            try:
                if row.get('erasure_pending'):
                    result['skipped'] += 1
                    continue
                owned = self.snapshots.get(row['user_id'], now)
                if str(row['id']) != owned.subscriptionId:
                    raise ValueError('WORKER_CANONICAL_IDENTITY_CHANGED')
                if interval and owned.billingInterval != interval:
                    result['skipped'] += 1
                    continue
                if owned.accessSource == 'paid' and owned.accessAllowed:
                    self.repository.activateDueCoverage(row['user_id'], now)
                    result['materialized'] += 1
                if trialWarnings and owned.status == 'trial':
                    import os
                    if os.environ.get('TRIAL_EXPIRY_EMAIL_MODE','legacy') != 'disabled':
                        from api.services.notifications.trialExpiryNotificationService import getTrialExpiryNotificationService
                        # The policy loads explicit trial authority again at enqueue.
                        delivery, created = getTrialExpiryNotificationService().enqueueEligible(row, now)
                        result['enqueued' if created else 'duplicates'] += int(delivery is not None)
                if reminders:
                    self._reminders(row, owned, now, result)
            except Exception:
                result['errors'] += 1
                logger.exception('Unified subscription worker could not verify owner {}',row.get('user_id'))
        return result

    def _reminders(self, row, owned, now, result):
        if owned.renewalOptOut or owned.nextPeriod or owned.purchaseBlockReason in ('recovery_required','account_restricted'):
            result['skipped'] += 1
            return
        invoice = None
        if owned.currentPeriod and owned.renewalAvailable:
            end = owned.currentPeriod.end
            if now < end - timedelta(days=30 if owned.billingInterval=='year' else 7):
                return
            context = self.repository.getRenewalContext(row['user_id'], now)
            if owned.billingInterval == 'year':
                invoice = self.repository.createAnnualRenewalInvoice(row['user_id'], row['id'])
                if invoice and now >= end-timedelta(days=7):
                    invoice = self.repository.prepareAnnualRenewalInvoice(invoice['id'])
            else:
                from dateutil.relativedelta import relativedelta
                invoice = self.repository.createFrozenRenewalInvoice({
                    'userId':row['user_id'],'subscription_id':row['id'],'billing_reason':'renewal',
                    'payment_flow':'razorpay_order_checkout','requires_customer_auth':True,
                    'period_start':end,'period_end':end+relativedelta(months=1),'status':'UPCOMING',
                    'metadata_json':{'manualBilling':{'lifecycleId':owned.lifecycleId,'purpose':'renewal',
                        'billingMode':'monthly_prepaid','domains':context['_verifiedCoverage']['nextExperts'],
                        'coverageState':'scheduled','revision':1}}},context['version'])
            if invoice:
                result['prepared'] += 1
            eventType = ('annual_renewal_upcoming' if owned.billingInterval=='year' and now < end-timedelta(days=7)
                else 'monthly_renewal_ready' if now < end-timedelta(days=1) else 'monthly_renewal_reminder')
        elif owned.status=='expired' and owned.lastPaidEnd and now-owned.lastPaidEnd <= timedelta(hours=25):
            end, eventType = owned.lastPaidEnd, 'monthly_subscription_expired'
        else:
            return
        from api.services.notifications.billingNotificationService import buildBillingNotificationIntent
        intent = buildBillingNotificationIntent({'type':eventType},
            {'subscription':row,'subscriptionSnapshot':owned,'invoice':invoice,'cycleId':end.isoformat()},now)
        if intent and self.repository.enqueueSubscriptionNotification(intent, now):
            result['enqueued'] += 1
