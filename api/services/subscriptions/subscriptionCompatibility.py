"""Migration phase and V1 vocabulary adapters; never entitlement authority."""
import os
from collections.abc import Mapping
from typing import Literal

from fastapi import HTTPException

from api.services.subscriptions.subscriptionModel import Interval, SubscriptionSnapshotV2

ModelPhase = Literal['legacy', 'shadow', 'dual', 'v2', 'contracted']


def usesUnifiedSubscription() -> bool:
    return getSubscriptionModelPhase() in ('dual', 'v2', 'contracted')


def refreshSubscriptionProjection(userId, row):
    """Replace only the caller's display copy. Never persist derived state."""
    from api.services.subscriptions.subscriptionSnapshotService import getSubscriptionSnapshotService
    from utils.exceptionHandler import CustomException
    try:
        snapshot = row.get('_subscriptionSnapshot') or getSubscriptionSnapshotService().get(userId)
        if row.get('id') is not None and str(row['id']) != snapshot.subscriptionId:
            raise ValueError('Canonical identity changed')
    except Exception as exc:
        raise CustomException(exc, statusCode=503, errorCode='SUBSCRIPTION_AUTHORITY_UNAVAILABLE',
            uiMessage='Subscription access could not be verified. Please try again.') from exc
    aliases = legacySubscriptionFields(snapshot)
    current = snapshot.currentPeriod
    row.update(id=snapshot.subscriptionId, user_id=userId, status=snapshot.status,
        plan_type=aliases['planType'], billing_mode=aliases['billingMode'],
        current_period_start=(current.start.isoformat() if current else snapshot.trial.startedAt.isoformat() if snapshot.status == 'trial' else None),
        current_period_end=aliases['planExpire'], renewal_due_at=aliases['nextBilling'],
        subscribed_experts=list(snapshot.subscribedExperts), domain_count=snapshot.domainCount,
        renewal_opt_out=snapshot.renewalOptOut, _subscriptionSnapshot=snapshot,
        _verifiedCoverage={'experts':list(snapshot.subscribedExperts),
            'nextExperts':list(snapshot.nextSelectedExperts or snapshot.subscribedExperts)})
    import math
    deadline = current.end if current else snapshot.trial.endsAt if snapshot.status == 'trial' else None
    return max(0, math.ceil((deadline - snapshot.evaluatedAt).total_seconds()/86400)) if deadline else 0


def getSubscriptionModelPhase() -> ModelPhase:
    """Read at use time; parsing a phase never activates a rollout by itself."""
    phase = os.environ.get('SUBSCRIPTION_MODEL_PHASE', 'legacy')
    if phase not in ('legacy', 'shadow', 'dual', 'v2', 'contracted'):
        raise ValueError('Invalid SUBSCRIPTION_MODEL_PHASE')
    return phase


def resolveProBillingInput(payload: Mapping) -> tuple[Literal['pro'], Interval]:
    """Translate old manual mode inputs; reject partial/conflicting V2 inputs."""
    def invalid():
        raise HTTPException(status_code=422, detail={
            'code': 'INVALID_SUBSCRIPTION_INPUT',
            'message': 'Select Pro with month/year interval or a compatible billingMode.',
        })

    if not isinstance(payload, Mapping):
        invalid()
    hasNew = 'planCode' in payload or 'billingInterval' in payload
    interval = None
    if hasNew:
        if payload.get('planCode') != 'pro' or payload.get('billingInterval') not in ('month', 'year'):
            invalid()
        interval = payload['billingInterval']
    if 'billingMode' in payload:
        mode = payload['billingMode']
        if not isinstance(mode, str):
            invalid()
        legacyInterval = {'monthly_prepaid': 'month', 'monthly_recurring': 'month',
                          'annual_prepaid': 'year'}.get(mode)
        if legacyInterval is None or (interval is not None and interval != legacyInterval):
            invalid()
        interval = legacyInterval
    if interval is None:
        invalid()
    return 'pro', interval


def legacySubscriptionFields(snapshot: SubscriptionSnapshotV2) -> dict:
    """Compute V1 profile-plan aliases without granting from a plan label.

    Annual/free survive only here for old clients. Original invoices and catalog
    SKUs are not rewritten. Account guards remain independent of service status.
    """
    def timestamp(value):
        return value.isoformat() if value is not None else None

    def period(value):
        if value is None:
            return None
        return {'start': timestamp(value.start), 'end': timestamp(value.end),
                'domains': list(value.experts), 'creditPeriodId': value.creditPeriodId}

    current = snapshot.currentPeriod
    deadline = current.end if current else snapshot.trial.endsAt if snapshot.status == 'trial' else None
    planType = ('annual' if snapshot.billingInterval == 'year' else 'pro') if snapshot.status == 'active' else (
        'free' if snapshot.status == 'trial' or (snapshot.status == 'expired' and snapshot.billingInterval is None) else 'none')
    mode = {'month': 'monthly_prepaid', 'year': 'annual_prepaid'}.get(snapshot.billingInterval, 'none')
    return {
        'planType': planType, 'status': snapshot.status.upper(), 'planExpire': timestamp(deadline),
        'nextBilling': timestamp(current.end) if current else None,
        'billingMode': mode, 'renewalDueAt': timestamp(current.end) if current else None,
        'subscribedExperts': list(snapshot.subscribedExperts), 'domainCount': snapshot.domainCount,
        'renewalOptOut': snapshot.renewalOptOut,
        'cancellationEffectiveEnd': timestamp(snapshot.cancellationEffectiveAt),
        'currentPeriod': period(current), 'nextPeriod': period(snapshot.nextPeriod),
        'billingStateAvailable': True,
        'autoRenewEnabled': False, 'paymentCollectionMode': 'authenticated_checkout',
    }
