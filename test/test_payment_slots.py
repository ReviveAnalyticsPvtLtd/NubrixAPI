"""Pure Pay decision and purchase slot keys."""
from datetime import datetime, timedelta, timezone

import pytest

from api.services.billing.manualBillingContracts import CheckoutProviderSnapshot
from api.services.billing.paymentSlots import LiveAttempt, PayDecision, decidePay, slotKey, slotKind

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def window(monkeypatch):
    monkeypatch.setenv('PAYMENT_CAPTURE_WINDOW_MINUTES', '45')


def live(expiresIn=600, order='order_1', hash_='h1'):
    return LiveAttempt('att_1', hash_, NOW + timedelta(seconds=expiresIn), order, 1)


def payment(status, ageMinutes=1):
    return {'id': 'pay_' + status, 'status': status, 'amount': 100,
            'created_at': int((NOW - timedelta(minutes=ageMinutes)).timestamp())}


def snap(payments=(), observedAgo=5, complete=True, outcome='unpaid', attemptId='att_1', order='order_1'):
    return CheckoutProviderSnapshot(attemptId, order, outcome, NOW - timedelta(seconds=observedAgo),
                                    complete, 100, 'INR', tuple(payments))


def test_no_live_attempt_opens():
    assert decidePay(None, 'h1', None, NOW) == PayDecision('open', None)


def test_same_hash_inside_ttl_without_capture_reuses():
    assert decidePay(live(), 'h1', snap(), NOW) == PayDecision('reuse', 'att_1')


def test_unbound_order_same_hash_reuses_without_snapshot():
    assert decidePay(live(order=None), 'h1', None, NOW) == PayDecision('reuse', 'att_1')


def test_unbound_order_changed_hash_or_lapsed_is_busy():
    assert decidePay(live(order=None), 'h2', None, NOW).action == 'busy'
    assert decidePay(live(expiresIn=-1, order=None), 'h1', None, NOW).action == 'busy'


@pytest.mark.parametrize('payments', [(), (payment('failed'),), (payment('created'),)])
def test_changed_hash_with_empty_or_failed_or_created_listing_replaces(payments):
    assert decidePay(live(), 'h2', snap(payments), NOW) == PayDecision('replace', 'att_1')


def test_lapsed_ttl_same_hash_replaces():
    assert decidePay(live(expiresIn=-1), 'h1', snap(), NOW) == PayDecision('replace', 'att_1')
    assert decidePay(live(expiresIn=0), 'h1', snap(), NOW).action == 'replace'


@pytest.mark.parametrize('hash_', ['h1', 'h2'])
def test_authorized_payment_alive_confirms(hash_):
    assert decidePay(live(), hash_, snap([payment('authorized', 44)]), NOW) == PayDecision('confirming', 'att_1')


def test_authorized_payment_past_window_replaces():
    assert decidePay(live(), 'h2', snap([payment('authorized', 45)]), NOW) == PayDecision('replace', 'att_1')
    assert decidePay(live(), 'h1', snap([payment('authorized', 45)]), NOW) == PayDecision('replace', 'att_1')
    mixed = snap([payment('authorized', 45), payment('authorized', 1)])
    assert decidePay(live(), 'h1', mixed, NOW).action == 'confirming'


def test_captured_payment_finalizes_first():
    got = decidePay(live(), 'h2', snap([payment('authorized'), payment('captured')]), NOW)
    assert got == PayDecision('finalize_first', 'att_1')


@pytest.mark.parametrize('kwargs', [{'observedAgo': 61}, {'observedAgo': -5}, {'complete': False}])
def test_stale_or_incomplete_snapshot_busy(kwargs):
    assert decidePay(live(), 'h1', snap(**kwargs), NOW) == PayDecision('busy', 'att_1')


def test_missing_snapshot_for_bound_order_busy_and_boundary_fresh():
    assert decidePay(live(), 'h1', None, NOW).action == 'busy'
    assert decidePay(live(), 'h1', snap(observedAgo=60), NOW).action == 'reuse'


def test_slot_kinds():
    assert slotKind('initial_purchase') == slotKind('renewal') == 'pro_term'
    assert slotKind('expert_addition') == 'expert_addition'
    assert slotKind('topup') == 'topup'
    with pytest.raises(ValueError, match='PAYMENT_SLOT_UNKNOWN_PURPOSE'):
        slotKind('gift')


