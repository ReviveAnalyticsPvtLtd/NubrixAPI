"""Consistent read boundary for verified facts and their database clock."""
from collections.abc import Callable
from datetime import datetime

from api.services.subscriptions.subscriptionModel import (
    SubscriptionAuthorityUnavailable, SubscriptionFacts, SubscriptionSnapshotV2,
    evaluateSubscription,
)

# Task 2's repository adapter loads facts + database now inside one consistent
# owner/canonical transaction. No wall-clock fallback or repository stub here.
FactsLoader = Callable[[str, datetime | None], tuple[SubscriptionFacts, datetime]]


class SubscriptionSnapshotService:
    def __init__(self, factsLoader: FactsLoader):
        self._factsLoader = factsLoader

    def get(self, userId: str, now: datetime | None = None) -> SubscriptionSnapshotV2:
        if not isinstance(userId, str) or not userId.strip() or userId != userId.strip():
            raise SubscriptionAuthorityUnavailable('Subscription owner required')
        try:
            facts, evaluatedAt = self._factsLoader(userId, now)
        except SubscriptionAuthorityUnavailable:
            raise
        except Exception as exc:
            raise SubscriptionAuthorityUnavailable('Subscription facts unavailable') from exc
        if not isinstance(facts, SubscriptionFacts) or facts.userId != userId:
            raise SubscriptionAuthorityUnavailable('Subscription owner/evidence mismatch')
        if now is not None and evaluatedAt != now:
            raise SubscriptionAuthorityUnavailable('Inconsistent explicit evaluation time')
        return evaluateSubscription(facts, evaluatedAt)
