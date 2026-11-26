"""
databaseImportTask.py

Celery task bodies of a database import, registered in nubrix/triggers/celery.py and routed to the `imports` queue.
Each batch enqueues the next one at the back of the queue, so imports take turns batch by batch.

The broker is not a place for secrets, so a batch message carries the connection only inside `sealed`: every
argument of the message, encrypted with a key derived from SECRET_KEY. A step runs only when its plain arguments
match the sealed ones, so a message altered in the broker cannot change what is read, for whom, or where it is
stored. A driver's error can still quote the credentials, so no exception leaves these tasks, logs name the
exception's type only, and every message is sent with its args' repr redacted.

The default worker, beat and the API import this module through nubrix/triggers/celery.py, so it imports the service
(and with it the AI stack) only inside the task bodies, which run on the imports worker alone.
"""

__all__ = ["IMPORT_BATCH_TASK", "IMPORT_TASK_OPTIONS", "importBatch", "sendBatch"]

import base64
import dataclasses
import hashlib
import hmac
import json
import os
from typing import TYPE_CHECKING

from celery.exceptions import Retry
from cryptography.fernet import Fernet

from utils.logger import logger

if TYPE_CHECKING:
    from api.services.databaseSources import Source

IMPORT_BATCH_TASK = "NubrixAI.importBatch"
# acks_late with reject_on_worker_lost: a batch whose worker died is delivered again and resumes from the record.
IMPORT_TASK_OPTIONS = {"bind": True, "ignore_result": True, "acks_late": True, "reject_on_worker_lost": True}
# Celery puts a repr of the args in each message's headers and in its events; a retry would otherwise rebuild it.
_REDACTED = {"argsrepr": "(redacted)", "kwargsrepr": "{}"}
# A dead delivery's marker runs out after 900 s, well within these 30 minutes.
_BUSY_RETRIES, _BUSY_RETRY_SECONDS = 6, 300
_KEY_DOMAIN = b"nubrix.import-source.v1"


def _cipher() -> Fernet:
    secret = os.environ["SECRET_KEY"]
    if not secret:
        raise ValueError("SECRET_KEY is empty")
    return Fernet(base64.urlsafe_b64encode(hmac.new(secret.encode(), _KEY_DOMAIN, hashlib.sha256).digest()))


def _seal(*fields) -> str:
    return _cipher().encrypt(json.dumps(fields).encode()).decode()


def _unseal(sealed: str, *expected) -> list:
    """The sealed fields after `expected`; raises unless the sealed message starts with exactly `expected`."""
    fields = json.loads(_cipher().decrypt(sealed.encode()))
    if fields[:len(expected)] != json.loads(json.dumps(expected)):
        raise ValueError("the message was sealed for another import step")
    return fields[len(expected):]


def sendBatch(app, projectId: str, userId: str, jobId: str, source: "Source", tables: list[list], start: int) -> None:
    """Enqueue the batch at `start`; the first one is sent the same way. The only place a source enters a message."""
    sealed = _seal(IMPORT_BATCH_TASK, projectId, userId, jobId, tables, start, dataclasses.asdict(source))
    args = [projectId, userId, jobId, sealed, tables, start]
    app.send_task(IMPORT_BATCH_TASK, args=args, ignore_result=True, **_REDACTED)


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


def importBatch(task, projectId: str, userId: str, jobId: str, sealed: str, tables: list[list], start: int) -> None:
    """Run one batch, then enqueue the next one; the last batch ends the job."""
    from api.services import databaseImportService as service
    from api.services.databaseSources import Source

    try:
        (fields,) = _unseal(sealed, IMPORT_BATCH_TASK, projectId, userId, jobId, tables, start)
        source, sourceTables = Source(**fields), [tuple(t) for t in tables]
        nextStart = service.runBatch(projectId, userId, jobId, source, sourceTables, start)
        if nextStart is not None:
            sendBatch(task.app, projectId, userId, jobId, source, tables, nextStart)
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
