import datetime
from pathlib import Path


NOW = datetime.datetime(2026, 9, 17, 1, 0, tzinfo=datetime.timezone.utc)


class _Service:
    def __init__(self):
        self.dispatchWorkerId = None
        self.reconciled = False

    def dispatchBatch(self, workerId):
        self.dispatchWorkerId = workerId
        return {"claimed": 2, "accepted": 2}

    def reconcileBatch(self):
        self.reconciled = True
        return {"checked": 3, "delivered": 1}


class _Repository:
    def __init__(self):
        self.cutoff = None
        self.healthNow = None
        self.claimValidated = False

    def deleteTerminalBefore(self, cutoff):
        self.cutoff = cutoff
        return 4

    def collectHealth(self, now):
        self.healthNow = now
        return {"pending": 0, "expiredLeases": 0}

    def validateClaimCapability(self):
        self.claimValidated = True


class _Validator:
    def __init__(self):
        self.validated = False

    def validate(self):
        self.validated = True


class _Redis:
    def __init__(self, result=True, heartbeat=None):
        self.result = result
        self.heartbeat = heartbeat or NOW.isoformat()
        self.pinged = False
        self.setCalls = []

    def ping(self):
        self.pinged = True
        return self.result

    def get(self, _key):
        return self.heartbeat

    def set(self, *args, **kwargs):
        self.setCalls.append((args, kwargs))
        return True


def _environment():
    return {
        "DATABASE_URL": "postgresql://example.invalid/db",
        "SUPABASE_URL": "https://example.invalid",
        "SUPABASE_KEY": "secret",
        "FREE_TRIAL_EXPIRY_WARNING_EMAIL_URL": "https://example.invalid/edge",
        "SUPABASE_KEY_OLD": "secret",
        "BREVO_API_KEY": "secret",
        "REDIS_HOST": "localhost",
        "REDIS_PORT": "6379",
        "REDIS_PASSWORD": "secret",
    }


def test_dispatch_task_is_disabled_without_flag(monkeypatch):
    from nubrix.triggers.tasks.notificationDispatchTask import (
        NotificationDispatchTask,
    )

    monkeypatch.setenv("NOTIFICATION_DISPATCH_ENABLED", "false")
    service = _Service()

    assert NotificationDispatchTask(service=service).execute() == {
        "status": "disabled"
    }
    assert service.dispatchWorkerId is None


def test_dispatch_task_delegates_when_enabled(monkeypatch):
    from nubrix.triggers.tasks.notificationDispatchTask import (
        NotificationDispatchTask,
    )

    monkeypatch.setenv("NOTIFICATION_DISPATCH_ENABLED", "true")
    service = _Service()

    result = NotificationDispatchTask(
        service=service,
        workerId=lambda: "worker-1",
    ).execute()

    assert result == {"claimed": 2, "accepted": 2}
    assert service.dispatchWorkerId == "worker-1"


def test_reconciliation_task_is_disabled_without_flag(monkeypatch):
    from nubrix.triggers.tasks.notificationReconciliationTask import (
        NotificationReconciliationTask,
    )

    monkeypatch.setenv("NOTIFICATION_RECONCILIATION_ENABLED", "false")
    service = _Service()

    assert NotificationReconciliationTask(service=service).execute() == {
        "status": "disabled"
    }
    assert service.reconciled is False


def test_reconciliation_task_delegates_when_enabled(monkeypatch):
    from nubrix.triggers.tasks.notificationReconciliationTask import (
        NotificationReconciliationTask,
    )

    monkeypatch.setenv("NOTIFICATION_RECONCILIATION_ENABLED", "true")
    service = _Service()

    result = NotificationReconciliationTask(service=service).execute()

    assert result == {"checked": 3, "delivered": 1}
    assert service.reconciled is True


