"""Pure purchase slot keys and the Pay decision (open, reuse, replace, confirm, finalize)."""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Literal

from api.services.billing.manualBillingContracts import CheckoutProviderSnapshot
from api.services.billing.paymentPolicy import paymentIsDead

_KINDS = {'initial_purchase': 'pro_term', 'renewal': 'pro_term',
          'expert_addition': 'expert_addition', 'topup': 'topup'}
_FRESHNESS = timedelta(seconds=60)


@dataclass(frozen=True)
class LiveAttempt:
    attemptId: str
    payloadHash: str
    expiresAt: datetime
    providerOrderId: str | None
    invoiceRevision: int | None


@dataclass(frozen=True)
class PayDecision:
    action: Literal['open', 'reuse', 'replace', 'confirming', 'finalize_first', 'busy']
    attemptId: str | None


def slotKind(purpose: str) -> Literal['pro_term', 'expert_addition', 'topup']:
    try:
        return _KINDS[purpose]
    except KeyError:
        raise ValueError('PAYMENT_SLOT_UNKNOWN_PURPOSE') from None


def _iso(value: datetime | None) -> str:
    if value is None or value.tzinfo is None:
        raise ValueError('PAYMENT_SLOT_KEY_INVALID')
    return value.astimezone(timezone.utc).isoformat()


def slotKey(purpose: str, *, currentTermEnd: datetime | None, renewalPeriodStart: datetime | None) -> str:
    kind = slotKind(purpose)
    if kind == 'topup':
        return 'topup'
    if kind == 'expert_addition':
        return 'current:' + _iso(currentTermEnd)
    if purpose == 'initial_purchase':
        return 'open'
    if currentTermEnd is None:
        _iso(renewalPeriodStart)  # an ended-term renewal must name its period
        return 'open'
    return _iso(currentTermEnd)


def _parsed(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def attemptSlotKey(frozen: dict, attemptedAt: datetime | None) -> str | None:
    """The slot key an attempt was reserved in; derived for attempts reserved before keys were recorded."""
    if frozen.get('slotKey'):
        return str(frozen['slotKey'])
    purpose = frozen.get('purpose')
    if purpose not in _KINDS:
        return None
    try:
        if purpose == 'expert_addition':
            return slotKey(purpose, currentTermEnd=_parsed(frozen.get('periodEnd')), renewalPeriodStart=None)
        if purpose == 'renewal':
            start = _parsed(frozen.get('periodStart'))
            ended = start is not None and attemptedAt is not None and start <= attemptedAt
            return slotKey(purpose, currentTermEnd=None if ended else start, renewalPeriodStart=start)
        return slotKey(purpose, currentTermEnd=None, renewalPeriodStart=None)
    except ValueError:
        return None


def _paymentCreatedAt(payment: dict) -> datetime | None:
    raw = payment.get('created_at')
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return datetime.fromtimestamp(raw, tz=timezone.utc)


def decidePay(live: LiveAttempt | None, payloadHash: str, snapshot: CheckoutProviderSnapshot | None,
              now: datetime, windowMinutes: int | None = None) -> PayDecision:
    if live is None:
        return PayDecision('open', None)
    attemptId = live.attemptId
    sameInsideTtl = live.payloadHash == payloadHash and now < live.expiresAt
    if live.providerOrderId is None:
        return PayDecision('reuse' if sameInsideTtl else 'busy', attemptId)
    if snapshot is None or not snapshot.complete or snapshot.attemptId != attemptId \
            or snapshot.providerOrderId != live.providerOrderId \
            or not timedelta(0) <= now - snapshot.observedAt <= _FRESHNESS:
        return PayDecision('busy', attemptId)
    statuses = [payment.get('status') for payment in snapshot.payments]
    # 'refunded' was captured earlier; outcome 'captured' also carries an Order the provider shows as paid.
    if snapshot.outcome == 'captured' or 'captured' in statuses or 'refunded' in statuses:
        return PayDecision('finalize_first', attemptId)
    if snapshot.outcome == 'unknown':
        return PayDecision('busy', attemptId)
    heldDead = False
    for payment in snapshot.payments:
        if payment.get('status') == 'authorized':
            createdAt = _paymentCreatedAt(payment)
            if createdAt is None:
                return PayDecision('busy', attemptId)
            if not paymentIsDead(createdAt, now, windowMinutes):
                return PayDecision('confirming', attemptId)
            heldDead = True
    # Razorpay rejects new payments on an Order holding an authorized payment until it auto-refunds.
    if heldDead:
        return PayDecision('replace', attemptId)
    return PayDecision('reuse' if sameInsideTtl else 'replace', attemptId)


_SETTLED = ('captured', 'refunded')


def _outcome(statuses) -> str:
    # Same classes as checkoutRecoveryPolicy, from payments only: the settled ones are gone.
    if set(statuses) - {'created', 'authorized', 'failed'}:
        return 'unknown'
    if 'authorized' in statuses:
        return 'authorized'
    return 'processing' if 'created' in statuses else 'unpaid'


def decideAfterFinalize(live: LiveAttempt, payloadHash: str, snapshot: CheckoutProviderSnapshot,
                        now: datetime, windowMinutes: int | None = None) -> PayDecision:
    """Decide again once every captured payment is finalized, on the payments still in play."""
    remaining = tuple(p for p in snapshot.payments if p.get('status') not in _SETTLED)
    rest = replace(snapshot, payments=remaining, outcome=_outcome([p.get('status') for p in remaining]))
    decision = decidePay(live, payloadHash, rest, now, windowMinutes)
    if decision.action == 'reuse':
        return PayDecision('replace', decision.attemptId)  # An Order that took money takes no other payment.
    if decision.action == 'finalize_first':
        return PayDecision('busy', decision.attemptId)
    return decision
