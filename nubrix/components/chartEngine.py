"""
chartEngine.py

Turns a ChartRequest into chart JSON: checks the SQL, runs it in the sandbox, builds the chart from the rows.
A failure at any step raises `ChartFailure` with the stage it happened at and a message written for a repair
prompt. A busy sandbox raises `SandboxBusyError` unchanged, as it says nothing about the request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Literal

from nubrix.components.chartBuilder import ChartBuildError, buildChart
from nubrix.components.chartRequest import ChartRequest
from nubrix.components.sqlCheck import SqlCheckError, checkSql
from utils.sqlRunner import ChartRunError, runChartQuery

if TYPE_CHECKING:
    from utils.codeExecutor import REPLManager


class ChartFailure(Exception):
    """A chart that could not be made. `stage` says where; `message` is what to tell the model."""

    def __init__(self, stage: Literal["model", "check", "bind", "run", "build", "timeout"], message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message


def renderChartRequest(
    manager: REPLManager,
    projectId: str,
    request: ChartRequest,
    activeTables: Iterable[str],
    filters: list | None = None,
    lenient: bool = False,
    waitForSlot: bool = True,
) -> dict:
    """Check, run and build one chart. Nothing runs when the check fails."""
    try:
        checked = checkSql(request.sql, activeTables)
    except SqlCheckError as error:
        raise ChartFailure("check", str(error)) from error
    try:
        result = runChartQuery(manager, projectId, checked, request, filters, waitForSlot=waitForSlot)
    except ChartRunError as error:
        raise ChartFailure(error.stage, error.message) from error
    try:
        return buildChart(request, result.table, result.truncated, lenient)
    except ChartBuildError as error:
        raise ChartFailure("build", str(error)) from error
