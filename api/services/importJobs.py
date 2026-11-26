"""
importJobs.py

Progress record of a database import, one per project, kept in Redis.
"""

__all__ = ["ImportJobs", "importJobs"]

import json
import os
import uuid

import redis

_LOCK_SECONDS = 21600
_RECORD_SECONDS = 604800
_TABLE_STATUSES = {"pending", "loading", "loaded", "skipped", "failed"}
_FINAL_STATES = {"done", "failed"}
# Only these fields can be written, so credentials cannot reach the record.
_TABLE_FIELDS = {"status", "rows", "reason"}


class ImportJobs:
    """Tracks the one running import of each project and its per-table progress.

    The lock value is the jobId, and every write names its jobId, so a stalled job
    whose lock ran out cannot change the record or lock of a newer job.
    """

    def __init__(self, redisClient=None):
        self._client = redisClient

    @property
    def _redis(self):
        """Build the default client on first use, so importing needs no Redis settings."""
        if self._client is None:
            self._client = redis.Redis(
                host=os.environ["REDIS_HOST"],
                port=int(os.environ["REDIS_PORT"]),
                password=os.environ["REDIS_PASSWORD"],
            )
        return self._client

    @staticmethod
    def _key(projectId: str) -> str:
        return f"import:{projectId}"

    def _lockKey(self, projectId: str) -> str:
        return f"{self._key(projectId)}:running"

    @staticmethod
    def keys(projectId: str) -> list[str]:
        """Every key of the project's import: the record, the lock, and the step marker and pointer of databaseImportService."""
        key = ImportJobs._key(projectId)
        return [key, f"{key}:running", f"{key}:batch", f"{key}:next"]

    def forget(self, projectId: str) -> None:
        """Remove the project's import; a running job's next write is refused, so it stops."""
        self._redis.delete(*self.keys(projectId))

    def _read(self, projectId: str) -> dict | None:
        raw = self._redis.get(self._key(projectId))
        return None if raw is None else json.loads(raw)

    def _write(self, projectId: str, record: dict, existing: bool = True) -> bool:
        """False when `existing` and the record is gone: a forgotten project's record is never written back."""
        options = {"xx": True} if existing else {}
        return bool(self._redis.set(self._key(projectId), json.dumps(record), ex=_RECORD_SECONDS, **options))

    def _owned(self, projectId: str, jobId: str) -> dict | None:
        """The record, only if it belongs to jobId."""
        record = self._read(projectId)
        return record if record and record["jobId"] == jobId else None

    def _holdsLock(self, projectId: str, jobId: str) -> bool:
        value = self._redis.get(self._lockKey(projectId))
        return value is not None and value.decode() == jobId

    def start(self, projectId: str, tables: list[tuple[str, str]]) -> str | None:
        """Take the project's lock and write a fresh record; None when an import is running."""
        jobId = uuid.uuid4().hex
        if not self._redis.set(self._lockKey(projectId), jobId, nx=True, ex=_LOCK_SECONDS):
            return None
        try:
            self._write(projectId, {
                "jobId": jobId,
                "state": "running",
                "tables": [
                    {"name": name, "storedAs": storedAs, "status": "pending", "rows": None, "reason": None}
                    for name, storedAs in tables
                ],
            }, existing=False)
        except Exception:
            self._redis.delete(self._lockKey(projectId))
            raise
        return jobId

    def get(self, projectId: str) -> dict | None:
        """Return the record; a running one whose lock has expired is reported as failed."""
        record = self._read(projectId)
        if record and record["state"] == "running" and not self._redis.exists(self._lockKey(projectId)):
            record = self._read(projectId)  # a finish may have landed meanwhile
            if record and record["state"] == "running":
                record["state"] = "failed"
        return record

    def setTable(self, projectId: str, jobId: str, storedAs: str, **fields) -> bool:
        """False, changing nothing, when jobId does not own the record."""
        if not fields.keys() <= _TABLE_FIELDS:
            raise ValueError(f"unknown table fields: {sorted(fields.keys() - _TABLE_FIELDS)}")
        if "status" in fields and fields["status"] not in _TABLE_STATUSES:
            raise ValueError(f"unknown table status: {fields['status']!r}")
        record = self._owned(projectId, jobId)
        if record is None:
            return False
        for table in record["tables"]:
            if table["storedAs"] == storedAs:
                table.update(fields)
                break
        else:
            raise ValueError(f"unknown table: {storedAs!r}")
        return self._write(projectId, record)

    def refreshLock(self, projectId: str, jobId: str) -> bool:
        """Extend the lock only while jobId still holds it."""
        if not self._holdsLock(projectId, jobId):
            return False
        self._redis.expire(self._lockKey(projectId), _LOCK_SECONDS)
        return True

    def finish(self, projectId: str, jobId: str, state: str) -> bool:
        if state not in _FINAL_STATES:
            raise ValueError(f"unknown final state: {state!r}")
        record = self._owned(projectId, jobId)
        if record is None:
            return False
        record["state"] = state
        if not self._write(projectId, record):
            return False
        if self._holdsLock(projectId, jobId):
            self._redis.delete(self._lockKey(projectId))
        return True


importJobs = ImportJobs()
