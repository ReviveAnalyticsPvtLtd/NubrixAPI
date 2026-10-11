"""Job wiring tests: monthly task registered, debit task retired, celery beat."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_billing_task_module_is_removed():
    assert not Path("nubrix/triggers/tasks/billingTask.py").exists(), (
        "DailyBillingTask (auto-debit engine) must be removed after "
        "automatic charging is disabled"
    )


def _celerySource() -> str:
    return Path("nubrix/triggers/celery.py").read_text(encoding="utf-8")


def test_celery_has_no_auto_debit_task_or_beat_entry():
    source = _celerySource()
    lowered = source.lower()
    assert "dailybilling" not in lowered, "auto-debit task must be unregistered"
    assert "billingtask" not in lowered, "auto-debit import must be removed"


def test_celery_registers_monthly_renewal_task_hourly():
    source = _celerySource()
    assert "monthlyRenewal" in source, "monthly task must be registered"
    assert "from nubrix.triggers.tasks.monthlyRenewalTask import MonthlyRenewalTask" in source
    # hourly beat entry at minute 0
    assert '"monthly-renewal-hourly"' in source
    assert 'crontab(minute=0)' in source


def test_monthly_renewal_task_class_exists():
    from nubrix.triggers.tasks.monthlyRenewalTask import MonthlyRenewalTask

    assert callable(MonthlyRenewalTask)


def test_monthly_renewal_task_has_no_charge_calls():
    source = Path("nubrix/triggers/tasks/monthlyRenewalTask.py").read_text(
        encoding="utf-8"
    )
    lowered = source.lower()
    for forbidden in (
        "createrecurring",
        "payment.create",
        "token.fetch",
        "customer.create",
        "razorpayclient.order.create",
    ):
        assert forbidden not in lowered, (
            f"monthlyRenewalTask must not attempt provider charges ({forbidden})"
        )


def test_past_due_suspension_keeps_annual_only():
    source = Path("nubrix/triggers/tasks/pastDueSuspensionTask.py").read_text(
        encoding="utf-8"
    )
    # Past-due/suspension sweeps select only annual_prepaid rows: no monthly
    # grace, dunning or buffer exists under manual billing.
    assert source.count('.eq("billing_mode", "annual_prepaid")') >= 2
    assert 'eq("billing_mode", "monthly' not in source