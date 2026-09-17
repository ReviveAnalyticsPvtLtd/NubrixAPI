"""
subscriptionExpiryTask.py

Celery Beat task that runs the subscription expiry sweep daily.

Delegates to ``recalculateSubscriptionDays`` which transitions
trial, cancelled, and other stale subscriptions to ``expired``
once their ``current_period_end`` has passed. Also refreshes
each row's ``billing_state`` lifecycle snapshot.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["SubscriptionExpiryTask"]

from utils.logger import logger
from api.services.billing.billingEventService import BillingEventService


class SubscriptionExpiryTask:
    """
    Daily sweep that expires subscriptions past their period end.
    """

    def __init__(self, client=None, sweep=None, eventServiceFactory=None):
        self._client = client
        self._sweep = sweep
        self._eventServiceFactory = eventServiceFactory or BillingEventService

    def execute(self) -> dict:
        """
        Run the subscription expiry recalculation.

        Returns:
            dict: Execution summary.
        """
        logger.info("Subscription expiry task started")
        if self._sweep is None:
            from nubrix.components.subscriptionManager import recalculateSubscriptionDays

            sweep = recalculateSubscriptionDays
        else:
            sweep = self._sweep
        if self._client is None:
            from api.commons import client
        else:
            client = self._client

        summary = sweep()
        self._eventServiceFactory(client).log_event(
            user_id="system",
            event_type="subscription.expiry_sweep.completed",
            event_status=(
                "COMPLETED"
                if summary["errors"] == 0
                else "COMPLETED_WITH_ERRORS"
            ),
            category="system",
            metadata=summary,
            idempotency_key=None,
        )
        logger.info(f"Subscription expiry task completed: {summary}")
        return summary
