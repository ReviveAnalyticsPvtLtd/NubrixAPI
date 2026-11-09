"""
sqlRunner.py — the API side of a SQL chart run.

`runChartQuery` hands one checked query to the sandbox (`utils/sql_launcher.py`) through the project's slot
and returns the rows it wrote. Every way a run can fail becomes a `ChartRunError` with a stage and a message
written for a repair prompt. A busy sandbox is not a failure of the query, so `SandboxBusyError` passes through.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import pyarrow as pa

from nubrix.components.chartRequest import ChartRequest, requiredColumns, rowLimit
from nubrix.components.sqlCheck import CheckedSql
from utils.sandboxRunner import ResultTooLargeError, SandboxResult

if TYPE_CHECKING:
    from utils.codeExecutor import REPLManager

_REPLY_STAGES = ("bind", "run")


@dataclass(frozen=True)
class QueryResult:
    table: pa.Table
    truncated: bool


class ChartRunError(RuntimeError):
    """A SQL run that did not give rows. `stage` says where it stopped; `message` is what to tell the model."""

    def __init__(self, stage: Literal["bind", "run", "timeout"], message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message


def _firstLine(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _reply(result: SandboxResult) -> dict:
    """The one JSON line the launcher printed, or a ChartRunError when it printed none."""
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        reply = json.loads(lines[-1]) if lines else None
    except ValueError:
        reply = None
    if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
        raise ChartRunError("run", "The query run returned no readable answer.")
    if not reply["ok"] and (reply.get("stage") not in _REPLY_STAGES or not isinstance(reply.get("message"), str)):
        raise ChartRunError("run", "The query run returned no readable answer.")
    return reply


def _readTable(resultBytes: bytes | None) -> pa.Table:
    if resultBytes is None:
        raise ChartRunError("run", "The query run wrote no result.")
    try:
        return pa.ipc.open_file(pa.BufferReader(resultBytes)).read_all()
    except pa.ArrowException as error:
        raise ChartRunError("run", "The query run wrote a result that could not be read.") from error


def runChartQuery(manager: REPLManager, projectId: str, checked: CheckedSql, request: ChartRequest, filters: list | None, waitForSlot: bool = True) -> QueryResult:
    """Run a checked query in the sandbox. Raises ChartRunError, or SandboxBusyError when no slot is free."""
    childRequest = {
        "sql": checked.sql,
        "tables": list(checked.tables),
        "filters": filters,
        "requiredColumns": requiredColumns(request),
        "rowLimit": rowLimit(request.chartType),
    }
    try:
        result = manager.runSql(childRequest, projectId, waitForSlot=waitForSlot)
    except ResultTooLargeError as error:
        raise ChartRunError("run", "The query result is too large to return. Select fewer columns or rows, or aggregate.") from error
    if result.stopped:
        raise ChartRunError("timeout" if "timed out" in result.stopped else "run", result.stopped)
    if result.returncode != 0:
        raise ChartRunError("run", _firstLine(result.stderr) or f"The query run exited with code {result.returncode}.")
    reply = _reply(result)
    if not reply["ok"]:
        raise ChartRunError(reply["stage"], reply["message"])
    return QueryResult(_readTable(result.resultBytes), bool(reply.get("truncated")))
