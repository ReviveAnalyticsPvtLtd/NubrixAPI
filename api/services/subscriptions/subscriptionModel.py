"""Immutable inputs and pure projection of verified, owned subscription facts.

Only accepted purchased periods belong here, never a quote or an old status
label. Loading/reconciling evidence and all financial writes stay repository
responsibilities. No caller is wired to this model until the migration gates.
"""
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Literal

Interval = Literal['month', 'year']
SubscriptionStatus = Literal['none', 'trial', 'active', 'expired']


class SubscriptionAuthorityUnavailable(RuntimeError):
    """Missing or conflicting authority; consumers must return unavailable/503."""


@dataclass(frozen=True)
class TrialFacts:
    startedAt: datetime | None = None
    endsAt: datetime | None = None
    closedAt: datetime | None = None
    experts: tuple[str, ...] = ()
    consumed: bool = False  # Reviewed consumed proof may have unknown dates.


@dataclass(frozen=True)
class PurchasedPeriod:
    id: str
    userId: str
    subscriptionId: str
    lifecycleId: str
    invoiceId: str
    start: datetime
    end: datetime
    experts: tuple[str, ...]
    billingInterval: Interval
    creditPeriodId: str | None = None
    revokedAt: datetime | None = None


@dataclass(frozen=True)
class RecoverableCheckout:
    invoiceId: str
    attemptId: str
    state: str


@dataclass(frozen=True)
class SubscriptionFacts:
    userId: str
    subscriptionId: str
    lifecycleId: str | None
    planCode: Literal['pro'] | None
    evidenceVerified: bool
    trial: TrialFacts = field(default_factory=TrialFacts)
    periods: tuple[PurchasedPeriod, ...] = ()
    renewalOptOut: bool = False
    accountBanned: bool = False
    erasurePending: bool = False
    moneyUnresolved: bool = False
    recoverableCheckout: RecoverableCheckout | None = None
    nextSelectedExperts: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SubscriptionSnapshotV2:
    subscriptionId: str
    lifecycleId: str | None
    planCode: Literal['pro'] | None
    billingInterval: Interval | None
    status: SubscriptionStatus
    accessAllowed: bool
    accessSource: Literal['none', 'trial', 'paid']
    denialReason: str | None
    trialAvailable: bool
    trial: TrialFacts | None
    currentPeriod: PurchasedPeriod | None
    nextPeriod: PurchasedPeriod | None
    paidUntil: datetime | None
    lastPaidEnd: datetime | None
    subscribedExperts: tuple[str, ...]
    domainCount: int
    renewalOptOut: bool
    renewalAvailable: bool
    resumeRenewalAvailable: bool
    cancellationEffectiveAt: datetime | None
    purchaseAllowed: bool
    purchaseBlockReason: Literal['current_coverage', 'recovery_required', 'account_restricted'] | None
    recoverableCheckout: RecoverableCheckout | None
    evaluatedAt: datetime
    nextSelectedExperts: tuple[str, ...] | None = None
    schemaVersion: Literal[2] = field(default=2, init=False)


