"""
databaseImportTask.py

Celery task bodies of a database import, registered in nubrix/triggers/celery.py and routed to the `imports` queue.
Each batch enqueues the next one at the back of the queue, so imports take turns batch by batch.

sourceFields carry the connection's credentials, and a driver's error can quote them, so no exception leaves these
tasks, logs name the exception's type only, and every message is sent with its args' repr redacted.

The default worker, beat and the API import this module through nubrix/triggers/celery.py, so it imports the service
(and with it the AI stack) only inside the task bodies, which run on the imports worker alone.
"""

__all__ = ["IMPORT_BATCH_TASK", "IMPORT_KPIS_TASK", "IMPORT_TASK_OPTIONS", "importBatch", "importKpis", "sendBatch"]

from celery.exceptions import Retry

from api.commons import CreditsUnavailable
from utils.logger import logger

IMPORT_BATCH_TASK = "NubrixAI.importBatch"
IMPORT_KPIS_TASK = "NubrixAI.importKpis"
# acks_late with reject_on_worker_lost: a step whose worker died is delivered again and resumes from the record.
IMPORT_TASK_OPTIONS = {"bind": True, "ignore_result": True, "acks_late": True, "reject_on_worker_lost": True}
# Celery puts a repr of the args in each message's headers and in its events; a retry would otherwise rebuild it.
_REDACTED = {"argsrepr": "(redacted)", "kwargsrepr": "{}"}
_CREDIT_RETRIES, _CREDIT_RETRY_SECONDS = 3, 60
# A dead delivery's marker runs out after 900 s, well within these 30 minutes.
_BUSY_RETRIES, _BUSY_RETRY_SECONDS = 6, 300


def sendBatch(app, projectId: str, userId: str, jobId: str, sourceFields: dict, tables: list[list], start: int) -> None:
    """Enqueue the batch at `start`; the first one is sent the same way."""
    app.send_task(IMPORT_BATCH_TASK, args=[projectId, userId, jobId, sourceFields, tables, start], ignore_result=True, **_REDACTED)


def _retry(task, retries: int, seconds: int) -> None:
    """Deliver this step again in `seconds`, until `retries` are spent."""
    if task.request.retries < retries:
        raise task.retry(countdown=seconds, max_retries=retries, **_REDACTED)


def _fail(service, projectId: str, jobId: str, error: Exception) -> None:
    logger.error(f"Import job {jobId} in project {projectId} failed ({type(error).__name__}).")
    try:
        service.failJob(projectId, jobId)
    except Exception as e:
        logger.error(f"Import job {jobId} in project {projectId} could not be marked failed ({type(e).__name__}).")


def importBatch(task, projectId: str, userId: str, jobId: str, sourceFields: dict, tables: list[list], start: int) -> None:
    """Run one batch, then enqueue the next one, or the KPI step after the last."""
    from api.services import databaseImportService as service
    from api.services.databaseSources import Source

    try:
        source, sourceTables = Source(**sourceFields), [tuple(t) for t in tables]
        try:
            nextStart = service.runBatch(projectId, userId, jobId, source, sourceTables, start)
        except CreditsUnavailable:
            _retry(task, _CREDIT_RETRIES, _CREDIT_RETRY_SECONDS)
            nextStart = service.runBatch(projectId, userId, jobId, source, sourceTables, start, skipWith=service.CREDITS_UNCHECKED)
        if nextStart is None:
            task.app.send_task(IMPORT_KPIS_TASK, args=[projectId, userId, jobId], ignore_result=True, **_REDACTED)
        else:
            sendBatch(task.app, projectId, userId, jobId, sourceFields, tables, nextStart)
        logger.info(f"Import job {jobId} in project {projectId}: batch from {start} done, next {nextStart}.")
    except Retry:
        raise
    except service.BatchBusy:
        _retry(task, _BUSY_RETRIES, _BUSY_RETRY_SECONDS)
        logger.info(f"Import job {jobId} in project {projectId}: batch from {start} is still held by a running delivery.")
    except service.JobStopped:
        logger.info(f"Import job {jobId} in project {projectId}: batch from {start} stopped, superseded or already done.")
    except Exception as e:
        _fail(service, projectId, jobId, e)


def importKpis(task, projectId: str, userId: str, jobId: str) -> None:
    """Generate KPIs if the project has none, and end the job."""
    from api.services import databaseImportService as service

    try:
        try:
            service.runKpis(projectId, userId, jobId)
        except CreditsUnavailable:
            _retry(task, _CREDIT_RETRIES, _CREDIT_RETRY_SECONDS)
            service.runKpis(projectId, userId, jobId, skip=True)
        logger.info(f"Import job {jobId} in project {projectId} done.")
    except Retry:
        raise
    except service.BatchBusy:
        _retry(task, _BUSY_RETRIES, _BUSY_RETRY_SECONDS)
        logger.info(f"Import job {jobId} in project {projectId}: KPI step is still held by a running delivery.")
    except service.JobStopped:
        logger.info(f"Import job {jobId} in project {projectId}: KPI step stopped, the job has ended or was superseded.")
    except Exception as e:
        _fail(service, projectId, jobId, e)
