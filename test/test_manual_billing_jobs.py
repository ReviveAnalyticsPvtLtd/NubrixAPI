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


def test_celery_beat_has_no_auto_debit_entry():
    import importlib

    for module in [m for m in list(sys.modules) if m.startswith("nubrix")]:
        del sys.modules[module]
    import nubrix.triggers.celery as celeryModule

    schedule = celeryModule.celeryApp.conf.beat_schedule
    serialized = str(schedule).lower()
    assert "dailybilling" not in serialized, "auto-debit beat entry must be gone"


def test_celery_registers_monthly_renewal_task_hourly():
    import importlib

    for module in [m for m in list(sys.modules) if m.startswith("nubrix")]:
        del sys.modules[module]
    import nubrix.triggers.celery as celeryModule

    schedule = celeryModule.celeryApp.conf.beat_schedule
    monthlyEntries = {
        key: value for key, value in schedule.items() if "monthly" in key.lower()
    }
    assert monthlyEntries, "monthly preparation/milestone sweep must be scheduled"
    hourly = [
        value
        for value in monthlyEntries.values()
        if "0 * * * *" in str(value.get("schedule", ""))
    ]
    assert hourly, "monthly sweep must run hourly"


def test_monthly_renewal_task_class_exists():
    from nubrix.triggers.tasks.monthlyRenewalTask import MonthlyRenewalTask

    assert callable(MonthlyRenewalTask)


def test_past_due_suspension_keeps_annual_only():
    source = Path("nubrix/triggers/tasks/pastDueSuspensionTask.py").read_text(
        encoding="utf-8"
    )
    # Past-due/suspension sweeps select only annual_prepaid rows: no monthly
    # grace, dunning or buffer exists under manual billing.
    assert source.count('.eq("billing_mode", "annual_prepaid")') >= 2
    assert 'eq("billing_mode", "monthly' not in source