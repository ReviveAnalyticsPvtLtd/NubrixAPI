"""Calendar policy uses the original purchase anchor, with no unpaid refill."""
from datetime import datetime, timedelta, timezone

import pytest
from dateutil.relativedelta import relativedelta


def at(year, month, day):
    return datetime(year, month, day, 10, tzinfo=timezone.utc)


@pytest.mark.parametrize('start', [at(2027, 1, 31), at(2028, 2, 29)])
def test_exactly_twelve_original_anchor_windows(start):
    from api.services.credits.paidAllowancePolicy import annualCreditWindow
    end = start + relativedelta(years=1)
    for index in range(12):
        left = start + relativedelta(months=index)
        right = min(start + relativedelta(months=index + 1), end)
        assert annualCreditWindow(start, end, left) == (index, left, right)
        assert annualCreditWindow(start, end, right - timedelta(microseconds=1)) == (index, left, right)
    assert annualCreditWindow(start, end, start - timedelta(microseconds=1)) is None
    assert annualCreditWindow(start, end, end) is None


def test_january_clamp_recovers_march_31_and_revocation_clips_window():
    from api.services.credits.paidAllowancePolicy import annualCreditWindow
    start = at(2027, 1, 31)
    assert annualCreditWindow(start, at(2028, 1, 31), at(2027, 2, 28)) == (1, at(2027, 2, 28), at(2027, 3, 31))
    assert annualCreditWindow(start, at(2027, 3, 15), at(2027, 3, 1)) == (1, at(2027, 2, 28), at(2027, 3, 15))


def test_policy_rejects_naive_or_invalid_service_duration():
    from api.services.credits.paidAllowancePolicy import annualCreditWindow
    with pytest.raises(ValueError):
        annualCreditWindow(at(2027, 1, 31).replace(tzinfo=None), at(2028, 1, 31), at(2027, 2, 28))
    with pytest.raises(ValueError):
        annualCreditWindow(at(2027, 1, 31), at(2028, 2, 1), at(2027, 2, 28))


def test_trial_and_paid_allowances_preserve_quantities_and_legacy_read_aliases():
    from api.services.credits.creditConfig import getAllowanceQuota, getTokenQuotaForPlan
    assert getAllowanceQuota('trial', 4) == 3_000_000
    for count in range(1, 5):
        assert getAllowanceQuota('paid', count) == count * 10_000_000
        assert getTokenQuotaForPlan('annual', count) == getTokenQuotaForPlan('pro', count) == getAllowanceQuota('paid', count)
    assert getTokenQuotaForPlan('free', 4) == getAllowanceQuota('trial', 4)
    for kind, count in [('paid', 0), ('paid', 5), ('paid', True), ('other', 1)]:
        with pytest.raises(ValueError):
            getAllowanceQuota(kind, count)


def test_period_engine_uses_the_same_window_and_preserves_admin_generation():
    from api.services.billing.manualBillingContracts import CoveragePeriod
    from api.services.credits.creditPeriodEngine import CreditPeriodEngine
    engine = CreditPeriodEngine()
    period = CoveragePeriod('user', 'sub', 'life', 'base', 'invoice', at(2027, 1, 31), at(2028, 1, 31), ('banking',), 'annual_prepaid', None)
    assert not engine.ensureCurrentAllocation('user', period, at(2027, 1, 30), 10_000_000)['allocated']
    first = engine.ensureCurrentAllocation('user', period, at(2027, 2, 28), 10_000_000)
    assert first['row']['period_end'] == at(2027, 3, 31).isoformat()
    engine.store.upsertBalance('user', {'credit_period_id': 'admin-generation', 'used_tokens': 500, 'remaining_tokens': 9_999_500})
    repeated = engine.ensureCurrentAllocation('user', period, at(2027, 3, 1), 10_000_000)
    assert not repeated['allocated'] and repeated['row']['credit_period_id'] == 'admin-generation'
    assert repeated['row']['remaining_tokens'] == 9_999_500
    assert not engine.ensureCurrentAllocation('user', period, at(2028, 1, 31), 10_000_000)['allocated']