def evaluateSubscription(facts: SubscriptionFacts, now: datetime) -> SubscriptionSnapshotV2:
    """Project verified facts without changing coverage, preferences or credits."""
    now = _utc(now)
    trial, periods = _validatedFacts(facts)
    valid = sorted((p for p in periods if p.revokedAt is None or p.revokedAt > now),
                   key=lambda p: p.start)
    if any(_serviceEnd(p) <= p.start for p in valid):
        raise SubscriptionAuthorityUnavailable('Scheduled revocation precedes service')
    for previous, following in zip(valid, valid[1:]):
        if _serviceEnd(previous) > following.start:
            raise SubscriptionAuthorityUnavailable('Overlapping accepted coverage')

    current = next((p for p in valid if p.start <= now < _serviceEnd(p)), None)
    future = [p for p in valid if p.start > now]
    if len(future) > 1 or (future and current is None):
        raise SubscriptionAuthorityUnavailable('Unattached or multiple future paid periods')
    upcoming = future[0] if future else None
    if current and current.lifecycleId != facts.lifecycleId:
        raise SubscriptionAuthorityUnavailable('Current lifecycle identity mismatch')
    if upcoming and (upcoming.start != _serviceEnd(current)
                     or upcoming.lifecycleId != current.lifecycleId
                     or upcoming.billingInterval != current.billingInterval):
        raise SubscriptionAuthorityUnavailable('Future coverage is not an adjacent renewal')

    paidHistory = bool(periods)
    trialConsumed = trial.consumed or trial.startedAt is not None
    # Accepted paid service permanently takes precedence, even when a legacy
    # trial-close write was missing. A later trial deadline cannot revive it.
    trialCurrent = (not paidHistory and trial.startedAt is not None
                    and trial.startedAt <= now < trial.endsAt
                    and (trial.closedAt is None or now < trial.closedAt))
    status: SubscriptionStatus = ('active' if current else 'trial' if trialCurrent
                                  else 'expired' if paidHistory or trialConsumed else 'none')
    source = 'paid' if current else 'trial' if trialCurrent else 'none'
    restriction = ('account_banned' if facts.accountBanned else
                   'erasure_pending' if facts.erasurePending else None)
    moneyUnresolved = facts.moneyUnresolved or facts.recoverableCheckout is not None
    purchaseBlock = ('account_restricted' if restriction else
                     'recovery_required' if moneyUnresolved else
                     'current_coverage' if current else None)
    paidUntil = _serviceEnd(upcoming or current) if current else None
    latest = max(periods, key=lambda p: (p.start, p.end, p.id)) if periods else None
    experts = current.experts if current else trial.experts if trialCurrent else ()

    return SubscriptionSnapshotV2(
        subscriptionId=facts.subscriptionId, lifecycleId=facts.lifecycleId,
        planCode='pro' if paidHistory or trialConsumed else facts.planCode,
        billingInterval=current.billingInterval if current else latest.billingInterval if latest else None,
        status=status, accessAllowed=source != 'none' and restriction is None,
        accessSource=source,
        denialReason=restriction or ('expired' if status == 'expired' else
                                     'no_subscription' if status == 'none' else None),
        trialAvailable=not paidHistory and not trialConsumed and restriction is None,
        trial=trial if trial.startedAt is not None else None,
        currentPeriod=_publicPeriod(current), nextPeriod=_publicPeriod(upcoming),
        paidUntil=paidUntil,
        lastPaidEnd=max((_serviceEnd(p) for p in periods), default=None),
        subscribedExperts=experts, domainCount=len(experts),
        renewalOptOut=facts.renewalOptOut,
        renewalAvailable=bool(current and not upcoming and not facts.renewalOptOut
                              and not restriction and not moneyUnresolved),
        resumeRenewalAvailable=bool(current and facts.renewalOptOut and not restriction),
        cancellationEffectiveAt=paidUntil if facts.renewalOptOut else None,
        purchaseAllowed=purchaseBlock is None, purchaseBlockReason=purchaseBlock,
        recoverableCheckout=facts.recoverableCheckout, evaluatedAt=now,
        nextSelectedExperts=(upcoming.experts if upcoming else facts.nextSelectedExperts) if current else None,
    )


def evaluatePurchaseEligibility(facts: SubscriptionFacts, now: datetime) -> tuple[bool, str | None]:
    """One purchase policy, including the snapshot's account and money guards."""
    snapshot = evaluateSubscription(facts, now)
    return snapshot.purchaseAllowed, snapshot.purchaseBlockReason


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SubscriptionAuthorityUnavailable('Timezone-aware timestamp required')
    return value.astimezone(timezone.utc)


