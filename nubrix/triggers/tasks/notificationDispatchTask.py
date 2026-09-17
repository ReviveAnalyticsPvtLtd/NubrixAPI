"""Celery wrapper for durable notification dispatch."""

__all__ = [
    "HEARTBEAT_KEY",
    "NotificationDispatchTask",
    "recordNotificationHeartbeat",
]


import os
import socket
import uuid
import datetime

from utils.logger import logger


HEARTBEAT_KEY = "nubrix:notification:beat-worker-heartbeat"


def recordNotificationHeartbeat(redisClient=None, now=None) -> None:
    if redisClient is None:
        import redis

        redisClient = redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            password=os.environ.get("REDIS_PASSWORD"),
            socket_connect_timeout=10,
            socket_timeout=10,
            decode_responses=True,
        )
    currentTime = (now or (
        lambda: datetime.datetime.now(datetime.timezone.utc)
    ))()
    redisClient.set(HEARTBEAT_KEY, currentTime.isoformat(), ex=180)


def _enabled(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() == "true"


def _workerId() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"


class NotificationDispatchTask:
    def __init__(self, service=None, workerId=None):
        self._service = service
        self._workerId = workerId or _workerId

    def execute(self) -> dict:
        if not _enabled("NOTIFICATION_DISPATCH_ENABLED"):
            logger.info("notification.dispatch.disabled")
            return {"status": "disabled"}

        if self._service is None:
            from api.services.notifications.notificationDeliveryService import (
                getNotificationDeliveryService,
            )

            service = getNotificationDeliveryService()
        else:
            service = self._service

        summary = service.dispatchBatch(self._workerId())
        logger.info(f"notification.dispatch.completed summary={summary}")
        return summary
