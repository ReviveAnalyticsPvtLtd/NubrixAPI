"""Read-only dependency preflight for durable notification delivery."""

__all__ = ["runPreflight"]


import datetime
import json
import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from dotenv import load_dotenv


_REQUIRED_ENVIRONMENT = (
    "DATABASE_URL",
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "FREE_TRIAL_EXPIRY_WARNING_EMAIL_URL",
    "SUPABASE_KEY_OLD",
    "BREVO_API_KEY",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_PASSWORD",
)


def loadRepositoryEnvironment() -> None:
    load_dotenv(REPOSITORY_ROOT / ".env", override=False)


def _defaultRedisClient(environ):
    import redis

    return redis.Redis(
        host=environ["REDIS_HOST"],
        port=int(environ["REDIS_PORT"]),
        password=environ["REDIS_PASSWORD"],
        socket_connect_timeout=10,
        socket_timeout=10,
        decode_responses=True,
    )


def runPreflight(
    *,
    environ=None,
    repository=None,
    edgeClient=None,
    brevoClient=None,
    redisClient=None,
    now=None,
) -> dict:
    """Validate dependencies without sending email or exposing secret values."""
    environment = os.environ if environ is None else environ
    currentTime = (now or (
        lambda: datetime.datetime.now(datetime.timezone.utc)
    ))()
    result = {
        "ok": True,
        "environment": "ok",
        "database": "not_checked",
        "edgeFunction": "not_checked",
        "brevoEvents": "not_checked",
        "redis": "not_checked",
        "scheduler": "not_checked",
    }

    missing = [
        key for key in _REQUIRED_ENVIRONMENT
        if not str(environment.get(key) or "").strip()
    ]
    if missing:
        result["environment"] = "failed:MISSING_REQUIRED_ENVIRONMENT"
        result["ok"] = False

    try:
        if repository is None:
            from api.services.notifications.notificationDeliveryRepository import (
                getNotificationDeliveryRepository,
            )

            repository = getNotificationDeliveryRepository()
        repository.collectHealth(currentTime.isoformat())
        repository.validateClaimCapability()
        result["database"] = "ok"
    except Exception:
        result["database"] = "failed:DATABASE_UNAVAILABLE"
        result["ok"] = False

    try:
        if edgeClient is None:
            from api.services.notifications.edgeEmailClient import EdgeEmailClient

            edgeClient = EdgeEmailClient()
        edgeClient.validate()
        result["edgeFunction"] = "ok"
    except Exception:
        result["edgeFunction"] = "failed:EDGE_VALIDATION_FAILED"
        result["ok"] = False

    try:
        if brevoClient is None:
            from api.services.notifications.brevoEventClient import BrevoEventClient

            brevoClient = BrevoEventClient()
        brevoClient.validate()
        result["brevoEvents"] = "ok"
    except Exception:
        result["brevoEvents"] = "failed:BREVO_EVENT_API_FAILED"
        result["ok"] = False

    try:
        if redisClient is None:
            redisClient = _defaultRedisClient(environment)
        if redisClient.ping() is not True:
            raise RuntimeError("REDIS_PING_FAILED")
        result["redis"] = "ok"
    except Exception:
        result["redis"] = "failed:REDIS_UNAVAILABLE"
        result["ok"] = False

    try:
        from nubrix.triggers.tasks.notificationDispatchTask import HEARTBEAT_KEY

        heartbeat = redisClient.get(HEARTBEAT_KEY)
        if isinstance(heartbeat, bytes):
            heartbeat = heartbeat.decode("utf-8")
        heartbeatAt = datetime.datetime.fromisoformat(
            str(heartbeat or "").replace("Z", "+00:00")
        )
        if heartbeatAt.tzinfo is None:
            heartbeatAt = heartbeatAt.replace(tzinfo=datetime.timezone.utc)
        ageSeconds = (
            currentTime - heartbeatAt.astimezone(datetime.timezone.utc)
        ).total_seconds()
        if ageSeconds < -60 or ageSeconds > 180:
            raise RuntimeError("SCHEDULER_HEARTBEAT_STALE")
        result["scheduler"] = "ok"
    except Exception:
        result["scheduler"] = "failed:SCHEDULER_HEARTBEAT_STALE"
        result["ok"] = False

    return result


def main() -> int:
    loadRepositoryEnvironment()
    result = runPreflight()
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
