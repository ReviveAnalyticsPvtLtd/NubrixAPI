"""Celery wrapper for Brevo delivery reconciliation."""

__all__ = ["NotificationReconciliationTask"]


import os

from utils.logger import logger


def _enabled(name: str) -> bool:
    return os.environ.get(name, "false").strip().lower() == "true"


class NotificationReconciliationTask:
    def __init__(self, service=None):
        self._service = service

    def execute(self) -> dict:
        if not _enabled("NOTIFICATION_RECONCILIATION_ENABLED"):
            logger.info("notification.reconciliation.disabled")
            return {"status": "disabled"}

        if self._service is None:
            from api.services.notifications.notificationDeliveryService import (
                getNotificationDeliveryService,
            )

            service = getNotificationDeliveryService()
        else:
            service = self._service

        summary = service.reconcileBatch()
        logger.info(f"notification.reconciliation.completed summary={summary}")
        return summary
