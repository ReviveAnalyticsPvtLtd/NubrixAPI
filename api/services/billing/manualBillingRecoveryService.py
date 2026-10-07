"""Recover provider outcomes using durable identities; never repeat a debit."""
from datetime import datetime, timezone
import json

from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence


class ManualBillingRecoveryService:
    def __init__(self, repository, provider):
        self.repository = repository
        self.provider = provider

    @staticmethod
    def _pages(fetch, parameters=None):
        skip = 0
        while True:
            page = fetch({**(parameters or {}), 'count':100, 'skip':skip})
            if not isinstance(page,dict) or not isinstance(page.get('items'),list):
                raise ValueError('RECOVERY_LISTING_INVALID')
            items = page['items']
            if any(not isinstance(item,dict) for item in items):
                raise ValueError('RECOVERY_LISTING_INVALID')
            yield from items
            if len(items) < 100:
                return
            skip += len(items)

    def recoverAttempt(self, attempt):
        orderId = attempt.get('provider_order_id')
        if not orderId:
            matches = []
            listed = False
            for order in self._pages(self.provider.order.all, {'receipt':str(attempt['id'])}):
                listed = True
                notes = order.get('notes') or {}
                if (order.get('receipt') == str(attempt['id'])
                    and notes.get('attemptId') == str(attempt['id'])
                    and notes.get('invoiceId') == str(attempt['invoice_id'])
                    and (attempt['user_id'] is None or notes.get('userId') == attempt['user_id'])
                    and int(order.get('amount',-1)) == int(attempt['amount'])
                    and order.get('currency') == attempt['currency']):
                    matches.append(order)
            if not matches and not listed:
                return int(self.repository.closeUncreatedOrder(attempt['id']))
            if len(matches) != 1:
                return 0
            order = matches[0]
            self.repository.bindProviderOrder(str(attempt['id']),order)
            orderId = order['id']
        metadata = attempt.get('metadata_json') or {}
        metadata = json.loads(metadata) if isinstance(metadata,str) else metadata
        frozen = metadata.get('manualBilling',{})
        # No timestamp on a payment entity is treated as proof of capture time.
        resolved = 0
        for payment in self._pages(lambda params:self.provider.order.payments(orderId,params)):
            if payment.get('status') != 'captured':
                continue
            if payment.get('order_id') != orderId:
                raise ValueError('RECOVERY_PAYMENT_ORDER_MISMATCH')
            evidence = VerifiedPaymentEvidence(str(attempt['id']),str(attempt['invoice_id']),
                attempt['user_id'],orderId,payment['id'],frozen.get('purpose','erased_checkout'),payment['currency'],
                'captured','server_observation',int(payment['amount']),datetime.now(timezone.utc),
                None,None,False)
            result = self.repository.finalizeCapturedPayment(evidence)
            resolved += int(result.state != 'awaiting_capture')
        if frozen.get('closedAt'):
            # A successful complete provider listing settles cancellation wording;
            # a later capture is still audited against the original cutoff.
            self.repository.markClosedAttemptReconciled(str(attempt['id']))
        return resolved

    def recoverRefund(self, intent):
        metadata = intent['metadata_json']
        metadata = json.loads(metadata) if isinstance(metadata,str) else metadata
        from api.services.billing.subscriptionRefundService import submitReservedRefund, _ProductionRefundProvider
        # Old intents without the durable submissions map have unknown send
        # history; never infer that they were not submitted.
        if 'submissions' in metadata and any(item['paymentId'] not in metadata['submissions'] for item in metadata['items']):
            submitted = submitReservedRefund(self.repository, _ProductionRefundProvider(self.provider), self.repository._refundIntent(metadata))
            if submitted.refundState == 'processed':
                return 1
        resolved = 0
        for item in metadata['items']:
            paymentId = item['paymentId']
            for refund in self._pages(lambda params:self.provider.payment.fetch_multiple_refund(paymentId,params)):
                if ((refund.get('notes') or {}).get('refundIntentId') != str(intent['id'])
                    or refund.get('payment_id') != paymentId
                    or int(refund.get('amount',-1)) != int(item['amount'])):
                    continue
                self.repository.settleRefundEvidence(str(intent['id']),{'refunds':[refund]})
                resolved += 1
        return resolved

    def execute(self):
        summary = {'resolved':0,'errors':0,'checked':0,'noProgress':0}
        for row in self.repository.pendingRecoveryRows():
            outcome='failed'
            try:
                resolved = (self.recoverAttempt(row) if row['event_category'] == 'payment_attempt'
                    else self.recoverRefund(row))
                summary['resolved'] += resolved
                summary['noProgress'] += int(resolved==0)
                outcome='resolved' if resolved else 'no_progress'
            except Exception:
                # Keep the original identity and reserve; no blind recreation.
                summary['errors'] += 1
            finally:
                summary['checked'] += 1
                try:
                    self.repository.recordRecoveryCheck(str(row['id']),outcome)
                except Exception:
                    summary['errors'] += 1
        return summary
