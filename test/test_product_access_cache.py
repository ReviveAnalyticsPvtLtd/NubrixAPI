"""The plan check on product-data routes: cheap on the hot path, never wrong for long, never a lockout."""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from api.services.subscriptions.entitlementService import EntitlementUnavailableError, SubscriptionEntitlement
from utils.productAccessCache import ProductAccessCache

START = datetime(2026, 11, 5, 10, tzinfo=timezone.utc)
USER = "user-1"


def entitlement(trialOrAbove, periodEnd=None):
    return SubscriptionEntitlement(
        userId=USER, status="active" if trialOrAbove else "expired", planType="pro",
        currentPeriodEnd=periodEnd.isoformat() if periodEnd else None,
        activeSubscription=trialOrAbove, trialOrAbove=trialOrAbove, paidPlan=trialOrAbove,
        topupEligible=trialOrAbove,
    )


class Lookup:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def __call__(self, userId):
        assert userId == USER
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)


@pytest.fixture
def clock():
    return Clock()


def cacheFor(lookup, clock):
    return ProductAccessCache(lookup, now=clock)


def test_access_is_looked_up_once_a_minute(clock):
    lookup = Lookup(entitlement(True))
    cache = cacheFor(lookup, clock)
    assert cache.check(USER) is True
    clock.advance(seconds=59)
    assert cache.check(USER) is True
    assert lookup.calls == 1
    clock.advance(seconds=1)
    assert cache.check(USER) is True
    assert lookup.calls == 2


def test_a_refusal_is_never_remembered(clock):
    # Someone who has just paid must get in on their next request, not a minute later.
    lookup = Lookup(entitlement(False))
    cache = cacheFor(lookup, clock)
    assert cache.check(USER) is False
    lookup.answer = entitlement(True)
    assert cache.check(USER) is True
    assert lookup.calls == 2


def test_a_lapsed_plan_is_refused_on_the_next_lookup(clock):
    lookup = Lookup(entitlement(True))
    cache = cacheFor(lookup, clock)
    cache.check(USER)
    lookup.answer = entitlement(False)
    clock.advance(seconds=60)
    assert cache.check(USER) is False
    clock.advance(seconds=1)
    assert cache.check(USER) is False
    assert lookup.calls == 3


def test_access_is_looked_up_again_when_the_plan_ends(clock):
    lookup = Lookup(entitlement(True, periodEnd=START + timedelta(seconds=10)))
    cache = cacheFor(lookup, clock)
    cache.check(USER)
    clock.advance(seconds=10)
    lookup.answer = entitlement(False)
    assert cache.check(USER) is False
    assert lookup.calls == 2


def test_an_unreadable_plan_keeps_a_recent_yes(clock):
    # A dropped database connection must not lock out someone who had access a minute ago.
    lookup = Lookup(entitlement(True))
    cache = cacheFor(lookup, clock)
    cache.check(USER)
    lookup.answer = EntitlementUnavailableError("database unavailable")
    clock.advance(minutes=14, seconds=59)
    assert cache.check(USER) is True


def test_an_unreadable_plan_with_no_earlier_yes_is_unavailable(clock):
    cache = cacheFor(Lookup(EntitlementUnavailableError("database unavailable")), clock)
    with pytest.raises(EntitlementUnavailableError):
        cache.check(USER)


def test_an_unreadable_plan_after_fifteen_minutes_is_unavailable(clock):
    lookup = Lookup(entitlement(True))
    cache = cacheFor(lookup, clock)
    cache.check(USER)
    lookup.answer = EntitlementUnavailableError("database unavailable")
    clock.advance(minutes=15)
    with pytest.raises(EntitlementUnavailableError):
        cache.check(USER)


def test_an_unreadable_plan_past_its_end_is_unavailable(clock):
    lookup = Lookup(entitlement(True, periodEnd=START + timedelta(minutes=5)))
    cache = cacheFor(lookup, clock)
    cache.check(USER)
    lookup.answer = EntitlementUnavailableError("database unavailable")
    clock.advance(minutes=5)
    with pytest.raises(EntitlementUnavailableError):
        cache.check(USER)


def test_an_unreadable_plan_after_a_refusal_is_unavailable(clock):
    lookup = Lookup(entitlement(True))
    cache = cacheFor(lookup, clock)
    cache.check(USER)
    clock.advance(seconds=60)
    lookup.answer = entitlement(False)
    assert cache.check(USER) is False
    lookup.answer = EntitlementUnavailableError("database unavailable")
    with pytest.raises(EntitlementUnavailableError):
        cache.check(USER)


def test_users_are_remembered_separately(clock):
    answers = {"user-1": entitlement(True), "user-2": entitlement(False)}
    calls = []

    def lookup(userId):
        calls.append(userId)
        return answers[userId]

    cache = ProductAccessCache(lookup, now=clock)
    assert cache.check("user-1") is True
    assert cache.check("user-2") is False
    assert cache.check("user-1") is True
    assert calls == ["user-1", "user-2"]


def test_requests_arriving_together_share_one_lookup(clock):
    # A dashboard asks for every widget at once; one lookup should answer all of them.
    started, release = threading.Event(), threading.Event()
    calls = []

    def lookup(userId):
        calls.append(userId)
        started.set()
        release.wait(5)
        return entitlement(True)

    cache = ProductAccessCache(lookup, now=clock)
    results = []
    threads = [threading.Thread(target=lambda: results.append(cache.check(USER))) for _ in range(8)]
    for thread in threads:
        thread.start()
    started.wait(5)
    release.set()
    for thread in threads:
        thread.join(5)
    assert results == [True] * 8
    assert calls == [USER]


def test_clear_forgets_every_user(clock):
    lookup = Lookup(entitlement(True))
    cache = cacheFor(lookup, clock)
    cache.check(USER)
    cache.clear()
    cache.check(USER)
    assert lookup.calls == 2


def test_the_number_of_remembered_users_is_bounded(clock):
    cache = ProductAccessCache(lambda userId: entitlement(True), now=clock, maxUsers=3)
    for index in range(10):
        cache.check(f"user-{index}")
    assert cache.size() <= 3
