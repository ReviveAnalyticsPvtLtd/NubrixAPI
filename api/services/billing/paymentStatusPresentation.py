"""The customer's payment status (spec 4.7): one checkout, the owner's refunds and one support reference.

Every surface that tells a customer whether Pay waits, a refund is moving or support is needed reads it
from here. Attempt, invoice, Order, payment and operation identities stay out of everything shown: the
checkout's attempt ID is for API binding only, and ``paymentAttention`` carries the single reference.
"""
import hashlib
from datetime import datetime, timedelta, timezone

from api.services.billing.manualBillingPresentation import purchaseSelectionLabel

PURPOSES = ('initial_purchase', 'renewal', 'expert_addition', 'topup')
PRO_TERM_PURPOSES = ('initial_purchase', 'renewal')  # one shared slot (spec 4.1)
# Pages and tabs poll every 3 s: an Order observed this recently is not read from the provider again.
PROVIDER_REFRESH_INTERVAL = timedelta(seconds=5)
WORKING_DAYS_BEFORE_DELAYED = 10
_CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
# A failed provider send the refund worker retries on its own within a minute; it needs a person once
# escalated (unresolved past PAYMENT_REFUND_ESCALATION_SECONDS), and then shows the reference at once
# even while the worker keeps retrying.
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


def _attempts(cursor, userId, purposes=PURPOSES, subscriptionId=None):
    from api.services.billing.manualBillingRepository import ManualBillingRepository, _utc
    cursor.execute('''SELECT * FROM public.billing_events WHERE user_id=%s AND event_category='payment_attempt' ''',
                   (userId,))
    rows = []
    for row in cursor.fetchall():
        frozen = ManualBillingRepository._json(row.get('metadata_json')).get('manualBilling') or {}
        if frozen.get('purpose') in purposes and (subscriptionId is None
                                                  or str(row.get('subscription_id')) == str(subscriptionId)):
            rows.append((row, frozen))
    # Newest last; at one instant a still-open attempt is newer than the one it replaced.
    rows.sort(key=lambda entry: (_utc(entry[0].get('attempted_at') or entry[0].get('created_at')) or _EARLIEST,
                                 not entry[1].get('closedAt'), str(entry[0]['id'])))
    return rows


def checkoutAttempt(cursor, userId: str, now: datetime, purposes=PURPOSES, fallbackPurposes=None,
                    subscriptionId=None):
    """(row, frozen) of the checkout shown: the newest attempt of ``purposes`` that Pay would wait on
    (``paymentInFlight``, closed attempts included), else the newest attempt of ``fallbackPurposes``
    (default ``purposes``). Payment status and the purchase editor both select through here."""
    from api.services.subscriptions.subscriptionModelBackfill import paymentInFlight
    rows = _attempts(cursor, userId, purposes, subscriptionId)
    held = [entry for entry in rows if paymentInFlight(entry[0], now)]
    if held:
        return held[-1]
    fallback = rows if fallbackPurposes is None else [entry for entry in rows if entry[1].get('purpose') in fallbackPurposes]
    if not fallback:
        return None
    latest = fallback[-1]
    if latest[1].get('closedReason') == 'SLOT_SETTLED':
        # Follow the committed settlement of this slot only. A later independent
        # purchase keeps its place; the cancelled sibling is never relabelled paid.
        cursor.execute('''SELECT s.settled_payment_id FROM public.payment_slot_controls c
            JOIN public.payment_slots s ON s.slot_id=c.slot_id
            WHERE c.attempt_id=%s AND c.user_id=%s''', (latest[0]['id'], userId))
        slot = cursor.fetchone()
        if slot and slot['settled_payment_id']:
            cursor.execute('''SELECT c.attempt_id FROM public.payment_slot_controls c
                JOIN public.billing_events b ON b.id=c.attempt_id
                JOIN public.billing_events paid ON paid.provider_order_id=b.provider_order_id
                WHERE c.slot_id=(SELECT slot_id FROM public.payment_slot_controls WHERE attempt_id=%s)
                AND c.user_id=%s AND b.payment_status='captured' AND paid.provider_payment_id=%s
                AND paid.event_type='payment.capture' AND paid.event_status='FINALIZED' ''',
                (latest[0]['id'], userId, slot['settled_payment_id']))
            winner = cursor.fetchone()
            if winner:
                return next((entry for entry in rows if str(entry[0]['id']) == str(winner['attempt_id'])), latest)
    return latest


def needsProviderRefresh(attempt: dict, now: datetime) -> bool:
    """A status read asks the provider only about an Order whose money can still change what is shown,
    and not again within ``PROVIDER_REFRESH_INTERVAL`` of the last recorded observation."""
    from api.services.billing.manualBillingRepository import ManualBillingRepository, _utc
    frozen = ManualBillingRepository._json(attempt.get('metadata_json')).get('manualBilling') or {}
    if not attempt.get('provider_order_id') or attempt.get('payment_status') == 'captured':
        return False
    checked = _utc((frozen.get('providerObservation') or {}).get('checkedAt'))
    if checked is not None and timedelta(0) <= now - checked < PROVIDER_REFRESH_INTERVAL:
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
        cursor.execute('''SELECT id,attempt_id,reason,state,amount,currency,error_code,escalation_reference,created_at
            FROM public.payment_operations WHERE user_id=%s AND kind='refund' AND state<>'cancelled' ''', (userId,))
        operations = sorted(cursor.fetchall(), key=lambda op: (_utc(op['created_at']), str(op['id'])), reverse=True)
        for op in operations:
            startedAt = _utc(op['created_at'])
            # attemptId binds the refund to the checkout it returns money for; it is never displayed.
            refunds.append({'attemptId': str(op['attempt_id']), 'amount': int(op['amount']),
                            'currency': op['currency'], 'reason': op['reason'],
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
