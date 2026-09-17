"""Retention cleanup for terminal notification-delivery records."""

__all__ = ["NotificationCleanupTask", "RETENTION_DAYS"]


import datetime

from utils.logger import logger


RETENTION_DAYS = 90


class NotificationCleanupTask:
    def __init__(self, repository=None, now=None):
        self._repository = repository
        self._now = now or (
            lambda: datetime.datetime.now(datetime.timezone.utc)
        )

    def execute(self) -> dict:
        if self._repository is None:
            from api.services.notifications.notificationDeliveryRepository import (
                getNotificationDeliveryRepository,
            )

            repository = getNotificationDeliveryRepository()
        else:
            repository = self._repository

        cutoff = self._now() - datetime.timedelta(days=RETENTION_DAYS)
        deleted = repository.deleteTerminalBefore(cutoff.isoformat())
        summary = {
            "deleted": deleted,
            "retentionDays": RETENTION_DAYS,
            "errors": 0,
        }
        logger.info(f"notification.cleanup.completed summary={summary}")
        return summary
