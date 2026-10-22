"""Recover provider outcomes using durable identities; never repeat a debit."""
from datetime import datetime, timezone
import json

from api.services.billing.manualBillingContracts import CheckoutProviderSnapshot, VerifiedPaymentEvidence
from api.services.billing.checkoutRecoveryPolicy import classifyCheckoutProviderOutcome


class ManualBillingRecoveryService:
    def __init__(self, repository, provider, requestOptions=None):
        self.repository = repository
        self.provider = provider
        self.requestOptions = dict(requestOptions or {})

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

    def inspectAttempt(self, attempt):
        from api.services.billing.manualBillingRepository import _now
        orderId = attempt['provider_order_id']
        payments = ()
        complete = False
        outcome = 'unknown'
        try:
            order = self.provider.order.fetch(orderId,**self.requestOptions)
            if (not isinstance(order, dict) or order.get('id') != orderId
                    or int(order.get('amount', -1)) != int(attempt['amount'])
                    or order.get('currency') != attempt['currency']):
                raise ValueError('RECOVERY_ORDER_EVIDENCE_MISMATCH')
            payments = tuple(self._pages(lambda params: self.provider.order.payments(orderId, params,**self.requestOptions)))
            if any(not payment.get('id') or payment.get('order_id') != orderId
                    or int(payment.get('amount', -1)) != int(attempt['amount'])
                    or payment.get('currency') != attempt['currency'] for payment in payments):
                raise ValueError('RECOVERY_PAYMENT_EVIDENCE_MISMATCH')
            complete = True
            outcome = classifyCheckoutProviderOutcome(order, payments, complete)
        except Exception:
            # Incomplete evidence also invalidates a previous unpaid observation.
            complete = False
        return CheckoutProviderSnapshot(str(attempt['id']), orderId, outcome, _now(),
            complete, int(attempt['amount']), attempt['currency'], payments)

    def recoverAttempt(self, attempt):
        orderId = attempt.get('provider_order_id')
        if not orderId:
            matches = []
            listed = False
            for order in self._pages(lambda params:self.provider.order.all(params,**self.requestOptions), {'receipt':str(attempt['id'])}):
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
            attempt = {**attempt, 'provider_order_id': orderId}
        snapshot = self.inspectAttempt(attempt)
        results = self.applyObservation(attempt, snapshot)
        return sum(result.state != 'awaiting_capture' for result in results)

    def applyObservation(self, attempt, snapshot):
        """Persist one inspection and settle only its verified captured money."""
        self.repository.recordProviderObservation(str(attempt['id']), snapshot)
        if snapshot.outcome == 'unknown':
            raise RuntimeError('PROVIDER_STATUS_UNKNOWN')
        metadata = attempt.get('metadata_json') or {}
        metadata = json.loads(metadata) if isinstance(metadata,str) else metadata
        frozen = metadata.get('manualBilling',{})
        # No timestamp on a payment entity is treated as proof of capture time.
        results = []
        for payment in snapshot.payments:
            if payment.get('status') != 'captured':
                continue
            if payment.get('order_id') != attempt['provider_order_id']:
                raise ValueError('RECOVERY_PAYMENT_ORDER_MISMATCH')
            evidence = VerifiedPaymentEvidence(str(attempt['id']),str(attempt['invoice_id']),
                attempt['user_id'],attempt['provider_order_id'],payment['id'],frozen.get('purpose','erased_checkout'),payment['currency'],
                'captured','server_observation',int(payment['amount']),snapshot.observedAt,
                None,None,False)
            result = self.repository.finalizeCapturedPayment(evidence)
            if evidence.purpose=='initial_purchase' and result.state=='requires_reconciliation':
                from api.services.billing.purchaseCheckoutRepository import PurchaseCheckoutRepository
                PurchaseCheckoutRepository(self.repository.connectionFactory).reserveCompensation(evidence)
            results.append(result)
        if frozen.get('closedAt'):
            # A complete listing may end the closed attempt's scan: an empty one for an ordinary
            # closure, recover_until for a superseded one (a later capture is still finalized).
            self.repository.recordClosedAttemptScan(str(attempt['id']), snapshot)
        return tuple(results)

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
