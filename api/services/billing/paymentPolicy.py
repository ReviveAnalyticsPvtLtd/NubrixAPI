"""Automatic-capture payment policy: Order options, capture window and rules cutoff."""
import os
from datetime import datetime, timedelta, timezone

_DEFAULT_WINDOW_MINUTES = 45
_MIN_WINDOW_MINUTES = 12
_MAX_WINDOW_MINUTES = 7200
_REFUND_SPEEDS = ('normal', 'optimum')


def captureWindowMinutes() -> int:
    raw = os.environ.get('PAYMENT_CAPTURE_WINDOW_MINUTES')
    if raw is None:
        return _DEFAULT_WINDOW_MINUTES
    try:
        minutes = int(raw.strip())
    except (TypeError, ValueError):
        raise ValueError('PAYMENT_CAPTURE_WINDOW_INVALID') from None
    if not _MIN_WINDOW_MINUTES <= minutes <= _MAX_WINDOW_MINUTES:
        raise ValueError('PAYMENT_CAPTURE_WINDOW_INVALID')
    return minutes


def refundSpeed() -> str:
    speed = os.environ.get('PAYMENT_REFUND_SPEED', 'normal').strip()
    if speed not in _REFUND_SPEEDS:
        raise ValueError('PAYMENT_REFUND_SPEED_INVALID')
    return speed


def paymentRulesEffectiveAt() -> datetime:
    """Cutover instant; unset or invalid means no new Order may open."""
    raw = os.environ.get('PAYMENT_RULES_EFFECTIVE_AT', '').strip()
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise RuntimeError('PAYMENT_ACTIONS_UNAVAILABLE') from None
    if value.tzinfo is None:
        raise RuntimeError('PAYMENT_ACTIONS_UNAVAILABLE')
    return value.astimezone(timezone.utc)


def orderPaymentOptions(windowMinutes: int | None = None) -> dict:
    """Order capture options; pass the attempt's reserved window so the Order matches its record."""
    if windowMinutes is None:
        window = captureWindowMinutes()
    elif type(windowMinutes) is not int or not _MIN_WINDOW_MINUTES <= windowMinutes <= _MAX_WINDOW_MINUTES:
        raise ValueError('PAYMENT_CAPTURE_WINDOW_INVALID')
    else:
        window = windowMinutes
    return {'capture': 'automatic', 'capture_options': {
        'automatic_expiry_period': window, 'manual_expiry_period': window, 'refund_speed': refundSpeed()}}


def paymentIsDead(createdAt: datetime, now: datetime) -> bool:
    return now >= createdAt + timedelta(minutes=captureWindowMinutes())
