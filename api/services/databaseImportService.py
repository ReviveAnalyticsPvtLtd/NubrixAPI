"""
databaseImportService.py

The background job of a database import: loads the chosen tables batch by batch and ends the job after the last
batch. Metadata and KPIs are a separate step the client runs afterwards through the existing routes, as for the
other connectors. The Celery task that runs it is in nubrix/triggers/tasks/databaseImportTask.py.

Reasons in the record are fixed sentences, and logs carry ids and counts only: a driver's error can quote the
connection's user, host or password, and table names are the customer's.

Two Redis keys beside the job's record keep deliveries apart. `import:{projectId}:batch` is held by the delivery
running a batch, so a second delivery of it waits instead of running it twice.
`import:{projectId}:next` names the step that should run next, so a late delivery of a step another delivery
already finished does nothing.
"""

__all__ = ["BatchBusy", "JobStopped", "runBatch", "failJob"]

from contextlib import contextmanager

from api.commons import tableFileState, updateProjectModifiedAt
from api.services.databaseSources import Source, SourceTable, readChunks, storedName
from api.services.dataLoadService import dataLoadService
from api.services.importJobs import importJobs
from utils.logger import logger
from utils.sandboxLimits import _readInt
from utils.storageListing import removeProjectFiles
from utils.storedTables import TOO_LARGE_CODE, TableTooLargeError, writeStoredChunksToPath

NAME_TAKEN = "A table with this name already exists"
TOO_LARGE = "Too large to import"
UNREADABLE = "Could not read this table"

# Outlives a chunk by far; a dead delivery's marker runs out well within the retries a second delivery makes.
_MARKER_SECONDS = 900
_POINTER_SECONDS = 604800
_DONE = "done"


class JobStopped(Exception):
    """The job was superseded, its project was erased or deleted, or this step was already done by another delivery:
    stop and enqueue nothing."""


class BatchBusy(Exception):
    """Another delivery holds this step: it is running it, or it died less than _MARKER_SECONDS ago."""


def _setting(name: str, default: int) -> int:
    return max(1, _readInt(name, default))


def _storedAs(table: tuple) -> str:
    """The name a table is stored under: a third element when the route made names unique, else derived."""
    name = table[2] if len(table) == 3 else storedName(table)
    # the route only makes sanitised names; anything else could reach another project's storage path
    if not name or name != dataLoadService._sanitizeName(name):
        raise ValueError("unsafe stored name")
    return name


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
    except JobStopped:
        _removeIfDeleted(projectId)
        raise
    finally:
        held = redis.get(key)
        if held is not None and held.decode() == value:
            redis.delete(key)


def _projectExists(projectId: str) -> bool:
    return bool(dataLoadService.client.table("Projects").select("projectId").eq("projectId", projectId).limit(1).execute().data)


def _requireProject(projectId: str) -> None:
    """Stop a job whose project is gone: it may have been started while the delete's own cleanup ran."""
    try:
        exists = _projectExists(projectId)
    except Exception as e:
        # a brief Supabase error must not end the import; the next check, and every record write, still stop it
        logger.warning(f"Project check of the import in project {projectId} failed ({type(e).__name__}).")
        return
    if not exists:
        importJobs.forget(projectId)
        raise JobStopped()


def _removeIfDeleted(projectId: str) -> None:
    """A step stopped by an erasure or project delete removes what it wrote after that delete's own removal.

    Only when both the job record and the project row are gone, so a lost Redis record never empties a live project.
    """
    try:
        if importJobs.get(projectId) is None and not _projectExists(projectId):
            importJobs.forget(projectId)  # a step may have set its pointer again after the delete
            removeProjectFiles(dataLoadService.client, projectId)
    except Exception as e:
        logger.error(f"Files of stopped import in project {projectId} could not be cleaned up ({type(e).__name__}).")


def _nextStep(projectId: str, jobId: str):
    """The step that should run next: a start index, or _DONE. Missing, or another job's, means 0."""
    raw = importJobs._redis.get(f"import:{projectId}:next")
    owner, _, step = raw.decode().partition(":") if raw else ("", "", "")
    if owner != jobId:
        return 0
    return step if step == _DONE else int(step)


def _advance(projectId: str, jobId: str, step) -> None:
    _record(projectId, jobId)
    importJobs._redis.set(f"import:{projectId}:next", f"{jobId}:{step}", ex=_POINTER_SECONDS)
    _record(projectId, jobId)  # a delete between the two must not leave the pointer, or send the next step


def runBatch(projectId: str, userId: str, jobId: str, source: Source, tables: list[SourceTable], start: int) -> int | None:
    """Load the batch of tables from `start`; the next batch's start, or None once every table is done and the job
    has ended as done.

    Raises JobStopped when the job was superseded or this batch is already done, and BatchBusy when another delivery
    holds it.
    """
    with _holding(projectId, jobId, start) as refreshMarker:
        _requireProject(projectId)
        step = _nextStep(projectId, jobId)
        if step == _DONE or step > start:
            raise JobStopped()
        nextStart = _runBatch(projectId, jobId, source, tables, start, refreshMarker)
        # moved on before the marker is released, so a delivery waiting for it finds the batch done
        _advance(projectId, jobId, _DONE if nextStart is None else nextStart)
        if nextStart is None:
            _check(importJobs.finish(projectId, jobId, "done"))
        return nextStart


def _runBatch(projectId, jobId, source, tables, start, refreshMarker) -> int | None:
    status = {t["storedAs"]: t["status"] for t in _record(projectId, jobId)["tables"]}

    def refresh():
        refreshMarker()
        _check(importJobs.refreshLock(projectId, jobId))

    _check(importJobs.refreshLock(projectId, jobId))
    maxTables, maxColumns = _setting("IMPORT_BATCH_TABLES", 8), _setting("IMPORT_BATCH_COLUMNS", 400)
    attempted = columns = loaded = 0
    index = start
    while index < len(tables) and attempted < maxTables and columns < maxColumns:
        table, index = tables[index], index + 1
        storedAs = _storedAs(table)
        state = status[storedAs]
        if state in ("skipped", "failed", "loaded"):
            continue
        if state == "loading" and tableFileState(dataLoadService.client, projectId, storedAs) is not None:
            # stored by an earlier delivery of this batch, which died before marking it
            _set(projectId, jobId, storedAs, status="loaded", reason=None)
            continue
        attempted += 1
        _set(projectId, jobId, storedAs, status="loading")
        fields, width = _loadTable(projectId, jobId, source, table, storedAs, refresh)
        _set(projectId, jobId, storedAs, **fields)
        if fields["status"] == "loaded":
            columns += width
            loaded += 1
    if loaded:
        updateProjectModifiedAt(projectId)
    logger.info(f"Import job {jobId} in project {projectId}: batch from {start} loaded {loaded} of {attempted} tables.")
    return index if index < len(tables) else None


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


def failJob(projectId: str, jobId: str) -> None:
    """End a running job as failed."""
    record = importJobs.get(projectId)
    if not record or record["jobId"] != jobId or record["state"] != "running":
        return
    importJobs.finish(projectId, jobId, "failed")
