"""
databaseImportService.py

The background job of a database import: loads the tables batch by batch, describes each batch with AI
metadata, then generates KPIs when the project has none. The Celery tasks that run it are in
nubrix/triggers/tasks/databaseImportTask.py.

Reasons in the record are fixed sentences, and logs carry ids and counts only: a driver's error can quote the
connection's user, host or password, and table names are the customer's.
"""

__all__ = ["JobStopped", "runBatch", "runKpis", "skipRemaining", "endJob", "failJob"]

from api.commons import creditsAllow, tableFileState, updateProjectModifiedAt
from api.services.databaseSources import Source, SourceTable, readChunks, storedName
from api.services.dataLoadService import dataLoadService
from api.services.importJobs import importJobs
from api.services.managementService import managementService
from utils.logger import logger
from utils.projectStorage import StorageObjectNotFound, readJson
from utils.sandboxLimits import _readInt
from utils.storedTables import TOO_LARGE_CODE, TableTooLargeError, writeStoredChunksToPath

NAME_TAKEN = "A table with this name already exists"
OUT_OF_CREDITS = "Out of credits"
CREDITS_UNCHECKED = "Could not check credits"
TOO_LARGE = "Too large to import"
UNREADABLE = "Could not read this table"
DESCRIPTION_MISSING = "Description could not be generated"

# Outlives a chunk by far, and runs out long before the broker's one-hour visibility timeout redelivers a crashed batch.
_MARKER_SECONDS = 900
_DONE = {"loaded", "skipped", "failed"}


class JobStopped(Exception):
    """The job was superseded, or another delivery of this batch is running it: stop and enqueue nothing."""


def _setting(name: str, default: int) -> int:
    return max(1, _readInt(name, default))


def _check(written: bool) -> None:
    """Every job write is refused once a newer job owns the project."""
    if not written:
        raise JobStopped()


def _record(projectId: str, jobId: str) -> dict:
    record = importJobs.get(projectId)
    if not record or record["jobId"] != jobId or record["state"] != "running":
        raise JobStopped()
    return record


def _set(projectId: str, jobId: str, storedAs: str, **fields) -> None:
    _check(importJobs.setTable(projectId, jobId, storedAs, **fields))


def runBatch(projectId: str, userId: str, jobId: str, source: Source, tables: list[SourceTable], start: int) -> int | None:
    """Load and describe the batch of tables from `start`; the next batch's start, or None when every table is done.

    Raises JobStopped when the job was superseded or another delivery of this batch holds it, and
    CreditsUnavailable when credits could not be checked.
    """
    redis = importJobs._redis  # the batch marker sits beside the job's record
    markerKey, marker = f"import:{projectId}:batch", f"{jobId}:{start}"
    # acks_late hands a batch that runs past the visibility timeout to a second worker while the first still runs it.
    if not redis.set(markerKey, marker, nx=True, ex=_MARKER_SECONDS):
        raise JobStopped()
    try:
        return _runBatch(projectId, userId, jobId, source, tables, start, lambda: redis.expire(markerKey, _MARKER_SECONDS))
    finally:
        held = redis.get(markerKey)
        if held is not None and held.decode() == marker:
            redis.delete(markerKey)


def _runBatch(projectId, userId, jobId, source, tables, start, refreshMarker) -> int | None:
    status = {t["storedAs"]: t["status"] for t in _record(projectId, jobId)["tables"]}

    def refresh():
        refreshMarker()
        _check(importJobs.refreshLock(projectId, jobId))

    _check(importJobs.refreshLock(projectId, jobId))
    if not creditsAllow(userId, "metadata_generation"):
        skipRemaining(projectId, jobId, tables, start, OUT_OF_CREDITS)
        return None
    maxTables, maxColumns = _setting("IMPORT_BATCH_TABLES", 8), _setting("IMPORT_BATCH_COLUMNS", 400)
    attempted = columns = 0
    loaded = []
    index = start
    while index < len(tables) and attempted < maxTables and columns < maxColumns:
        table, index = tables[index], index + 1
        storedAs = storedName(table)
        if status[storedAs] in _DONE:  # done by an earlier delivery of this batch
            continue
        attempted += 1
        if status[storedAs] == "loading" and tableFileState(dataLoadService.client, projectId, storedAs) is not None:
            # stored before that delivery stopped, and never described
            fields, width = {"status": "loaded", "reason": DESCRIPTION_MISSING}, 0
        else:
            _set(projectId, jobId, storedAs, status="loading")
            fields, width = _loadTable(projectId, jobId, source, table, storedAs, refresh)
            if fields["status"] == "loaded":
                loaded.append(storedAs)
                columns += width
        _set(projectId, jobId, storedAs, **fields)
        status[storedAs] = fields["status"]
    if loaded:
        updateProjectModifiedAt(projectId)
        described = _describe(projectId, jobId, userId, loaded)
        for name in loaded:
            if name not in described:
                refresh()
                if name not in _describe(projectId, jobId, userId, [name]):
                    _set(projectId, jobId, name, reason=DESCRIPTION_MISSING)
    logger.info(f"Import job {jobId} in project {projectId}: batch from {start} loaded {len(loaded)} of {attempted} tables.")
    return index if index < len(tables) else None


