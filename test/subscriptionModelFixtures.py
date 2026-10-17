"""Synthetic, hand-dated accepted purchase facts; no live customer records."""
from datetime import datetime, timezone

from api.services.subscriptions.subscriptionModel import (
    PurchasedPeriod, SubscriptionFacts, TrialFacts,
)

NOW = datetime(2026, 10, 17, 10, tzinfo=timezone.utc)
USER = 'subscription-model-user'
SUBSCRIPTION = '00000000-0000-4000-8000-000000000001'
LIFECYCLE = '00000000-0000-4000-8000-000000000002'


def period(start, end, *, interval='month', id='period-current', **overrides):
    values = dict(id=id, userId=USER, subscriptionId=SUBSCRIPTION,
                  lifecycleId=LIFECYCLE, invoiceId=f'invoice-{id}',
                  start=start, end=end, experts=('banking', 'telecom'),
                  billingInterval=interval, creditPeriodId=None, revokedAt=None)
    return PurchasedPeriod(**(values | overrides))


def factsFor(condition):
    values = dict(userId=USER, subscriptionId=SUBSCRIPTION, lifecycleId=None,
                  planCode=None, evidenceVerified=True, trial=TrialFacts(), periods=())
    if condition == 'trial':
        values.update(planCode='pro', trial=TrialFacts(
            startedAt=datetime(2026, 10, 10, 10, tzinfo=timezone.utc),
            endsAt=datetime(2026, 10, 22, 10, tzinfo=timezone.utc),
            experts=('banking', 'manufacturing', 'supplychain', 'telecom')))
    elif condition in ('monthly', 'expired', 'paid_future'):
        end = datetime(2026, 11, 17, 10, tzinfo=timezone.utc)
        start = NOW
        if condition == 'expired':
            start, end = datetime(2026, 9, 17, 10, tzinfo=timezone.utc), NOW
        periods = (period(start, end),)
        if condition == 'paid_future':
            periods += (period(end, datetime(2026, 12, 17, 10, tzinfo=timezone.utc),
                               id='period-next', experts=('banking',)),)
        values.update(planCode='pro', lifecycleId=LIFECYCLE, periods=periods)
    elif condition == 'annual':
        values.update(planCode='pro', lifecycleId=LIFECYCLE, periods=(period(
            datetime(2026, 1, 31, 10, tzinfo=timezone.utc),
            datetime(2027, 1, 31, 10, tzinfo=timezone.utc), interval='year'),))
    elif condition != 'none':
        raise ValueError(condition)
    return SubscriptionFacts(**values)
