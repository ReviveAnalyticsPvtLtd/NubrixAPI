"""Pure grant rules for a captured payment under automatic capture (spec 4.4).

The first captured payment in an unsettled slot grants its own attempt's frozen snapshot;
every other captured payment is returned in full. The checks below run in exactly the
spec order; the repository gathers the facts under the owner lock.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

Outcome = Literal['attention', 'replay', 'legacy', 'refund', 'grant']
REFUND_REASONS = ('duplicate', 'account_closed', 'paid_after_close', 'not_deliverable')


@dataclass(frozen=True)
class CaptureFacts:
    evidenceMatches: bool
    alreadyFinalized: bool
    paymentCreatedAt: datetime
    rulesEffectiveAt: datetime
    slotSettledByOther: bool
    ownerClosed: bool
    attemptClosedAt: datetime | None
    deliverable: bool


def classifyCapture(f: CaptureFacts) -> tuple[Outcome, str | None]:
    if not f.evidenceMatches:
        return 'attention', None  # Cannot prove whose money: neither grant nor refund.
    if f.alreadyFinalized:
        return 'replay', None
    if f.paymentCreatedAt < f.rulesEffectiveAt:
        return 'legacy', None  # Payments started before the cutover keep today's handling.
    if f.slotSettledByOther:
        return 'refund', 'duplicate'
    if f.ownerClosed:
        return 'refund', 'account_closed'
    if f.attemptClosedAt is not None and f.paymentCreatedAt >= f.attemptClosedAt:
        return 'refund', 'paid_after_close'
    if not f.deliverable:
        return 'refund', 'not_deliverable'
    return 'grant', None


def attemptClosureTime(expiresAt: datetime | None, *closedAt: datetime | None) -> datetime | None:
    """When the attempt stopped accepting payments: its TTL expiry (``expiresAt``) or the
    supersede/decline/close moment, whichever came first."""
    moments = [moment for moment in (expiresAt, *closedAt) if moment is not None]
    return min(moments) if moments else None


def providerCreatedAt(payment: dict) -> datetime | None:
    """A provider payment entity's ``created_at`` (seconds since the epoch), or None when absent."""
    raw = payment.get('created_at') if isinstance(payment, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
        return None
    return datetime.fromtimestamp(raw, tz=timezone.utc)