def test_slot_keys_share_pro_term_for_repurchase_and_ended_renewal():
    end = datetime(2026, 12, 1, tzinfo=timezone.utc)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert slotKey('initial_purchase', currentTermEnd=None, renewalPeriodStart=None) == 'open'
    assert slotKey('renewal', currentTermEnd=None, renewalPeriodStart=start) == 'open'
    assert slotKey('renewal', currentTermEnd=end, renewalPeriodStart=end) == end.isoformat()
    assert slotKey('expert_addition', currentTermEnd=end, renewalPeriodStart=None) == 'current:' + end.isoformat()
    assert slotKey('topup', currentTermEnd=None, renewalPeriodStart=None) == 'topup'


def test_slot_key_errors():
    with pytest.raises(ValueError, match='PAYMENT_SLOT_UNKNOWN_PURPOSE'):
        slotKey('gift', currentTermEnd=None, renewalPeriodStart=None)
    with pytest.raises(ValueError, match='PAYMENT_SLOT_KEY_INVALID'):
        slotKey('expert_addition', currentTermEnd=None, renewalPeriodStart=None)
    with pytest.raises(ValueError, match='PAYMENT_SLOT_KEY_INVALID'):
        slotKey('renewal', currentTermEnd=None, renewalPeriodStart=None)


def test_refunded_payment_or_paid_order_finalizes_first():
    assert decidePay(live(), 'h1', snap([payment('refunded')], outcome='unknown'), NOW) == PayDecision('finalize_first', 'att_1')
    assert decidePay(live(), 'h2', snap([payment('refunded'), payment('failed')]), NOW).action == 'finalize_first'
    assert decidePay(live(), 'h1', snap(outcome='captured'), NOW) == PayDecision('finalize_first', 'att_1')


def test_unknown_outcome_is_busy():
    assert decidePay(live(), 'h1', snap(outcome='unknown'), NOW).action == 'busy'


@pytest.mark.parametrize('created', [None, 'x', True])
def test_authorized_payment_with_bad_created_at_busy(created):
    bad = {'id': 'p', 'status': 'authorized', 'created_at': created}
    assert decidePay(live(), 'h1', snap([bad]), NOW) == PayDecision('busy', 'att_1')
    missing = {'id': 'p', 'status': 'authorized'}
    assert decidePay(live(), 'h1', snap([missing]), NOW).action == 'busy'


def test_snapshot_for_other_attempt_or_order_busy():
    assert decidePay(live(), 'h1', snap(attemptId='att_2'), NOW).action == 'busy'
    assert decidePay(live(), 'h1', snap(order='order_2'), NOW).action == 'busy'


def test_slot_key_naive_datetime_rejected_and_renewal_ignores_period_start_with_term():
    naive = datetime(2026, 12, 1)
    with pytest.raises(ValueError, match='PAYMENT_SLOT_KEY_INVALID'):
        slotKey('expert_addition', currentTermEnd=naive, renewalPeriodStart=None)
    with pytest.raises(ValueError, match='PAYMENT_SLOT_KEY_INVALID'):
        slotKey('renewal', currentTermEnd=None, renewalPeriodStart=naive)
    end = datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert slotKey('renewal', currentTermEnd=end, renewalPeriodStart=None) == end.isoformat()


def test_window_comes_from_the_attempt_snapshot():
    held = snap([payment('authorized', 50)])
    assert decidePay(live(), 'h2', held, NOW).action == 'replace'
    assert decidePay(live(), 'h2', held, NOW, windowMinutes=60) == PayDecision('confirming', 'att_1')
    assert decidePay(live(), 'h2', snap([payment('authorized', 44)]), NOW, windowMinutes=12).action == 'replace'


@pytest.mark.parametrize('payments,hash_,expected', [
    ([payment('refunded'), payment('authorized')], 'h2', 'confirming'),
    ([payment('refunded'), {'id': 'p', 'status': 'authorized'}], 'h2', 'busy'),
    ([payment('refunded'), payment('unclassified')], 'h2', 'busy'),
    ([payment('captured'), payment('authorized', 50)], 'h2', 'replace'),
    ([payment('captured'), payment('failed'), payment('created')], 'h2', 'replace'),
    # An Order that took money never takes another payment: same selection is still replaced.
    ([payment('refunded')], 'h1', 'replace'),
])
def test_after_finalizing_the_remaining_payments_decide_again(payments, hash_, expected):
    from api.services.billing.paymentSlots import decideAfterFinalize
    snapshot = snap(payments, outcome='unknown')
    assert decidePay(live(), hash_, snapshot, NOW).action == 'finalize_first'
    assert decideAfterFinalize(live(), hash_, snapshot, NOW) == PayDecision(expected, 'att_1')
