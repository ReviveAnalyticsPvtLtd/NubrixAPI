"""
databaseImportTask.py

Celery task bodies of a database import, registered in nubrix/triggers/celery.py and routed to the `imports` queue.
Each batch enqueues the next one at the back of the queue, so imports take turns batch by batch.

sourceFields carry the connection's credentials, and a driver's error can quote them, so no exception leaves these
tasks and logs name the exception's type only.
"""

__all__ = ["IMPORT_BATCH_TASK", "IMPORT_KPIS_TASK", "IMPORT_TASK_OPTIONS", "importBatch", "importKpis"]

from celery.exceptions import Retry

from api.commons import CreditsUnavailable
from api.services.databaseImportService import CREDITS_UNCHECKED, JobStopped, endJob, failJob, runBatch, runKpis, skipRemaining
from api.services.databaseSources import Source
from utils.logger import logger

IMPORT_BATCH_TASK = "NubrixAI.importBatch"
IMPORT_KPIS_TASK = "NubrixAI.importKpis"
# acks_late with reject_on_worker_lost: a batch whose worker died is delivered again and resumes from the record.
IMPORT_TASK_OPTIONS = {"bind": True, "ignore_result": True, "acks_late": True, "reject_on_worker_lost": True}
_CREDIT_RETRIES = 3
_CREDIT_RETRY_SECONDS = 60


def _retryCredits(task) -> None:
    """Ask again in a minute, until the retries are spent."""
    if task.request.retries < _CREDIT_RETRIES:
        raise task.retry(countdown=_CREDIT_RETRY_SECONDS, max_retries=_CREDIT_RETRIES)


def _fail(projectId: str, jobId: str, error: Exception) -> None:
    logger.error(f"Import job {jobId} in project {projectId} failed ({type(error).__name__}).")
    try:
        failJob(projectId, jobId)
    except Exception as e:
        logger.error(f"Import job {jobId} in project {projectId} could not be marked failed ({type(e).__name__}).")


def importBatch(task, projectId: str, userId: str, jobId: str, sourceFields: dict, tables: list[list], start: int) -> None:
    """Run one batch, then enqueue the next one, or the KPI step after the last."""
    try:
        sourceTables = [tuple(t) for t in tables]
        try:
            nextStart = runBatch(projectId, userId, jobId, Source(**sourceFields), sourceTables, start)
        except CreditsUnavailable:
            _retryCredits(task)
            skipRemaining(projectId, jobId, sourceTables, start, CREDITS_UNCHECKED)
            nextStart = None
        if nextStart is None:
            task.app.send_task(IMPORT_KPIS_TASK, args=[projectId, userId, jobId])
        else:
            task.app.send_task(IMPORT_BATCH_TASK, args=[projectId, userId, jobId, sourceFields, tables, nextStart])
        logger.info(f"Import job {jobId} in project {projectId}: batch from {start} done, next {nextStart}.")
    except Retry:
        raise
    except JobStopped:
        logger.info(f"Import job {jobId} in project {projectId}: batch from {start} stopped, superseded or already running.")
    except Exception as e:
        _fail(projectId, jobId, e)


def importKpis(task, projectId: str, userId: str, jobId: str) -> None:
    """Generate KPIs if the project has none, and end the job."""
    try:
        try:
            runKpis(projectId, userId, jobId)
        except CreditsUnavailable:
            _retryCredits(task)
            endJob(projectId, jobId, "skipped")
        logger.info(f"Import job {jobId} in project {projectId} done.")
    except Retry:
        raise
    except JobStopped:
        logger.info(f"Import job {jobId} in project {projectId}: KPI step stopped, superseded.")
    except Exception as e:
        _fail(projectId, jobId, e)
