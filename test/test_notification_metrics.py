import datetime


NOW = datetime.datetime(2026, 9, 17, 12, 0, tzinfo=datetime.timezone.utc)


class _NotificationRepository:
    def __init__(self, health):
        self.health = health
        self.now = None

    def collectHealth(self, now):
        self.now = now
        return dict(self.health)


def _health(**overrides):
    return {
        "pending": 2,
        "retryPending": 1,
        "acceptedUnresolved": 1,
        "expiredLeases": 1,
        "delivered": 8,
        "terminalFailures": 2,
        "oldestPendingMinutes": 20,
        **overrides,
    }


def _service(
    *,
    health=None,
    lastSweep="2026-09-17T01:00:00+00:00",
    oldestAcceptedHours=7.0,
):
    from api.services.billing.billingMetricsService import BillingMetricsService

    class TestMetricsService(BillingMetricsService):
        def _collectRecurringMetrics(self, _windowStart):
            return {"queued": 0, "captured": 0, "failed": 0}

        def _collectThresholdRedirects(self, _windowStart):
            return {"count": 0}

        def _collectTokenPrecheckFailures(self, _windowStart):
            return {"count": 0}

        def _collectReconciliationMetrics(self):
            return {"unresolvedAttempts": 0}

        def _collectWebhookBacklog(self):
            return {"count": 0}

        def _collectExpirySweepHeartbeat(self):
            return {"lastCompletedAt": lastSweep}

        def _collectOldestAcceptedAgeHours(self, _now):
            return oldestAcceptedHours

        def _persistAlerts(self, alerts):
            self.persistedAlerts = list(alerts)

    repository = _NotificationRepository(health or _health())
    service = TestMetricsService(
        client=object(),
        notificationRepository=repository,
        now=lambda: NOW,
    )
    service.persistedAlerts = []
    return service, repository


def test_notification_health_is_included_in_metrics():
    service, repository = _service()

    metrics = service.collectMetrics()

    assert metrics["notificationDelivery"] == _health()
    assert metrics["expirySweep"] == {
        "lastCompletedAt": "2026-09-17T01:00:00+00:00"
    }
    assert repository.now == NOW.isoformat()


def test_stale_backlog_and_unresolved_acceptance_trigger_alerts():
    service, _repository = _service()

    alertTypes = {alert["alertType"] for alert in service.evaluateAlerts()}

    assert "notification_backlog_stale" in alertTypes
    assert "notification_delivery_unresolved" in alertTypes


def test_missing_expiry_sweep_heartbeat_triggers_alert():
    service, _repository = _service(
        health=_health(
            pending=0,
            retryPending=0,
            acceptedUnresolved=0,
            expiredLeases=0,
            oldestPendingMinutes=0,
            terminalFailures=0,
        ),
        lastSweep=None,
        oldestAcceptedHours=0,
    )

    alerts = service.evaluateAlerts()

    assert {alert["alertType"] for alert in alerts} == {
        "expiry_sweep_stale"
    }


def test_notification_terminal_failure_rate_uses_terminal_outcomes(
    monkeypatch,
):
    import api.services.billing.billingMetricsService as metricsModule

    monkeypatch.setattr(
        metricsModule,
        "_NOTIFICATION_FAILURE_RATE_THRESHOLD",
        0.1,
    )
    service, _repository = _service(
        health=_health(
            pending=0,
            retryPending=0,
            acceptedUnresolved=0,
            expiredLeases=0,
            oldestPendingMinutes=0,
            delivered=8,
            terminalFailures=2,
        ),
        lastSweep="2026-09-17T01:00:00+00:00",
        oldestAcceptedHours=0,
    )

    alerts = service.evaluateAlerts()
    failureAlert = next(
        alert
        for alert in alerts
        if alert["alertType"] == "notification_terminal_failure_rate"
    )

    assert failureAlert["actualValue"] == 0.2
    assert failureAlert["threshold"] == 0.1


def _quietHealth(**overrides):
    return _health(**{
        "pending": 0,
        "retryPending": 0,
        "acceptedUnresolved": 0,
        "expiredLeases": 0,
        "oldestPendingMinutes": 0,
        "terminalFailures": 0,
        **overrides,
    })


def _dispatchAlerts(monkeypatch, flag, health):
    if flag is None:
        monkeypatch.delenv("NOTIFICATION_DISPATCH_ENABLED", raising=False)
    else:
        monkeypatch.setenv("NOTIFICATION_DISPATCH_ENABLED", flag)
    service, _repository = _service(health=health, oldestAcceptedHours=0)
    return [
        alert
        for alert in service.evaluateAlerts()
        if alert["alertType"] == "notification_dispatch_disabled"
    ]


def test_dispatch_switched_off_while_emails_wait_raises_high_alert(
    monkeypatch,
):
    alerts = _dispatchAlerts(
        monkeypatch,
        "false",
        _quietHealth(pending=3, retryPending=1, unbridgedBillingIntents=8),
    )

    assert len(alerts) == 1
    assert alerts[0]["severity"] == "HIGH"
    assert alerts[0]["actualValue"] == 12
    assert "NOTIFICATION_DISPATCH_ENABLED" in alerts[0]["message"]


def test_unset_dispatch_flag_counts_as_switched_off(monkeypatch):
    alerts = _dispatchAlerts(
        monkeypatch, None, _quietHealth(unbridgedBillingIntents=2)
    )

    assert [alert["actualValue"] for alert in alerts] == [2]


def test_dispatch_switched_on_raises_no_dispatch_alert(monkeypatch):
    alerts = _dispatchAlerts(
        monkeypatch,
        "true",
        _quietHealth(pending=3, unbridgedBillingIntents=8),
    )

    assert alerts == []


def test_dispatch_switched_off_with_nothing_waiting_raises_no_alert(
    monkeypatch,
):
    alerts = _dispatchAlerts(monkeypatch, "false", _quietHealth())

    assert alerts == []
