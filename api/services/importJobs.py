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
_KPI_STATUSES = {"waiting", "running", "done", "skipped", "failed"}
_FINAL_STATES = {"done", "failed"}
# Only these fields can be written, so credentials cannot reach the record.
_TABLE_FIELDS = {"status", "rows", "reason"}


class ImportJobs:
    """Tracks the one running import of each project and its per-table progress."""

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

    def _read(self, projectId: str) -> dict | None:
        raw = self._redis.get(self._key(projectId))
        return None if raw is None else json.loads(raw)

    def _write(self, projectId: str, record: dict) -> None:
        self._redis.set(self._key(projectId), json.dumps(record))
        self._redis.expire(self._key(projectId), _RECORD_SECONDS)

    def start(self, projectId: str, tables: list[tuple[str, str]]) -> str | None:
        """Take the project's lock and write a fresh record; None when an import is running."""
        if not self._redis.set(self._lockKey(projectId), "1", nx=True, ex=_LOCK_SECONDS):
            return None
        jobId = uuid.uuid4().hex
        self._write(projectId, {
            "jobId": jobId,
            "state": "running",
            "tables": [
                {"name": name, "storedAs": storedAs, "status": "pending", "rows": None, "reason": None}
                for name, storedAs in tables
            ],
            "kpis": "waiting",
        })
        return jobId

    def get(self, projectId: str) -> dict | None:
        """Return the record; a running one whose lock has expired is reported as failed."""
        record = self._read(projectId)
        if record and record["state"] == "running" and not self._redis.exists(self._lockKey(projectId)):
            record["state"] = "failed"
        return record

    def setTable(self, projectId: str, storedAs: str, **fields) -> None:
        if not fields.keys() <= _TABLE_FIELDS:
            raise ValueError(f"unknown table fields: {sorted(fields.keys() - _TABLE_FIELDS)}")
        if "status" in fields and fields["status"] not in _TABLE_STATUSES:
            raise ValueError(f"unknown table status: {fields['status']!r}")
        record = self._read(projectId)
        for table in record["tables"]:
            if table["storedAs"] == storedAs:
                table.update(fields)
                break
        else:
            raise ValueError(f"unknown table: {storedAs!r}")
        self._write(projectId, record)

    def setKpis(self, projectId: str, status: str) -> None:
        if status not in _KPI_STATUSES:
            raise ValueError(f"unknown kpis status: {status!r}")
        record = self._read(projectId)
        record["kpis"] = status
        self._write(projectId, record)

    def refreshLock(self, projectId: str) -> None:
        """Extend the lock while the job lives; never recreates one that is gone."""
        if self._redis.exists(self._lockKey(projectId)):
            self._redis.expire(self._lockKey(projectId), _LOCK_SECONDS)

    def finish(self, projectId: str, state: str) -> None:
        if state not in _FINAL_STATES:
            raise ValueError(f"unknown final state: {state!r}")
        record = self._read(projectId)
        record["state"] = state
        self._write(projectId, record)
        self._redis.delete(self._lockKey(projectId))


importJobs = ImportJobs()
