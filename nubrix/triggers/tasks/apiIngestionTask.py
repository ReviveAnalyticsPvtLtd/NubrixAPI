"""Run a bounded REST snapshot and publish it under an immutable object name."""

from datetime import date, datetime
from hashlib import file_digest, sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pyarrow as pa

from api.commons import client, productAccessCache
from api.services.ingestion import repository
from api.services.ingestion.credentials import reveal
from api.services.ingestion.restPull import IngestionError, extract
from utils.logger import logger
from utils.projectStorage import readBytes
from utils.storedTables import writeStoredTableToPath


def _mappedTable(rows, columns):
    arrowTypes = {
        "string": pa.string(), "integer": pa.int64(), "number": pa.float64(), "boolean": pa.bool_(),
        "date": pa.date32(), "timestamp": pa.timestamp("us", tz="UTC"),
    }
    fields, arrays = [], []
    for column in columns:
        name, kind = column["name"], column["type"]
        values = [row[name] for row in rows]
        if kind == "date":
            values = [None if value is None else date.fromisoformat(value) for value in values]
        elif kind == "timestamp":
            values = [None if value is None else datetime.fromisoformat(value) for value in values]
        fields.append(pa.field(name, arrowTypes[kind], nullable=column.get("nullable", False)))
        arrays.append(pa.array(values, type=arrowTypes[kind]))
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


def _stage(projectId, logicalName, objectName, table):
    bucket = client.storage.from_("AnalyticsHub")
    with TemporaryDirectory(prefix="nubrix-api-ingestion-") as directory:
        path = Path(directory) / "snapshot.parquet"
        writeStoredTableToPath(table, projectId, logicalName, str(path))
        with path.open("rb") as file:
            digest = file_digest(file, "sha256").hexdigest()
        bucket.upload(file=str(path), path=f"{projectId}/{objectName}",
                      file_options={"content-type": "application/octet-stream", "upsert": "false"})
        if sha256(readBytes(projectId, objectName)).hexdigest() != digest:
            raise IngestionError("SNAPSHOT_VERIFICATION_FAILED", retryable=True)
    return digest


def _projectCanSync(projectId):
    owners = client.table("Projects").select("ownerUserId").eq("projectId", projectId).limit(1).execute().data
    if not owners:
        return False
    ownerId = owners[0]["ownerUserId"]
    users = client.table("Users").select("isBanned").eq("userId", ownerId).limit(1).execute().data
    return bool(users and not users[0].get("isBanned") and productAccessCache.check(ownerId))


def executeRun(runId):
    lease = repository.claimRun(runId)
    if not lease:
        return
    try:
        secret = reveal(lease["source_id"], lease["credential"])
    except ValueError:
        repository.finishAttempt(lease, "CREDENTIAL_UNAVAILABLE", needsAttention=True)
        return
    try:
        if not _projectCanSync(lease["project_id"]):
            repository.finishAttempt(lease, "PROJECT_ACCESS_REVOKED")
            return
        config = lease["config"]
        rows = extract(config, secret)
        table = _mappedTable(rows, config["columns"])
        versionId = str(uuid4())
        objectName = repository.snapshotName(lease, versionId)
        with repository.projectStorageLock(lease["project_id"]):
            if not repository.recordStaging(lease, versionId):
                return
            digest = _stage(lease["project_id"], lease["table_name"], objectName, table)
            # Keep the receipt if the outcome of the publication commit is uncertain.
            if not repository.publishSnapshot(lease, versionId, table.num_rows, digest):
                _removeSnapshot(lease["project_id"], objectName)
                repository.recordCleanup({"version_id": versionId, "kind": "staged"})
    except IngestionError as error:
        repository.finishAttempt(lease, error.code, retryable=error.retryable, retryAfter=error.retryAfter,
                                 needsAttention=error.code == "AUTH_FAILED")
        return
    except Exception as error:
        logger.warning("API ingestion run {} could not stage snapshot ({})", runId, type(error).__name__)
        repository.finishAttempt(lease, "SNAPSHOT_FAILED", retryable=True)
        return

def _removeSnapshot(projectId, objectName):
    # Reuse storage path validation before passing a path to the bucket API.
    from utils.projectStorage import objectUrl

    objectUrl(projectId, objectName)
    client.storage.from_("AnalyticsHub").remove([f"{projectId}/{objectName}"])


def collectSnapshots():
    for candidate in repository.cleanupCandidates():
        try:
            _removeSnapshot(candidate["project_id"], candidate["object_name"])
            repository.recordCleanup(candidate)
        except Exception as error:
            logger.warning("API snapshot cleanup failed ({})", type(error).__name__)
