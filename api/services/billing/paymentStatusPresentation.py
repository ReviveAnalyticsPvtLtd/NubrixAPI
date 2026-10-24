"""The customer's payment status (spec 4.7): one checkout, the owner's refunds and one support reference.

Every surface that tells a customer whether Pay waits, a refund is moving or support is needed reads it
from here. Attempt, invoice, Order, payment and operation identities stay out of everything shown: the
checkout's attempt ID is for API binding only, and ``paymentAttention`` carries the single reference.
"""
import hashlib
from datetime import datetime, timedelta, timezone

from api.services.billing.manualBillingPresentation import purchaseSelectionLabel

PURPOSES = ('initial_purchase', 'renewal', 'expert_addition', 'topup')
WORKING_DAYS_BEFORE_DELAYED = 10
_CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
# A failed provider send the refund worker retries on its own within a minute; it needs a person only
# once escalated (after 30 minutes unresolved).
_SELF_RETRIED = 'PAYMENT_REFUND_FAILED'
# Captures decided by the new rules (or that should have been) and left for a person; historical
# pre-cutover anomalies keep today's handling and are never shown.
_ATTENTION_RULES = ('CAPTURE_RULES', 'RULES_FACTS_MISSING')
_EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


def supportReference(operationId: str) -> str:
    """'NBX-' and four Crockford base32 characters of SHA-256(operationId): short, readable, stable."""
    bits = int.from_bytes(hashlib.sha256(str(operationId).encode('utf-8')).digest()[:3], 'big') >> 4
    return 'NBX-' + ''.join(_CROCKFORD[(bits >> shift) & 31] for shift in (15, 10, 5, 0))


def workingDaysAfter(start: datetime, days: int) -> datetime:
    """``start`` moved forward by ``days`` working days (Monday to Friday, UTC), keeping its time of day."""
    moment, counted = start, 0
    while counted < days:
        moment += timedelta(days=1)
        if moment.weekday() < 5:
            counted += 1
    return moment


def refundState(operationState: str, startedAt: datetime, now: datetime) -> str:
    """'returned' once processed; 'delayed' after more than ten working days without; else 'on_its_way'."""
    if operationState == 'succeeded':
        return 'returned'
    return 'delayed' if now > workingDaysAfter(startedAt, WORKING_DAYS_BEFORE_DELAYED) else 'on_its_way'


def checkoutState(attempt: dict, now: datetime) -> str:
    """The one reducer: completed once granted; confirming while an authorized payment may still capture
    (the same evidence and window Pay holds on, closed attempts included); otherwise ready."""
    from api.services.subscriptions.subscriptionModelBackfill import paymentInFlight
    if attempt.get('payment_status') == 'captured':
        return 'completed'
    return 'confirming_earlier_payment' if paymentInFlight(attempt, now) else 'ready'


def _attempts(cursor, userId):
    from api.services.billing.manualBillingRepository import ManualBillingRepository, _utc
    cursor.execute('''SELECT * FROM public.billing_events WHERE user_id=%s AND event_category='payment_attempt' ''',
                   (userId,))
    rows = []
    for row in cursor.fetchall():
        frozen = ManualBillingRepository._json(row.get('metadata_json')).get('manualBilling') or {}
        if frozen.get('purpose') in PURPOSES:
            rows.append((row, frozen))
    # Newest last; at one instant a still-open attempt is newer than the one it replaced.
    rows.sort(key=lambda entry: (_utc(entry[0].get('attempted_at') or entry[0].get('created_at')) or _EARLIEST,
                                 not entry[1].get('closedAt'), str(entry[0]['id'])))
    return rows


def checkoutAttempt(cursor, userId: str, now: datetime):
    """(row, frozen) of the checkout the customer is shown: the newest one Pay would wait on, else the newest."""
    from api.services.subscriptions.subscriptionModelBackfill import paymentInFlight
    rows = _attempts(cursor, userId)
    held = [entry for entry in rows if paymentInFlight(entry[0], now)]
    return (held or rows or [None])[-1]


def needsProviderRefresh(attempt: dict, now: datetime) -> bool:
    """A status read asks the provider only about an Order whose money can still change what is shown."""
    from api.services.billing.manualBillingRepository import ManualBillingRepository
    frozen = ManualBillingRepository._json(attempt.get('metadata_json')).get('manualBilling') or {}
    if not attempt.get('provider_order_id') or attempt.get('payment_status') == 'captured':
        return False
    open_ = not frozen.get('closedAt') and attempt.get('payment_status') in ('created', 'pending_provider_ack', 'authorized')
    return open_ or checkoutState(attempt, now) == 'confirming_earlier_payment'


def _storage(cursor):
    cursor.execute("SELECT to_regclass('public.payment_operations') AS relation")
    row = cursor.fetchone()
    return bool(row['relation'] if isinstance(row, dict) else row[0])


def presentPaymentStatus(cursor, userId: str, now: datetime) -> dict:
    """``{'checkout', 'refunds', 'paymentAttention'}`` for the owner, from committed facts only."""
    from api.services.billing.manualBillingRepository import ManualBillingRepository, _utc
    shown = checkoutAttempt(cursor, userId, now)
    checkout = None
    if shown is not None:
        row, frozen = shown
        checkout = {'state': checkoutState(row, now), 'attemptId': str(row['id']), 'amount': int(row['amount']),
                    'currency': row['currency'], 'selectionLabel': purchaseSelectionLabel(frozen)}

    refunds, attention = [], []
    if _storage(cursor):
        cursor.execute('''SELECT id,reason,state,amount,currency,error_code,escalation_reference,created_at
            FROM public.payment_operations WHERE user_id=%s AND kind='refund' AND state<>'cancelled' ''', (userId,))
        operations = sorted(cursor.fetchall(), key=lambda op: (_utc(op['created_at']), str(op['id'])), reverse=True)
        for op in operations:
            startedAt = _utc(op['created_at'])
            refunds.append({'amount': int(op['amount']), 'currency': op['currency'], 'reason': op['reason'],
                            'state': refundState(op['state'], startedAt, now), 'startedAt': startedAt.isoformat()})
            if op['state'] != 'succeeded' and (op['escalation_reference'] or (
                    op['state'] == 'needs_attention' and op['error_code'] != _SELF_RETRIED)):
                attention.append((startedAt, str(op['id'])))
    cursor.execute('''SELECT id,metadata_json,occurred_at,created_at FROM public.billing_events
        WHERE user_id=%s AND event_type='payment.capture' AND event_status='REQUIRES_RECONCILIATION' ''', (userId,))
    for capture in cursor.fetchall():
        if ManualBillingRepository._json(capture.get('metadata_json')).get('rulesFacts') in _ATTENTION_RULES:
            attention.append((_utc(capture.get('occurred_at') or capture.get('created_at')) or _EARLIEST,
                              str(capture['id'])))
    paymentAttention = {'reference': supportReference(max(attention)[1])} if attention else None
    return {'checkout': checkout, 'refunds': refunds, 'paymentAttention': paymentAttention}
