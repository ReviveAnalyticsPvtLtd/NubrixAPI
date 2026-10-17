"""Boundary regressions for the pure model, independent of jobs and old labels."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone

import pytest

from api.services.subscriptions.subscriptionModel import (
    RecoverableCheckout, SubscriptionAuthorityUnavailable, TrialFacts,
    evaluateSubscription,
)
from test.subscriptionModelFixtures import NOW, factsFor, period


@pytest.mark.parametrize('condition,status,source,can_buy,interval', [
    ('none', 'none', 'none', True, None),
    ('trial', 'trial', 'trial', True, None),
    ('monthly', 'active', 'paid', False, 'month'),
    ('annual', 'active', 'paid', False, 'year'),
    ('expired', 'expired', 'none', True, 'month'),
])
def test_status_and_purchase_follow_verified_time_not_product_label(condition, status, source, can_buy, interval):
    snapshot = evaluateSubscription(factsFor(condition), NOW)
    assert (snapshot.status, snapshot.accessSource, snapshot.purchaseAllowed,
            snapshot.billingInterval) == (status, source, can_buy, interval)
    assert snapshot.accessAllowed is (source != 'none')


def test_exact_paid_end_allows_new_purchase_despite_old_product_and_opt_out():
    facts = replace(factsFor('expired'), renewalOptOut=True)
    snapshot = evaluateSubscription(facts, NOW)
    assert (snapshot.status, snapshot.accessAllowed, snapshot.purchaseAllowed) == ('expired', False, True)
    assert snapshot.planCode == 'pro'
    assert snapshot.currentPeriod is None and snapshot.paidUntil is None
    assert snapshot.lastPaidEnd == NOW
    assert snapshot.subscribedExperts == () and snapshot.domainCount == 0
    assert not snapshot.renewalAvailable and not snapshot.resumeRenewalAvailable
    assert snapshot.cancellationEffectiveAt is None


def test_paid_start_inclusive_and_end_exclusive_to_microsecond():
    facts = factsFor('monthly')
    assert evaluateSubscription(facts, NOW).accessAllowed
    end = facts.periods[0].end
    assert evaluateSubscription(facts, end - timedelta(microseconds=1)).accessAllowed
    assert not evaluateSubscription(facts, end).accessAllowed


def test_paid_future_extends_access_without_changing_current_experts_or_credit_identity():
    facts = factsFor('paid_future')
    snapshot = evaluateSubscription(facts, NOW)
    assert snapshot.paidUntil == datetime(2026, 12, 17, 10, tzinfo=timezone.utc)
    assert snapshot.subscribedExperts == ('banking', 'telecom')
    assert snapshot.nextPeriod.experts == ('banking',)
    assert snapshot.currentPeriod.creditPeriodId is None  # Pure reads cannot invent an allocation.
    assert not snapshot.renewalAvailable
    boundary = evaluateSubscription(facts, facts.periods[0].end)
    assert boundary.status == 'active' and boundary.currentPeriod.id == 'period-next'
    assert boundary.nextPeriod is None and boundary.subscribedExperts == ('banking',)


@pytest.mark.parametrize('condition', ['monthly', 'annual', 'paid_future'])
def test_opt_out_preserves_all_purchased_time_and_only_changes_preference_permissions(condition):
    facts = factsFor(condition)
    before = evaluateSubscription(facts, NOW)
    after = evaluateSubscription(replace(facts, renewalOptOut=True), NOW)
    assert after.status == 'active' and after.accessAllowed
    assert (after.currentPeriod, after.nextPeriod, after.paidUntil) == (
        before.currentPeriod, before.nextPeriod, before.paidUntil)
    assert after.resumeRenewalAvailable and not after.renewalAvailable
    assert after.cancellationEffectiveAt == before.paidUntil
    assert before.renewalAvailable is (condition != 'paid_future')


def test_trial_ends_exactly_and_is_never_available_again():
    facts = factsFor('trial')
    assert not evaluateSubscription(facts, NOW).trialAvailable
    snapshot = evaluateSubscription(facts, facts.trial.endsAt)
    assert snapshot.status == 'expired' and snapshot.purchaseAllowed
    assert not snapshot.accessAllowed and not snapshot.trialAvailable


def test_closed_or_historically_consumed_trial_cannot_return():
    facts = factsFor('trial')
    closed = replace(facts, trial=replace(facts.trial, closedAt=NOW))
    assert evaluateSubscription(closed, NOW).status == 'expired'
    unknown_dates = replace(factsFor('none'), planCode='pro', trial=TrialFacts(consumed=True))
    snapshot = evaluateSubscription(unknown_dates, NOW)
    assert snapshot.status == 'expired' and not snapshot.trialAvailable
    assert snapshot.trial is None and snapshot.purchaseAllowed


def test_paid_history_prevents_trial_resurrection_even_if_old_trial_close_write_was_missing():
    trial = replace(factsFor('trial').trial, endsAt=NOW + timedelta(days=60))
    facts = replace(factsFor('monthly'), trial=trial)
    assert evaluateSubscription(facts, NOW).accessSource == 'paid'
    elapsed = evaluateSubscription(facts, facts.periods[0].end)
    assert elapsed.status == 'expired' and elapsed.accessSource == 'none'
    assert not elapsed.trialAvailable


@pytest.mark.parametrize('restriction,reason', [('accountBanned', 'account_banned'), ('erasurePending', 'erasure_pending')])
@pytest.mark.parametrize('condition', ['none', 'trial', 'monthly', 'expired'])
def test_account_restrictions_do_not_relabel_paid_or_trial_history(restriction, reason, condition):
    facts = factsFor(condition)
    ordinary = evaluateSubscription(facts, NOW)
    blocked = evaluateSubscription(replace(facts, **{restriction: True}), NOW)
    assert blocked.status == ordinary.status and blocked.accessSource == ordinary.accessSource
    assert not blocked.accessAllowed and not blocked.purchaseAllowed and not blocked.trialAvailable
    assert blocked.denialReason == reason and blocked.purchaseBlockReason == 'account_restricted'
    assert not blocked.renewalAvailable and not blocked.resumeRenewalAvailable


def test_unknown_money_requires_recovery_without_changing_valid_access():
    recovery = RecoverableCheckout('invoice-old', 'attempt-old', 'processing')
    for condition in ('none', 'trial', 'monthly', 'expired'):
        facts = replace(factsFor(condition), moneyUnresolved=True, recoverableCheckout=recovery)
        snapshot = evaluateSubscription(facts, NOW)
        assert snapshot.status == evaluateSubscription(factsFor(condition), NOW).status
        assert not snapshot.purchaseAllowed and not snapshot.renewalAvailable
        assert snapshot.purchaseBlockReason == 'recovery_required'
        assert snapshot.recoverableCheckout == recovery


def test_unresolved_money_without_a_recoverable_browser_attempt_still_blocks_purchase():
    snapshot = evaluateSubscription(replace(factsFor('expired'), moneyUnresolved=True), NOW)
    assert not snapshot.purchaseAllowed and snapshot.purchaseBlockReason == 'recovery_required'


@pytest.mark.parametrize('mutate', [
    lambda f: replace(f, evidenceVerified=False),
    lambda f: replace(f, periods=(replace(f.periods[0], userId='different-owner'),)),
    lambda f: replace(f, periods=(replace(f.periods[0], subscriptionId='different-subscription'),)),
    lambda f: replace(f, lifecycleId='different-current-lifecycle'),
    lambda f: replace(f, periods=(replace(f.periods[0], end=f.periods[0].start),)),
    lambda f: replace(f, periods=(replace(f.periods[0], start=NOW.replace(tzinfo=None)),)),
    lambda f: replace(f, periods=(replace(f.periods[0], billingInterval='annual'),)),
    lambda f: replace(f, periods=(replace(f.periods[0], experts=()),)),
    lambda f: replace(f, periods=(replace(f.periods[0], experts=('banking', 'banking')),)),
    lambda f: replace(f, periods=f.periods + (replace(f.periods[0], id='overlap'),)),
])
def test_unproven_or_ambiguous_authority_never_becomes_active_or_none(mutate):
    with pytest.raises(SubscriptionAuthorityUnavailable):
        evaluateSubscription(mutate(factsFor('monthly')), NOW)


def test_gapped_or_multiple_future_paid_slots_require_review():
    facts = factsFor('paid_future')
    bad = replace(facts.periods[1], start=facts.periods[1].start + timedelta(days=1))
    with pytest.raises(SubscriptionAuthorityUnavailable):
        evaluateSubscription(replace(facts, periods=(facts.periods[0], bad)), NOW)
    third = period(facts.periods[1].end, datetime(2027, 1, 17, 10, tzinfo=timezone.utc), id='third')
    with pytest.raises(SubscriptionAuthorityUnavailable):
        evaluateSubscription(replace(facts, periods=facts.periods + (third,)), NOW)


def test_a_future_revocation_before_service_start_cannot_emit_a_negative_period():
    facts = factsFor('paid_future')
    invalid = replace(facts.periods[1], revokedAt=NOW + timedelta(days=1))
    with pytest.raises(SubscriptionAuthorityUnavailable):
        evaluateSubscription(replace(facts, periods=(facts.periods[0], invalid)), NOW)


def test_revoked_service_does_not_grant_access_or_hide_consumed_history():
    facts = factsFor('monthly')
    facts = replace(facts, periods=(replace(facts.periods[0], revokedAt=NOW),))
    snapshot = evaluateSubscription(facts, NOW)
    assert snapshot.status == 'expired' and not snapshot.accessAllowed
    assert snapshot.purchaseAllowed and not snapshot.trialAvailable


def test_revoked_future_purchase_cannot_override_the_current_billing_interval():
    facts = factsFor('monthly')
    revoked = period(facts.periods[0].end, datetime(2027, 11, 17, 10, tzinfo=timezone.utc),
                     id='revoked-year', interval='year', revokedAt=NOW)
    snapshot = evaluateSubscription(replace(facts, periods=facts.periods + (revoked,)), NOW)
    assert snapshot.status == 'active' and snapshot.billingInterval == 'month'
    assert snapshot.nextPeriod is None
    assert snapshot.paidUntil == datetime(2026, 11, 17, 10, tzinfo=timezone.utc)


@pytest.mark.parametrize('trial', [
    TrialFacts(endsAt=NOW),
    TrialFacts(closedAt=NOW),
    TrialFacts(startedAt=NOW, endsAt=NOW, experts=('banking',)),
    TrialFacts(startedAt=NOW, endsAt=NOW + timedelta(days=1)),
])
def test_incomplete_trial_evidence_is_unavailable_instead_of_fresh_trial_eligibility(trial):
    with pytest.raises(SubscriptionAuthorityUnavailable):
        evaluateSubscription(replace(factsFor('none'), trial=trial), NOW)


def test_aware_non_utc_evaluation_normalizes_and_naive_time_is_unavailable():
    local = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert evaluateSubscription(factsFor('monthly'), local).evaluatedAt == NOW
    with pytest.raises(SubscriptionAuthorityUnavailable):
        evaluateSubscription(factsFor('monthly'), NOW.replace(tzinfo=None))


def test_evaluation_is_immutable_and_does_not_close_trial_or_change_period_facts():
    facts = replace(factsFor('monthly'), trial=factsFor('trial').trial)
    snapshot = evaluateSubscription(facts, NOW)
    assert facts.trial.closedAt is None and facts.periods[0].creditPeriodId is None
    with pytest.raises(FrozenInstanceError):
        snapshot.status = 'expired'
