"""Provider observations distinguish an unpaid checkout from unsettled money."""
from datetime import timedelta


def classifyCheckoutProviderOutcome(order, payments, complete):
    if not complete or order.get('status') not in ('created', 'attempted', 'paid'):
        return 'unknown'
    statuses = {payment.get('status') for payment in payments}
    if statuses - {'created', 'authorized', 'captured', 'failed'}:
        return 'unknown'
    if 'captured' in statuses:
        return 'captured'
    if order.get('status') == 'paid':
        return 'unknown'
    if 'authorized' in statuses:
        return 'authorized'
    if 'created' in statuses:
        return 'processing'
    return 'unpaid'


def mayExpireCheckout(snapshot, now):
    return (snapshot.complete and snapshot.outcome == 'unpaid' and not snapshot.payments
        and timedelta(0) <= now - snapshot.observedAt <= timedelta(seconds=60))
