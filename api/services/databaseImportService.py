"""
databaseImportService.py

The background job of a database import: loads the tables batch by batch, describes each batch with AI
metadata, then generates KPIs when the project has none. The Celery tasks that run it are in
nubrix/triggers/tasks/databaseImportTask.py.

Reasons in the record are fixed sentences, and logs carry ids and counts only: a driver's error can quote the
connection's user, host or password, and table names are the customer's.

Two Redis keys beside the job's record keep deliveries apart. `import:{projectId}:batch` is held by the delivery
running a step (a batch or the KPI step), so a second delivery of it waits instead of running it twice.
`import:{projectId}:next` names the step that should run next, so a late delivery of a step another delivery
already finished does nothing.
"""

__all__ = ["CREDITS_UNCHECKED", "BatchBusy", "JobStopped", "runBatch", "runKpis", "failJob"]

from contextlib import contextmanager

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

# Outlives a chunk by far; a dead delivery's marker runs out well within the retries a second delivery makes.
_MARKER_SECONDS = 900
_POINTER_SECONDS = 604800
_KPIS = "kpis"


class JobStopped(Exception):
    """The job was superseded, or this step was already done by another delivery: stop and enqueue nothing."""


class BatchBusy(Exception):
    """Another delivery holds this step: it is running it, or it died less than _MARKER_SECONDS ago."""


def _setting(name: str, default: int) -> int:
    return max(1, _readInt(name, default))


def _storedAs(table: tuple) -> str:
    """The name a table is stored under: a third element when the route made names unique, else derived."""
    return table[2] if len(table) == 3 else storedName(table)


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


@contextmanager
def _holding(projectId: str, jobId: str, step):
    """Hold the project's step marker for this delivery; yields a function that extends it."""
    redis = importJobs._redis  # the markers sit beside the job's record
    key, value = f"import:{projectId}:batch", f"{jobId}:{step}"
    # acks_late delivers a step again when its worker dies, or when it runs past the visibility timeout
    if not redis.set(key, value, nx=True, ex=_MARKER_SECONDS):
        raise BatchBusy()
    try:
        yield lambda: redis.expire(key, _MARKER_SECONDS)
    finally:
        held = redis.get(key)
        if held is not None and held.decode() == value:
            redis.delete(key)


def _nextStep(projectId: str, jobId: str):
    """The step that should run next: a start index, or _KPIS. Missing, or another job's, means 0."""
    raw = importJobs._redis.get(f"import:{projectId}:next")
    owner, _, step = raw.decode().partition(":") if raw else ("", "", "")
    if owner != jobId:
        return 0
    return step if step == _KPIS else int(step)


def _advance(projectId: str, jobId: str, step) -> None:
    _record(projectId, jobId)
    importJobs._redis.set(f"import:{projectId}:next", f"{jobId}:{step}", ex=_POINTER_SECONDS)


def runBatch(projectId: str, userId: str, jobId: str, source: Source, tables: list[SourceTable], start: int,
             skipWith: str | None = None) -> int | None:
    """Load and describe the batch of tables from `start`; the next batch's start, or None when every table is done.

    With `skipWith`, every table left from `start` is skipped with that reason instead. Raises JobStopped when the
    job was superseded or this batch is already done, BatchBusy when another delivery holds it, and
    CreditsUnavailable when credits could not be checked.
    """
    with _holding(projectId, jobId, start) as refreshMarker:
        step = _nextStep(projectId, jobId)
        if step == _KPIS or step > start:
            raise JobStopped()
        nextStart = _runBatch(projectId, userId, jobId, source, tables, start, refreshMarker, skipWith)
        # moved on before the marker is released, so a delivery waiting for it finds the batch done
        _advance(projectId, jobId, _KPIS if nextStart is None else nextStart)
        return nextStart