def _identity(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _experts(value, *, allowEmpty=False):
    if (not isinstance(value, tuple) or (not value and not allowEmpty)
            or any(not _identity(expert) for expert in value) or len(set(value)) != len(value)):
        raise SubscriptionAuthorityUnavailable('Verified immutable expert selection required')


def _validatedFacts(facts: SubscriptionFacts) -> tuple[TrialFacts, tuple[PurchasedPeriod, ...]]:
    if not isinstance(facts, SubscriptionFacts) or facts.evidenceVerified is not True:
        raise SubscriptionAuthorityUnavailable('Subscription evidence has not been verified')
    if not _identity(facts.userId) or not _identity(facts.subscriptionId):
        raise SubscriptionAuthorityUnavailable('Subscription ownership required')
    if facts.lifecycleId is not None and not _identity(facts.lifecycleId):
        raise SubscriptionAuthorityUnavailable('Invalid lifecycle identity')
    if facts.planCode not in (None, 'pro'):
        raise SubscriptionAuthorityUnavailable('Unknown product')
    if any(type(getattr(facts, key)) is not bool for key in (
            'renewalOptOut', 'accountBanned', 'erasurePending', 'moneyUnresolved')):
        raise SubscriptionAuthorityUnavailable('Explicit guard facts required')
    trial = facts.trial
    if not isinstance(trial, TrialFacts) or type(trial.consumed) is not bool:
        raise SubscriptionAuthorityUnavailable('Verified trial history required')
    _experts(trial.experts, allowEmpty=trial.startedAt is None)
    if (trial.startedAt is None) != (trial.endsAt is None):
        raise SubscriptionAuthorityUnavailable('Incomplete trial dates')
    if trial.startedAt is not None:
        trial = replace(trial, startedAt=_utc(trial.startedAt), endsAt=_utc(trial.endsAt),
                        closedAt=_utc(trial.closedAt) if trial.closedAt is not None else None)
        if trial.endsAt <= trial.startedAt or (trial.closedAt is not None and trial.closedAt < trial.startedAt):
            raise SubscriptionAuthorityUnavailable('Invalid trial interval')
    elif trial.closedAt is not None:
        raise SubscriptionAuthorityUnavailable('Trial closure without trial dates')
    if not isinstance(facts.periods, tuple):
        raise SubscriptionAuthorityUnavailable('Immutable purchased history required')
    if facts.nextSelectedExperts is not None:
        _experts(facts.nextSelectedExperts)
    periods = []
    ids, invoices = set(), set()
    for period in facts.periods:
        if not isinstance(period, PurchasedPeriod):
            raise SubscriptionAuthorityUnavailable('Accepted purchased period required')
        if (period.userId != facts.userId or period.subscriptionId != facts.subscriptionId
                or not all(_identity(value) for value in (period.id, period.lifecycleId, period.invoiceId))
                or period.id in ids or period.invoiceId in invoices):
            raise SubscriptionAuthorityUnavailable('Purchased period ownership or identity mismatch')
        if period.billingInterval not in ('month', 'year'):
            raise SubscriptionAuthorityUnavailable('Unknown purchased interval')
        if period.creditPeriodId is not None and not _identity(period.creditPeriodId):
            raise SubscriptionAuthorityUnavailable('Invalid allocation identity')
        _experts(period.experts)
        period = replace(period, start=_utc(period.start), end=_utc(period.end),
                         revokedAt=_utc(period.revokedAt) if period.revokedAt is not None else None)
        if period.end <= period.start:
            raise SubscriptionAuthorityUnavailable('Invalid purchased interval')
        ids.add(period.id)
        invoices.add(period.invoiceId)
        periods.append(period)
    recovery = facts.recoverableCheckout
    if recovery is not None and (not isinstance(recovery, RecoverableCheckout)
                                or not all(_identity(v) for v in (recovery.invoiceId, recovery.attemptId, recovery.state))):
        raise SubscriptionAuthorityUnavailable('Invalid owned checkout recovery')
    return trial, tuple(periods)


def _serviceEnd(period: PurchasedPeriod) -> datetime:
    return min(period.end, period.revokedAt) if period.revokedAt else period.end


def _publicPeriod(period: PurchasedPeriod | None) -> PurchasedPeriod | None:
    # A scheduled audited revocation shortens the service projection, never the
    # original invoice/fact object. It cannot promise access beyond revocation.
    return replace(period, end=_serviceEnd(period)) if period else None
