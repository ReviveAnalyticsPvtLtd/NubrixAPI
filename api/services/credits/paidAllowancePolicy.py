"""Purchased service and its included allowance have separate, UTC clocks."""
from dataclasses import replace
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from dateutil.relativedelta import relativedelta


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('ALLOWANCE_REQUIRES_AWARE_TIMESTAMP')
    return value.astimezone(timezone.utc)


def annualCreditWindow(serviceStart: datetime, serviceEnd: datetime, now: datetime) -> tuple[int, datetime, datetime] | None:
    start, end, evaluated = map(_aware, (serviceStart, serviceEnd, now))
    if not start < end <= start + relativedelta(years=1):
        raise ValueError('INVALID_ANNUAL_ALLOWANCE_DURATION')
    if not start <= evaluated < end:
        return None
    for index in range(12):
        left = start + relativedelta(months=index)
        right = min(start + relativedelta(months=index + 1), end)
        if left <= evaluated < right:
            return index, left, right
    return None


def paidCreditWindow(period, now):
    """Return the current allowance period, never a future or expired allocation.

    Window zero retains the original capture identity. Later annual windows
    have deterministic identities; an admin reset may rotate the generation
    within those same boundaries without the natural window refilling again.
    """
    start, end, evaluated = map(_aware, (period.start, period.end, now))
    if period.revokedAt is not None:
        end = min(end, _aware(period.revokedAt))
    if not start <= evaluated < end:
        return None
    if period.billingMode == 'annual_prepaid':
        index, left, right = annualCreditWindow(start, end, evaluated)
    elif period.billingMode == 'monthly_prepaid':
        index, left, right = 0, start, end
    else:
        raise ValueError('INVALID_PAID_ALLOWANCE_MODE')
    identity = period.creditPeriodId if index == 0 else str(uuid5(NAMESPACE_URL,
        f'nubrix:paid-allowance:{period.subscriptionId}:{period.lifecycleId}:{period.invoiceId}:{index}'))
    if not identity:
        raise ValueError('PAID_ALLOWANCE_IDENTITY_MISSING')
    return replace(period, start=left, end=right, creditPeriodId=identity)


def matchesPaidAllocation(balance, window):
    return bool(balance and window and balance.get('credit_period_id')
        and str(balance.get('subscription_id')) == window.subscriptionId
        and str(balance.get('lifecycle_id')) == window.lifecycleId
        and balance.get('period_start') == window.start and balance.get('period_end') == window.end)