def _runBatch(projectId, userId, jobId, source, tables, start, refreshMarker, skipWith) -> int | None:
    status = {t["storedAs"]: t["status"] for t in _record(projectId, jobId)["tables"]}

    def refresh():
        refreshMarker()
        _check(importJobs.refreshLock(projectId, jobId))

    _check(importJobs.refreshLock(projectId, jobId))
    if skipWith is None and not creditsAllow(userId, "metadata_generation"):
        skipWith = OUT_OF_CREDITS
    if skipWith is not None:
        for table in tables[start:]:
            storedAs = _storedAs(table)
            if status[storedAs] in ("pending", "loading"):
                _set(projectId, jobId, storedAs, status="skipped", reason=skipWith)
                status[storedAs] = "skipped"
        return None
    maxTables, maxColumns = _setting("IMPORT_BATCH_TABLES", 8), _setting("IMPORT_BATCH_COLUMNS", 400)
    attempted = columns = 0
    loaded = []
    described = None  # metadata.json's tables, read when the first table an earlier delivery stored comes up
    index = start
    while index < len(tables) and attempted < maxTables and columns < maxColumns:
        table, index = tables[index], index + 1
        storedAs = _storedAs(table)
        state = status[storedAs]
        if state in ("skipped", "failed"):
            continue
        if state == "loaded" or (state == "loading" and tableFileState(dataLoadService.client, projectId, storedAs) is not None):
            # stored by an earlier delivery of this batch, which may have died before describing it
            if described is None:
                described = _describedTables(projectId, jobId)
            if storedAs in described:
                continue
            attempted += 1  # its column count would need the file's footer read; 0 keeps the batch at least as big
            _set(projectId, jobId, storedAs, status="loaded", reason=None)
        else:
            attempted += 1
            _set(projectId, jobId, storedAs, status="loading")
            fields, width = _loadTable(projectId, jobId, source, table, storedAs, refresh)
            _set(projectId, jobId, storedAs, **fields)
            if fields["status"] != "loaded":
                status[storedAs] = fields["status"]
                continue
            columns += width
        status[storedAs] = "loaded"
        loaded.append(storedAs)
    if loaded:
        updateProjectModifiedAt(projectId)
        answered = _describe(projectId, jobId, userId, loaded)
        for name in loaded:
            if name not in answered:
                refresh()
                if name not in _describe(projectId, jobId, userId, [name]):
                    _set(projectId, jobId, name, reason=DESCRIPTION_MISSING)
    logger.info(f"Import job {jobId} in project {projectId}: batch from {start} loaded {len(loaded)} of {attempted} tables.")
    return index if index < len(tables) else None


def _describedTables(projectId: str, jobId: str) -> set[str]:
    try:
        return set(readJson(projectId, "metadata.json"))
    except StorageObjectNotFound:
        return set()
    except Exception as e:
        # describing a table again only costs a model call
        logger.warning(f"Import job {jobId} in project {projectId}: metadata.json could not be read ({type(e).__name__}).")
        return set()


def _loadTable(projectId, jobId, source, table, storedAs, refresh) -> tuple[dict, int]:
    """Store one table; its fields for the record and its column count."""
    shape = {}

    def refreshing(chunks):
        for chunk in chunks:
            yield chunk
            refresh()  # the chunk is written once the writer asks for the next one

    def write(path):
        rows = _setting("IMPORT_MONGO_CHUNK_ROWS", 5000) if source.kind == "mongodb" else _setting("IMPORT_CHUNK_ROWS", 50000)
        chunks = readChunks(source, table[:2], rows, _setting("IMPORT_CHUNK_CELLS", 2000000))
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


def runKpis(projectId: str, userId: str, jobId: str, skip: bool = False) -> None:
    """Generate KPIs when the project has none yet (never, with `skip`), then end the job as done.

    Raises JobStopped when the job has ended or was superseded, BatchBusy when another delivery holds the step, and
    CreditsUnavailable when credits could not be checked.
    """
    with _holding(projectId, jobId, _KPIS):
        kpis = _record(projectId, jobId)["kpis"]
        if kpis in ("waiting", "running"):  # otherwise an earlier delivery stopped between the two writes below
            kpis = "skipped" if skip else _generateKpis(projectId, userId, jobId)
        _check(importJobs.setKpis(projectId, jobId, kpis))
        _check(importJobs.finish(projectId, jobId, "done"))


def _exists(projectId: str, fileName: str) -> bool:
    try:
        readJson(projectId, fileName)
        return True
    except StorageObjectNotFound:
        return False


def _generateKpis(projectId: str, userId: str, jobId: str) -> str:
    # skipped when the project has KPIs already, or when nothing was described to find KPIs in
    if _exists(projectId, "insights.json") or not _exists(projectId, "metadata.json"):
        return "skipped"
    if not creditsAllow(userId, "insight_generation"):
        return "skipped"
    _check(importJobs.setKpis(projectId, jobId, "running"))
    try:
        managementService.generateInsightsForProject(projectId, userId=userId)
        return "done"
    except Exception as e:
        logger.warning(f"Import job {jobId} in project {projectId}: KPI generation failed ({type(e).__name__}).")
        return "failed"


def failJob(projectId: str, jobId: str) -> None:
    """End a running job as failed, with its KPI step if that had not ended."""
    record = importJobs.get(projectId)
    if not record or record["jobId"] != jobId or record["state"] != "running":
        return
    if record["kpis"] in ("waiting", "running"):
        importJobs.setKpis(projectId, jobId, "failed")
    importJobs.finish(projectId, jobId, "failed")
