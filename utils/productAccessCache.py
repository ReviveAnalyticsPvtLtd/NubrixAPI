"""Remembers recent "has access" answers so product-data routes don't read the plan on every request.

A dashboard loads many widgets at once, and each plan lookup is its own database transaction, so the
plan check on those routes keeps a short memory per server worker:

- A "yes" is reused for one minute, and never past the end of the plan period it saw.
- A "no" is never reused, so someone who has just paid gets in on their next request.
- When the plan can't be read, a "yes" from the last fifteen minutes still counts, unless the plan
  period it saw has ended. A dropped database connection then can't lock out someone who had access.
- Requests for the same user that arrive together share one lookup.
"""

__all__ = ["ProductAccessCache"]

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from api.services.subscriptions.entitlementService import EntitlementUnavailableError, SubscriptionEntitlement
from utils.logger import logger

FRESH_FOR = timedelta(minutes=1)
GRACE_FOR = timedelta(minutes=15)
_LOCK_STRIPES = 64


@dataclass(frozen=True)
class _Allowed:
    checkedAt: datetime
    periodEnd: datetime | None

    def validAt(self, now: datetime, within: timedelta) -> bool:
        return now - self.checkedAt < within and (self.periodEnd is None or now < self.periodEnd)


def _periodEnd(entitlement: SubscriptionEntitlement) -> datetime | None:
    value = entitlement.currentPeriodEnd
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class ProductAccessCache:
    def __init__(
        self,
        lookup: Callable[[str], SubscriptionEntitlement],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        maxUsers: int = 10_000,
    ) -> None:
        self._lookup = lookup
        self._now = now
        self._maxUsers = maxUsers
        self._allowed: OrderedDict[str, _Allowed] = OrderedDict()
        self._guard = threading.Lock()
        self._stripes = [threading.Lock() for _ in range(_LOCK_STRIPES)]

    def check(self, userId: str) -> bool:
        """True when the user has a plan or trial. Raises EntitlementUnavailableError when that
        can't be told."""
        if self._valid(userId, FRESH_FOR):
            return True
        with self._stripes[hash(userId) % _LOCK_STRIPES]:
            if self._valid(userId, FRESH_FOR):
                return True
            try:
                entitlement = self._lookup(userId)
            except EntitlementUnavailableError:
                if self._valid(userId, GRACE_FOR):
                    logger.warning("Plan lookup unavailable for userId={}; using its check from the last {} minutes",
                                   userId, int(GRACE_FOR.total_seconds() // 60))
                    return True
                raise
            if not entitlement.trialOrAbove:
                self._forget(userId)
                return False
            self._remember(userId, _Allowed(self._now(), _periodEnd(entitlement)))
            return True

    def clear(self) -> None:
        with self._guard:
            self._allowed.clear()

    def size(self) -> int:
        with self._guard:
            return len(self._allowed)

    def _valid(self, userId: str, within: timedelta) -> bool:
        with self._guard:
            entry = self._allowed.get(userId)
        return entry is not None and entry.validAt(self._now(), within)

    def _remember(self, userId: str, entry: _Allowed) -> None:
        with self._guard:
            self._allowed[userId] = entry
            self._allowed.move_to_end(userId)
            while len(self._allowed) > self._maxUsers:
                self._allowed.popitem(last=False)

    def _forget(self, userId: str) -> None:
        with self._guard:
            self._allowed.pop(userId, None)
