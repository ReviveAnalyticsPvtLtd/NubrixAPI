"""One case per row of the spec 4.4 grant-rule table, checked in the spec order."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from api.services.billing.captureRules import CaptureFacts, attemptClosureTime, classifyCapture

CUTOFF = datetime(2026, 10, 21, tzinfo=timezone.utc)
STARTED = CUTOFF + timedelta(days=1)
GRANTABLE = CaptureFacts(evidenceMatches=True, alreadyFinalized=False, paymentCreatedAt=STARTED,
                         rulesEffectiveAt=CUTOFF, slotSettledByOther=False, ownerClosed=False,
                         attemptClosedAt=None, deliverable=True)
# Every condition that could apply at once; each row only wins when the rows above it pass.
EVERYTHING_WRONG = replace(GRANTABLE, evidenceMatches=False, alreadyFinalized=True,
                           paymentCreatedAt=CUTOFF - timedelta(seconds=1), slotSettledByOther=True,
                           ownerClosed=True, attemptClosedAt=CUTOFF - timedelta(days=1), deliverable=False)


def test_rule_mismatch_attention():
    assert classifyCapture(EVERYTHING_WRONG) == ('attention', None)
    assert classifyCapture(replace(GRANTABLE, evidenceMatches=False)) == ('attention', None)


def test_rule_replay():
    assert classifyCapture(replace(EVERYTHING_WRONG, evidenceMatches=True)) == ('replay', None)


def test_rule_pre_cutoff_legacy():
    facts = replace(EVERYTHING_WRONG, evidenceMatches=True, alreadyFinalized=False)
    assert classifyCapture(facts) == ('legacy', None)
    # The cutover instant itself is already under the new rules.
    assert classifyCapture(replace(GRANTABLE, paymentCreatedAt=CUTOFF)) == ('grant', None)


def test_rule_duplicate_refund():
    facts = replace(EVERYTHING_WRONG, evidenceMatches=True, alreadyFinalized=False, paymentCreatedAt=STARTED)
    assert classifyCapture(facts) == ('refund', 'duplicate')


def test_rule_closed_account_refund():
    facts = replace(EVERYTHING_WRONG, evidenceMatches=True, alreadyFinalized=False, paymentCreatedAt=STARTED,
                    slotSettledByOther=False)
    assert classifyCapture(facts) == ('refund', 'account_closed')


def test_rule_paid_after_close_refund():
    facts = replace(GRANTABLE, attemptClosedAt=STARTED, deliverable=False)
    assert classifyCapture(facts) == ('refund', 'paid_after_close')
    assert classifyCapture(replace(facts, attemptClosedAt=STARTED - timedelta(minutes=1))) == ('refund', 'paid_after_close')


def test_rule_not_deliverable_refund():
    facts = replace(GRANTABLE, attemptClosedAt=STARTED + timedelta(seconds=1), deliverable=False)
    assert classifyCapture(facts) == ('refund', 'not_deliverable')


def test_rule_superseded_first_capture_grants():
    # Superseded after the user had started paying: the first capture in the slot still wins.
    facts = replace(GRANTABLE, attemptClosedAt=STARTED + timedelta(minutes=5))
    assert classifyCapture(facts) == ('grant', None)


def test_rule_ttl_expired_started_in_time_grants():
    expiresAt = STARTED + timedelta(seconds=30)
    sweptAt = expiresAt + timedelta(hours=3)
    closure = attemptClosureTime(expiresAt, sweptAt)
    assert closure == expiresAt  # TTL expiry closes at expiresAt, not when the sweep noticed it.
    assert classifyCapture(replace(GRANTABLE, attemptClosedAt=closure)) == ('grant', None)
    assert classifyCapture(replace(GRANTABLE, paymentCreatedAt=expiresAt, attemptClosedAt=closure)) == (
        'refund', 'paid_after_close')


@pytest.mark.parametrize('expiresAt,closed,expected', [
    (None, (None,), None),
    (STARTED, (None,), STARTED),
    (STARTED, (STARTED - timedelta(minutes=1),), STARTED - timedelta(minutes=1)),
    (None, (STARTED, STARTED + timedelta(seconds=1)), STARTED),
])
def test_closure_is_the_first_moment_the_attempt_stopped_taking_payments(expiresAt, closed, expected):
    assert attemptClosureTime(expiresAt, *closed) == expected
