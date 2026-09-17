"""
celery.py

Celery application + beat schedule for NubrixAI billing/subscription/credit/cron jobs.
All times are UTC. See the schedule dict below for exact cadences.
"""

__all__ = ["celeryApp"]


from nubrix.triggers.tasks.pastDueSuspensionTask import PastDueSuspensionTask
from nubrix.triggers.tasks.generateForecasts import GenerateForecasts
from nubrix.triggers.tasks.renewalLifecycleTask import RenewalLifecycleTask
from nubrix.triggers.tasks.annualRenewalTask import AnnualRenewalTask
from nubrix.triggers.tasks.reconciliationTask import ReconciliationTask
from nubrix.triggers.tasks.billingMetricsTask import BillingMetricsTask
from nubrix.triggers.tasks.entitlementBoundaryTask import EntitlementBoundaryTask
from nubrix.triggers.tasks.subscriptionExpiryTask import SubscriptionExpiryTask
from nubrix.triggers.tasks.creditReconciliationTask import CreditReconciliationTask
from nubrix.triggers.tasks.adminSessionCleanupTask import AdminSessionCleanupTask
from nubrix.triggers.tasks.billingTask import DailyBillingTask
from nubrix.triggers.tasks.userErasureTask import UserErasureTask
from nubrix.triggers.tasks.adminTrialCreditSyncTask import AdminTrialCreditSyncTask
from nubrix.triggers.tasks.notificationDispatchTask import (
    NotificationDispatchTask,
    recordNotificationHeartbeat,
)
from nubrix.triggers.tasks.notificationReconciliationTask import NotificationReconciliationTask
from nubrix.triggers.tasks.notificationCleanupTask import NotificationCleanupTask
from celery.schedules import crontab
from celery import Celery
import os

APP_NAME = "NubrixAI"
_redisUrl = f'redis://default:{os.environ.get("REDIS_PASSWORD")}@{os.environ.get("REDIS_HOST")}:{os.environ.get("REDIS_PORT")}'

celeryApp = Celery(APP_NAME, broker=_redisUrl, backend=_redisUrl)


@celeryApp.task(name=f"{APP_NAME}.generateForecasts")
def sendForecasts():
    return GenerateForecasts().generateAndSendForecasts()

@celeryApp.task(name=f"{APP_NAME}.dailyBilling")
def runDailyBilling():
    return DailyBillingTask().execute()

@celeryApp.task(name=f"{APP_NAME}.annualRenewal")
def runAnnualRenewal():
    return AnnualRenewalTask().execute()

@celeryApp.task(name=f"{APP_NAME}.renewalLifecycle")
def runRenewalLifecycle():
    return RenewalLifecycleTask().execute()

@celeryApp.task(name=f"{APP_NAME}.pastDueSuspension")
def runPastDueSuspension():
    return PastDueSuspensionTask().execute()

@celeryApp.task(name=f"{APP_NAME}.entitlementBoundary")
def runEntitlementBoundary():
    return EntitlementBoundaryTask().execute()

@celeryApp.task(name=f"{APP_NAME}.reconciliation")
def runReconciliation():
    return ReconciliationTask().execute()

@celeryApp.task(name=f"{APP_NAME}.billingMetrics")
def runBillingMetrics():
    return BillingMetricsTask().execute()

@celeryApp.task(name=f"{APP_NAME}.subscriptionExpiry")
def runSubscriptionExpiry():
    return SubscriptionExpiryTask().execute()

@celeryApp.task(name=f"{APP_NAME}.creditReconciliation")
def runCreditReconciliation():
    return CreditReconciliationTask().execute()

@celeryApp.task(name=f"{APP_NAME}.adminSessionCleanup")
def runAdminSessionCleanup():
    return AdminSessionCleanupTask().execute()

@celeryApp.task(name=f"{APP_NAME}.userErasure")
def runUserErasure(requestId: str):
    return UserErasureTask().execute(requestId)

@celeryApp.task(name=f"{APP_NAME}.userErasureSweep")
def runUserErasureSweep():
    return UserErasureTask().sweep()

@celeryApp.task(name=f"{APP_NAME}.adminTrialCreditSync")
def syncAdminTrialCredits(extensionId: str):
    return AdminTrialCreditSyncTask().execute(extensionId)

@celeryApp.task(name=f"{APP_NAME}.adminTrialCreditSyncSweep")
def sweepAdminTrialCreditSync():
    return AdminTrialCreditSyncTask().sweep()

@celeryApp.task(name=f"{APP_NAME}.notificationDispatch")
def runNotificationDispatch():
    recordNotificationHeartbeat()
    return NotificationDispatchTask().execute()

@celeryApp.task(name=f"{APP_NAME}.notificationReconciliation")
def runNotificationReconciliation():
    return NotificationReconciliationTask().execute()

@celeryApp.task(name=f"{APP_NAME}.notificationCleanup")
def runNotificationCleanup():
    return NotificationCleanupTask().execute()


celeryApp.conf.beat_schedule = {
    "daily-billing-midnight": {"task": f"{APP_NAME}.dailyBilling", "schedule": crontab(minute=0, hour=0)},
    "annual-renewal-daily": {"task": f"{APP_NAME}.annualRenewal", "schedule": crontab(minute=30, hour=0)},
    "renewal-reminders-daily": {"task": f"{APP_NAME}.renewalLifecycle", "schedule": crontab(minute=0, hour=2)},
    "past-due-suspension-every-30min": {"task": f"{APP_NAME}.pastDueSuspension", "schedule": crontab(minute="*/30")},
    "entitlement-boundary-every-30min": {"task": f"{APP_NAME}.entitlementBoundary", "schedule": crontab(minute="*/30")},
    "reconciliation-every-15min": {"task": f"{APP_NAME}.reconciliation", "schedule": crontab(minute="*/15")},
    "billing-metrics-every-30min": {"task": f"{APP_NAME}.billingMetrics", "schedule": crontab(minute="*/30")},
    "subscription-expiry-daily": {"task": f"{APP_NAME}.subscriptionExpiry", "schedule": crontab(minute=0, hour=1)},
    "credit-reconciliation-hourly": {"task": f"{APP_NAME}.creditReconciliation", "schedule": crontab(minute=0)},
    "admin-session-cleanup-daily": {"task": f"{APP_NAME}.adminSessionCleanup", "schedule": crontab(minute=0, hour=3)},
    "user-erasure-recovery-every-minute": {"task": f"{APP_NAME}.userErasureSweep", "schedule": crontab(minute="*")},
    "admin-trial-credit-sync-every-minute": {"task": f"{APP_NAME}.adminTrialCreditSyncSweep", "schedule": crontab(minute="*")},
    "notification-dispatch-every-minute": {"task": f"{APP_NAME}.notificationDispatch", "schedule": crontab(minute="*")},
    "notification-reconciliation-every-5min": {"task": f"{APP_NAME}.notificationReconciliation", "schedule": crontab(minute="*/5")},
    "notification-cleanup-daily": {"task": f"{APP_NAME}.notificationCleanup", "schedule": crontab(minute=0, hour=4)},
}
celeryApp.conf.timezone = "UTC"