def _loadTable(projectId, jobId, source, table, storedAs, refresh) -> tuple[dict, int]:
    """Store one table; its fields for the record and its column count."""
    shape = {}

    def refreshing(chunks):
        for chunk in chunks:
            yield chunk
            refresh()  # the chunk is written once the writer asks for the next one

    def write(path):
        chunks = readChunks(source, table, _setting("IMPORT_CHUNK_ROWS", 50000))
        shape["rows"], shape["columns"] = writeStoredChunksToPath(refreshing(chunks), projectId, storedAs, path)

    try:
        claims = dataLoadService._acceptTableName(projectId, storedAs)
        dataLoadService._writeTable(projectId, storedAs, claims, write)
    except JobStopped:
        raise
    except Exception as e:
        if getattr(e, "statusCode", None) == 409:
            return {"status": "skipped", "reason": NAME_TAKEN}, 0
        if isinstance(e, TableTooLargeError) or getattr(e, "errorCode", None) == TOO_LARGE_CODE:
            return {"status": "failed", "reason": TOO_LARGE}, 0
        logger.warning(f"Import job {jobId} in project {projectId}: a table could not be read ({type(e).__name__}).")
        return {"status": "failed", "reason": UNREADABLE}, 0
    return {"status": "loaded", "rows": shape["rows"]}, shape["columns"]


def _describe(projectId: str, jobId: str, userId: str, names: list[str]) -> set[str]:
    """The names that got a description."""
    try:
        return set(managementService.generateMetadataForTables(projectId=projectId, tableNames=names, userId=userId))
    except Exception as e:
        logger.warning(f"Import job {jobId} in project {projectId}: describing {len(names)} tables failed ({type(e).__name__}).")
        return set()


def skipRemaining(projectId: str, jobId: str, tables: list[SourceTable], start: int, reason: str) -> None:
    """Skip every table from `start` that is not done yet."""
    status = {t["storedAs"]: t["status"] for t in _record(projectId, jobId)["tables"]}
    for table in tables[start:]:
        storedAs = storedName(table)
        if status[storedAs] not in _DONE:
            _set(projectId, jobId, storedAs, status="skipped", reason=reason)
            status[storedAs] = "skipped"


def runKpis(projectId: str, userId: str, jobId: str) -> None:
    """Generate KPIs when the project has none yet, then end the job as done.

    Raises JobStopped when the job was superseded, and CreditsUnavailable when credits could not be checked.
    """
    kpis = _record(projectId, jobId)["kpis"]
    if kpis in ("waiting", "running"):  # otherwise an earlier delivery stopped between the two writes of endJob
        kpis = _generateKpis(projectId, userId, jobId)
    endJob(projectId, jobId, kpis)


def _generateKpis(projectId: str, userId: str, jobId: str) -> str:
    try:
        readJson(projectId, "insights.json")
        return "skipped"
    except StorageObjectNotFound:
        pass
    if not creditsAllow(userId, "insight_generation"):
        return "skipped"
    _check(importJobs.setKpis(projectId, jobId, "running"))
    try:
        managementService.generateInsightsForProject(projectId, userId=userId)
        return "done"
    except Exception as e:
        logger.warning(f"Import job {jobId} in project {projectId}: KPI generation failed ({type(e).__name__}).")
        return "failed"


def endJob(projectId: str, jobId: str, kpis: str) -> None:
    """Record the KPI step's outcome and end the job as done."""
    _check(importJobs.setKpis(projectId, jobId, kpis))
    _check(importJobs.finish(projectId, jobId, "done"))


def failJob(projectId: str, jobId: str) -> None:
    """End a running job as failed, with its KPI step if that had not ended."""
    record = importJobs.get(projectId)
    if not record or record["jobId"] != jobId or record["state"] != "running":
        return
    if record["kpis"] in ("waiting", "running"):
        importJobs.setKpis(projectId, jobId, "failed")
    importJobs.finish(projectId, jobId, "failed")