def test_cleanup_uses_ninety_day_cutoff():
    from nubrix.triggers.tasks.notificationCleanupTask import (
        NotificationCleanupTask,
    )

    repository = _Repository()
    result = NotificationCleanupTask(
        repository=repository,
        now=lambda: NOW,
    ).execute()

    assert repository.cutoff == "2026-06-19T01:00:00+00:00"
    assert result == {"deleted": 4, "retentionDays": 90, "errors": 0}


def test_celery_schedule_contains_notification_jobs():
    from nubrix.triggers.celery import celeryApp

    schedule = celeryApp.conf.beat_schedule
    assert schedule["notification-dispatch-every-minute"]["task"] == (
        "NubrixAI.notificationDispatch"
    )
    assert schedule["notification-reconciliation-every-5min"]["task"] == (
        "NubrixAI.notificationReconciliation"
    )
    assert schedule["notification-cleanup-daily"]["task"] == (
        "NubrixAI.notificationCleanup"
    )


def test_preflight_is_read_only_and_validates_every_dependency():
    from scripts.notificationDeliveryPreflight import runPreflight

    repository = _Repository()
    edge = _Validator()
    brevo = _Validator()
    redisClient = _Redis()

    result = runPreflight(
        environ=_environment(),
        repository=repository,
        edgeClient=edge,
        brevoClient=brevo,
        redisClient=redisClient,
        now=lambda: NOW,
    )

    assert result == {
        "ok": True,
        "environment": "ok",
        "database": "ok",
        "edgeFunction": "ok",
        "brevoEvents": "ok",
        "redis": "ok",
        "scheduler": "ok",
    }
    assert repository.healthNow == NOW.isoformat()
    assert repository.claimValidated is True
    assert edge.validated is True
    assert brevo.validated is True
    assert redisClient.pinged is True


def test_preflight_reports_missing_environment_without_secret_values():
    from scripts.notificationDeliveryPreflight import runPreflight

    result = runPreflight(
        environ={},
        repository=_Repository(),
        edgeClient=_Validator(),
        brevoClient=_Validator(),
        redisClient=_Redis(),
        now=lambda: NOW,
    )

    assert result["ok"] is False
    assert result["environment"] == "failed:MISSING_REQUIRED_ENVIRONMENT"
    assert set(result) == {
        "ok",
        "environment",
        "database",
        "edgeFunction",
        "brevoEvents",
        "redis",
        "scheduler",
    }


def test_preflight_entrypoint_loads_repository_environment(monkeypatch):
    import scripts.notificationDeliveryPreflight as preflight

    calls = []
    monkeypatch.setattr(
        preflight,
        "load_dotenv",
        lambda path, override: calls.append((Path(path), override)),
    )

    preflight.loadRepositoryEnvironment()

    assert calls == [
        (
            Path(preflight.__file__).resolve().parents[1] / ".env",
            False,
        )
    ]
    assert preflight.REPOSITORY_ROOT == Path(preflight.__file__).resolve().parents[1]
    assert str(preflight.REPOSITORY_ROOT) in preflight.sys.path


def test_dispatch_heartbeat_is_bounded_and_contains_no_secrets():
    from nubrix.triggers.tasks.notificationDispatchTask import (
        HEARTBEAT_KEY,
        recordNotificationHeartbeat,
    )

    redisClient = _Redis()

    recordNotificationHeartbeat(
        redisClient=redisClient,
        now=lambda: NOW,
    )

    assert redisClient.setCalls == [(
        (HEARTBEAT_KEY, NOW.isoformat()),
        {"ex": 180},
    )]


def test_preflight_rejects_stale_scheduler_heartbeat():
    from scripts.notificationDeliveryPreflight import runPreflight

    result = runPreflight(
        environ=_environment(),
        repository=_Repository(),
        edgeClient=_Validator(),
        brevoClient=_Validator(),
        redisClient=_Redis(heartbeat="2026-09-17T00:56:59+00:00"),
        now=lambda: NOW,
    )

    assert result["ok"] is False
    assert result["scheduler"] == "failed:SCHEDULER_HEARTBEAT_STALE"
