"""The verified conversion of saved dashboard widgets: a one-off operator job, run only with its own approval.

    python -m scripts.convertSavedWidgets --project ID --out REPORT            (dry run: writes nothing)
    python -m scripts.convertSavedWidgets --project ID --out REPORT --write
    python -m scripts.convertSavedWidgets ... --model-stub STUB.jsonl          (tests: recorded model replies)

It reads the project's `dashboardConfig.json` and takes every saved widget whose `generatedCode` is model-written
Python. For each one, one at a time, it asks the SQL chart workflow for a chart request, describing the chart by the
widget's stored `query` or, without one, by its Python code, against the project's metadata as a served request sees it
(`utils.modelMetadata`). The request carries the widget's saved title, because a refresh shows the request's title.

A widget is converted only when the job can show that users would see no change, now and later. Each step below that
fails leaves the widget as Python, with a reason:
  1. Before any model call: the saved title fits a request (`title`) and is a literal in the Python (`dynamicTitle`),
     and the Python does not read the clock (`relativeTime`) or draw random numbers (`nondeterministic`).
  2. The SQL is refused (`sqlRefusal`) when it cuts the result (LIMIT, OFFSET, FETCH, a sample, QUALIFY, a ranking
     window, an 'Other' fold: `truncated`), moves with time (`relativeTime`) or is not repeatable (`nondeterministic`).
  3. The refresh-style run (unfiltered, lenient, through `renderChartRequest`) and the workflow's own run must both
     strictly match the saved widget (`verdictOf`; a second render that differs is `nondeterministic`).
  4. The result must be whole (`resultRefusal`): no launcher or builder cap reached or met (`truncated`), and with more
     than one row an outer ORDER BY on output columns whose key values are unique across the rows (`unstableOrder`).
  5. Under every filter set (`_filterSets`), the saved Python and the request re-run through the dashboard's own filter
     path (`DashboardService._rerunWidgetProduced`) must strictly agree (`filter`); no filter set at all is `filter`.
Only a converted widget's `generatedCode` changes; its title, layout, id and saved data stay as they are.

Candidates: a widget whose `decodeGeneratedCode` is None, that is not a `panelChart(...)` template, not a
`# Failed to generate code` card, and has code. Every other widget is skipped with a reason. A doubt, a failure, a
timeout or a busy sandbox is that widget's verdict and leaves it as it is. A sandbox slot is waited for as a served
request waits.

`--write` changes the live config only when at least one widget matched, in this order:
  1. the project row's `modifiedAt` is read (read only) before the config is read;
  2. the config's exact bytes were kept when it was read;
  3. `dashboardConfig.backup-<UTC time>.json` is uploaded with those bytes (never over an existing file), read back
     and compared byte for byte; only then is it named as the backup;
  4. the live config is read again and must still be those bytes, else the dashboard was edited meanwhile;
  5. the new config, written with the service's JSON settings, is uploaded and read back;
  6. `modifiedAt` is read again: a change means the project was edited during the job (`possibleConcurrentEdit`).
A failure at 3 or 4 aborts with the live config untouched. A failure at 5 is never restored automatically: the live
config is read once more and reported as `writeLanded`, `writeNotApplied`, `changedBySomeoneElse` or `unreadable`.

Nothing is charged and nothing is traced: the workflow runs with no callbacks, and Langfuse is off for the whole job
even when its keys are set. No `.env` file is read. Redis is cut off for the run (`scripts.chartEval._noRedis`): table
and schema reads go to storage, and each table's schema is read once per job (`_memoisedSchemas`). A dry run uploads
nothing.

Standard output holds only the counts and the backup name. The report (`--out`, a local JSON file) holds every
widget's page, id and verdict: `match` with the new chart request, `differ` with the first differing field or the
refusal, `failed` with its step and stage, or `skipped` with its reason, plus the filter sets checked, the backup name
and the counts. Filter values and differing fields can hold data, so they stay in the local report.

Exit code: 0 when the job finished (also when nothing matched); 1 when it aborted (the project row or the config could
not be read, the backup could not be verified, the dashboard was edited meanwhile, or the write could not be verified)
or when the write was verified but the project changed during the job; 2 for a usage error (nothing was read).
"""

__all__ = ["main", "orderKeys", "resultRefusal", "sqlRefusal", "verdictOf", "visibleDifference"]

import argparse
import ast
import contextlib
import copy
import datetime as dt
import json
import math
import os
import re
import sys
import time
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pyarrow as pa
import sqlglot
from pydantic import ValidationError
from sqlglot import exp

from nubrix.components.chartCompare import compareCharts
from nubrix.components.chartEngine import ChartFailure, renderChartRequest
from nubrix.components.chartRequest import (
    CATEGORY_TYPES,
    LABEL_CAP,
    ChartRequest,
    decodeGeneratedCode,
    encodeGeneratedCode,
    rowLimit,
)
from scripts import chartEval
from utils.mapPointLabels import ensureScatterMapPointLabels

CONFIG = "dashboardConfig.json"
METADATA = "metadata.json"
REL_TOL = 1e-9
FAILED_CODE_PREFIX = "# Failed to generate code"
# The chart keys a refresh of a saved chart request replaces (`dashboardService._CHART_KEYS`), title aside: the
# request carries the saved title. `columns` comes before `data` so a table's column order is named first.
VISIBLE_KEYS = ("chartType", "xLabels", "yLabels", "label", "columns", "data", "map")
# Chart types whose numbers the client draws as they are, so any float difference can show (see `_sameScalar`).
EXACT_FLOAT_TYPES = ("scatter", "bubble", "radar", "geoMap")
_PANEL_DEF = re.compile(r"^\s*def\s+panelChart\s*\(", re.MULTILINE)
_DETAIL_CHARS = 2000
# Python that reads the clock or draws random numbers shows a different chart on another day or run.
_PYTHON_CLOCK = re.compile(
    r"\b(now|today|utcnow|fromtimestamp|utcfromtimestamp|localtime|gmtime|time|monotonic|perf_counter)\s*\("
    r"|(['\"])\s*(now|today|yesterday|tomorrow)\s*\2"
    r"|\bcurrent_(date|timestamp|time)\b",
    re.IGNORECASE,
)
_PYTHON_RANDOM = re.compile(r"\brandom\b|\.sample\s*\(|\bshuffle\s*\(|\buuid\d?\s*\(", re.IGNORECASE)
# SQL names that read the clock. Compared on tokens that are not strings or quoted identifiers.
_SQL_CLOCK = frozenset({
    "current_date", "current_time", "current_timestamp", "current_localtime", "current_localtimestamp", "now", "today",
    "localtime", "localtimestamp", "get_current_time", "get_current_timestamp", "transaction_timestamp",
    "statement_timestamp", "clock_timestamp", "sysdate", "getdate", "current_datetime",
})
_SQL_RANKING = frozenset({"row_number", "rank", "dense_rank", "ntile", "percent_rank", "cume_dist"})
_SQL_NONDETERMINISTIC = frozenset({
    "rand", "random", "uuid", "gen_random_uuid", "setseed", "any_value", "arbitrary", "first", "last", "string_agg",
    "group_concat", "listagg", "list", "array_agg", "approx_count_distinct", "approx_distinct", "approx_quantile",
    "reservoir_quantile", "histogram",
})
_FOLD_LABELS = frozenset({"other", "others"})
_COMPARISONS = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ, exp.Between)
_TEXT_COLUMNS_PROBED = 20


class _UsageError(Exception):
    """A mistake in the arguments: exit code 2, nothing was read."""


class _Abort(Exception):
    """The job stops with exit code 1. `reason` is a fixed word for the report and standard error."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# The strict comparison


def _isNumber(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _shown(value: float) -> tuple:
    """
    What the client shows for a float: `toLocaleString` with at most 3 decimals.

    The client formats every number this way outside scatter, bubble and radar: client `chartsConfig.ts:280-281`,
    `:438` and `:504` (axis and tooltip values), `Card.tsx:9-13`, `Table.tsx:48-52` and `Pivot.tsx:266`. Radar
    (`chartsConfig.ts:580-584`, built at `:540`), scatter (`:637-643`, `:601`) and bubble (`:718-726`, `:659`) draw the
    raw number, which is why those compare exactly. `Intl.NumberFormat` rounds half away from zero; whether it rounds
    the double's exact value or its shortest decimal form, two floats are taken as shown alike only when both readings
    agree, and when they carry the same sign (`-0` is shown).
    """
    step = Decimal("0.001")
    with localcontext() as context:
        context.prec = 800
        exact = Decimal(value).quantize(step, rounding=ROUND_HALF_UP)
        shortest = Decimal(repr(value)).quantize(step, rounding=ROUND_HALF_UP)
    return exact, shortest, math.copysign(1.0, value) < 0


def _sameScalar(a: Any, b: Any, relTol: float, exactFloats: bool = False) -> bool:
    """
    Integers exactly; other numbers exactly when `exactFloats`, else within `relTol` AND shown alike (`_shown`).

    A bool is never a number; NaN and infinity never match.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _isNumber(a) or _isNumber(b):
        if not (_isNumber(a) and _isNumber(b)) or not (math.isfinite(a) and math.isfinite(b)):
            return False
        if isinstance(a, int) or isinstance(b, int):
            # An integer is exact: 20 equals 20.0, never 20.000000001.
            return float(a).is_integer() and float(b).is_integer() and int(a) == int(b)
        if exactFloats:
            return a == b and math.copysign(1.0, a) == math.copysign(1.0, b)
        return math.isclose(a, b, rel_tol=relTol, abs_tol=0.0) and _shown(a) == _shown(b)
    if a is None or b is None:
        return a is None and b is None
    return type(a) is type(b) and a == b


def _difference(a: Any, b: Any, path: str, relTol: float, orderedKeys: bool, exactFloats: bool = False) -> str | None:
    """The path of the first difference, or None. Lists compare in order; dicts by their keys, in order when `orderedKeys`."""
    if isinstance(a, dict) or isinstance(b, dict):
        if not (isinstance(a, dict) and isinstance(b, dict)):
            return path
        keysA, keysB = list(a), list(b)
        if (keysA != keysB) if orderedKeys else (set(keysA) != set(keysB)):
            return path
        for key in keysA:
            found = _difference(a[key], b[key], f"{path}.{key}", relTol, orderedKeys, exactFloats)
            if found:
                return found
        return None
    if isinstance(a, list) or isinstance(b, list):
        if not (isinstance(a, list) and isinstance(b, list)) or len(a) != len(b):
            return path
        for index, (x, y) in enumerate(zip(a, b)):
            found = _difference(x, y, f"{path}[{index}]", relTol, orderedKeys, exactFloats)
            if found:
                return found
        return None
    return None if _sameScalar(a, b, relTol, exactFloats) else path


def _asShown(chart: dict) -> dict:
    """The chart as the dashboard returns it: a geoMap's empty point labels are filled (`getData` does this)."""
    shown = copy.deepcopy(chart)
    ensureScatterMapPointLabels(shown)
    return shown


def _tableColumns(chart: dict) -> list | None:
    """A table's column order: its `columns`, else the keys of its first row (what a table without `columns` shows)."""
    columns = chart.get("columns")
    if columns is not None:
        return columns
    rows = chart.get("data")
    if isinstance(rows, list):
        return list(rows[0]) if rows and isinstance(rows[0], dict) else []
    return None


def visibleDifference(saved: dict, new: dict, relTol: float = REL_TOL) -> str | None:
    """
    The first user-visible chart field where the new chart differs from the saved widget, or None.

    Compared, in this order, as both are shown (map point labels filled): `chartType`, `xLabels`, `yLabels`,
    `label`, `columns`, `data`, `map`. A key that is missing equals a key that is None. Labels, datasets, table rows
    and map points compare in order; every key of a dataset or a point must be on both sides with an equal value; a
    table's rows and a pivot's keys also keep their key order. A table's `columns` is its column order: `columns`
    when it has one, else its first row's keys. Integers are exact. Floats are exact for scatter, bubble, radar and
    geoMap, which the client draws as they are, and otherwise must be within `relTol` and shown alike by the client
    (`_shown`). A bool is never a number, NaN and infinity never match, and text is exact. The title, id, layout and
    keys a refresh does not replace are not compared.

    Returns:
        str | None: A path such as `data.datasets[0].data[3]` or `xLabels`. It can name a table column or a pivot
            key, so it belongs in the local report only.
    """
    saved, new = _asShown(saved), _asShown(new)
    chartType = saved.get("chartType")
    exactFloats = chartType in EXACT_FLOAT_TYPES or new.get("chartType") in EXACT_FLOAT_TYPES
    for key in VISIBLE_KEYS:
        a, b = saved.get(key), new.get(key)
        if key == "columns" and chartType == "table":
            a, b = _tableColumns(saved), _tableColumns(new)
        found = _difference(a, b, key, relTol, key == "data" and chartType in ("table", "pivot"), exactFloats)
        if found:
            return found
    return None


def verdictOf(saved: dict, new: dict) -> tuple[str, str | None]:
    """("match", None) when users would see no change, else ("differ", the first differing field)."""
    field = visibleDifference(saved, new, REL_TOL)
    if field is not None:
        return "differ", field
    if compareCharts(saved, new, relTol=REL_TOL) != "match":
        return "differ", "compareCharts"
    return "match", None


# Refusals: what a single unfiltered match cannot prove


def _parsed(sql: str) -> exp.Expression | None:
    try:
        statements = [statement for statement in sqlglot.parse(sql, read="duckdb") if statement is not None]
    except Exception:
        return None
    return statements[0] if len(statements) == 1 else None


def _functionName(node: exp.Expression) -> str:
    return (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()


def sqlRefusal(sql: str) -> str | None:
    """
    Why a request's SQL cannot be trusted to show the same chart later, or None.

    - `relativeTime`: it calls a clock function (`current_date`, `now()`, `today()`, ...), or compares against an
      expression with an INTERVAL in it: the window moves.
    - `truncated`: it cuts the result (LIMIT, OFFSET, FETCH, a sample, QUALIFY, a ranking window function) or folds
      labels into an 'Other' bucket: the cut moves when the data changes, and Python may not cut the same way.
    - `nondeterministic`: it calls a function whose answer can change between runs (random, any_value, string_agg,
      array_agg, approximations, ...).
    - `unparsable`: it is not one statement sqlglot can read.
    """
    tree = _parsed(sql)
    if tree is None:
        return "unparsable"
    try:
        tokens = sqlglot.Dialect.get_or_raise("duckdb").tokenize(sql)
    except Exception:
        return "unparsable"
    plain = (sqlglot.TokenType.STRING, sqlglot.TokenType.IDENTIFIER)
    if any(token.token_type not in plain and token.text.lower() in _SQL_CLOCK for token in tokens):
        return "relativeTime"
    functions = list(tree.find_all(exp.Func))
    if any(_functionName(node) in _SQL_CLOCK for node in functions) or tree.find(
        exp.CurrentDate, exp.CurrentTimestamp, exp.CurrentTime
    ):
        return "relativeTime"
    for interval in tree.find_all(exp.Interval):
        if interval.find_ancestor(*_COMPARISONS) is not None:
            return "relativeTime"
    if tree.find(exp.Limit, exp.Fetch, exp.Offset, exp.TableSample, exp.Qualify):
        return "truncated"
    for window in tree.find_all(exp.Window):
        if isinstance(window.this, exp.Func) and _functionName(window.this) in _SQL_RANKING:
            return "truncated"
    if any(_functionName(node) in _SQL_RANKING for node in functions):
        return "truncated"
    for literal in tree.find_all(exp.Literal):
        if literal.is_string and literal.this.strip().lower() in _FOLD_LABELS:
            return "truncated"
    if any(_functionName(node) in _SQL_NONDETERMINISTIC for node in functions):
        return "nondeterministic"
    return None


def orderKeys(sql: str, columns: list[str]) -> list[str] | None:
    """
    The output columns the outer ORDER BY sorts on, in order, or None when it has none or one is not an output column.

    A key counts when it is an output position (`ORDER BY 2`), an unqualified name of an output column, or the same
    expression as an unaliased select-list item (`ORDER BY SUM(x)` with `SUM(x) AS total` gives `total`). Anything
    else (an expression of other columns, a position out of range, a name two output columns share) gives None.
    """
    tree = _parsed(sql)
    while isinstance(tree, exp.Subquery) and not tree.alias:
        tree = tree.this
    if tree is None:
        return None
    order = tree.args.get("order")
    if order is None or not order.expressions:
        return None
    selects = list(tree.selects) if hasattr(tree, "selects") else []
    comparable = len(selects) == len(columns) and not any(isinstance(item, exp.Star) or item.find(exp.Star) for item in selects)
    keys = []
    for ordered in order.expressions:
        key = ordered.this
        name = None
        if isinstance(key, exp.Literal) and not key.is_string:
            try:
                position = int(key.this)
            except ValueError:
                return None
            if 1 <= position <= len(columns):
                name = columns[position - 1]
        elif isinstance(key, exp.Column) and not key.table and key.name in columns:
            name = key.name
        elif comparable:
            matches = [index for index, item in enumerate(selects) if item.unalias() == key]
            if len(matches) == 1:
                name = columns[matches[0]]
        if name is None or columns.count(name) != 1:
            return None
        keys.append(name)
    return keys


def _hashable(value: Any) -> Any:
    try:
        hash(value)
        return value
    except TypeError:
        return json.dumps(value, sort_keys=True, default=str)


def resultRefusal(request: ChartRequest, chart: dict, table: pa.Table, truncated: bool) -> str | None:
    """
    Why the unfiltered result cannot stand for every later refresh, or None.

    - `truncated`: the launcher cut the rows, the row count meets the chart type's launcher cap (`rowLimit`: 5000 for
      category charts, table 1000, geoMap 2000, scatter and bubble 5000, pivot 10000, card 2), a category chart meets
      the builder's label cap (`LABEL_CAP`, 500; lenient keeps the first 500), or the builder dropped rows (scatter and
      bubble points with an empty value, map points with an empty or out-of-range coordinate).
    - `unstableOrder`: more than one row, and no outer ORDER BY whose keys are output columns with unique values.
    """
    chartType = request.chartType
    rows = table.num_rows
    if truncated or rows >= rowLimit(chartType):
        return "truncated"
    data = chart.get("data")
    if chartType in CATEGORY_TYPES:
        labels = data.get("labels") if isinstance(data, dict) else None
        if not isinstance(labels, list) or len(labels) >= LABEL_CAP:
            return "truncated"
    elif chartType in ("scatter", "bubble"):
        datasets = data.get("datasets") if isinstance(data, dict) else None
        if not isinstance(datasets, list) or sum(len(dataset.get("data") or []) for dataset in datasets) != rows:
            return "truncated"
    elif chartType == "geoMap":
        points = ((chart.get("map") or {}).get("data") or {}).get("points")
        if not isinstance(points, list) or len(points) != rows:
            return "truncated"
    if rows > 1:
        keys = orderKeys(request.sql, list(table.column_names))
        if not keys:
            return "unstableOrder"
        values = [table.column(table.column_names.index(key)).to_pylist() for key in keys]
        seen = {tuple(_hashable(value) for value in row) for row in zip(*values)}
        if len(seen) != rows:
            return "unstableOrder"
    return None


def _withoutFences(code: str) -> str:
    return "\n".join(line for line in code.splitlines() if not line.strip().startswith("```"))


def _titleIsLiteral(code: str, title: str) -> bool:
    """The saved title is a whole string literal in the Python, so the Python shows that title on every run."""
    body = _withoutFences(code)
    try:
        tree = ast.parse(body)
    except (SyntaxError, ValueError):
        return json.dumps(title) in body or repr(title) in body
    formatted = {id(part) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for part in node.values}
    return any(
        isinstance(node, ast.Constant) and node.value == title and id(node) not in formatted for node in ast.walk(tree)
    )


def _pythonRefusal(widget: dict) -> str | None:
    """Why a widget cannot be converted, known from the widget alone: no model call is spent on it."""
    title = widget.get("title")
    if _withTitle(_TITLE_PROBE, title) is None:
        # A refresh would show the request's title, and no request can hold this one.
        return "title"
    code = widget["generatedCode"]
    if not _titleIsLiteral(code, title):
        return "dynamicTitle"
    if _PYTHON_CLOCK.search(code):
        return "relativeTime"
    if _PYTHON_RANDOM.search(code):
        return "nondeterministic"
    return None


# Candidates


def _isPanelTemplate(code: str) -> bool:
    """A manual Panel widget: its code defines and calls `panelChart` at the top level (fences allowed)."""
    body = _withoutFences(code)
    try:
        tree = ast.parse(body)
    except (SyntaxError, ValueError):
        return bool(_PANEL_DEF.search(body))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "panelChart":
            return True
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) \
                and node.value.func.id == "panelChart":
            return True
    return False


def _skipReason(widget: Any) -> str | None:
    """Why a widget is not a candidate, or None for a model-written Python widget."""
    if not isinstance(widget, dict):
        return "notAWidget"
    code = widget.get("generatedCode")
    if not isinstance(code, str) or not code.strip():
        return "emptyCode"
    try:
        if decodeGeneratedCode(code) is not None:
            return "chartRequest"
    except ValueError:
        # The marker is there with a version or body this code does not read: it is not Python either.
        return "chartRequest"
    if code.lstrip().startswith(FAILED_CODE_PREFIX):
        return "failedCard"
    if _isPanelTemplate(code):
        return "panelChart"
    return None


def _description(widget: dict) -> str:
    query = widget.get("query")
    return query if isinstance(query, str) and query.strip() else widget["generatedCode"]


# Generation and the runs


class _Engine(chartEval._Engine):
    """The SQL chart workflow, built like the evaluation harness builds it (with its model stub when given)."""

    def __init__(self, replay):
        super().__init__("sql", replay)

    def invoke(self, metadata: dict, prompt: str, project: str) -> dict:
        """One workflow run with no callbacks: no credit callback and no Langfuse handler."""
        self.failure = None
        state = {"metadata": metadata, "inputQuery": prompt, "projectId": project}
        with mock.patch.object(self.module, "logChartFailure", self._recordFailure):
            return self.workflow.invoke(state, config=None)


def _clipped(value: Any) -> str:
    return str(value)[:_DETAIL_CHARS]


def _failure(error: BaseException) -> dict:
    """A run's failure as `{"stage", "detail"}`: a chart failure's stage, `busy`, or `run`."""
    from utils.sandboxSlots import SandboxBusyError, isBusy

    if isinstance(error, ChartFailure):
        return {"stage": error.stage, "detail": _clipped(error.message)}
    if isinstance(error, SandboxBusyError) or isBusy(error):
        return {"stage": "busy", "detail": _clipped(error)}
    return {"stage": "run", "detail": _clipped(f"{type(error).__name__}: {error}")}


def _generated(engine, metadata: dict, description: str, project: str) -> tuple[ChartRequest, dict] | dict:
    """The chart request the workflow made and the chart its own run showed, or the failure `{"stage", "detail"}`."""
    try:
        response = engine.invoke(metadata, description, project)
    except Exception as error:
        return {"stage": "exception", "detail": _clipped(f"{type(error).__name__}: {error}")}
    output = response.get("finalOutput") if isinstance(response, dict) else None
    if not isinstance(output, dict):
        return {"stage": "noOutput", "detail": None}
    if "error" in output:
        stage = (engine.failure or {}).get("stage") or "error"
        return {"stage": stage, "detail": _clipped(output["error"])}
    if "chartType" not in output:
        stage = "doubt" if "response" in output else "noChart"
        return {"stage": stage, "detail": _clipped(output.get("response"))}
    try:
        request = decodeGeneratedCode(response.get("generatedCode"))
    except (ValueError, ValidationError) as error:
        return {"stage": "noRequest", "detail": _clipped(type(error).__name__)}
    if request is None:
        return {"stage": "noRequest", "detail": None}
    return request, output


def _renderWithResult(manager, project: str, request: ChartRequest, activeTables: frozenset) -> tuple[dict, pa.Table, bool]:
    """
    The chart a dashboard refresh of the saved request would show, with the query result it was built from.

    The run is `renderChartRequest` as a refresh calls it (unfiltered, lenient); the builder call is watched to keep the
    rows and the launcher's `truncated` flag, which `resultRefusal` needs.
    """
    import nubrix.components.chartEngine as chartEngine

    build = chartEngine.buildChart
    seen: dict = {}

    def watched(request, table, truncated=False, lenient=False):
        seen.update(table=table, truncated=bool(truncated))
        return build(request, table, truncated, lenient)

    with mock.patch.object(chartEngine, "buildChart", watched):
        chart = renderChartRequest(manager, project, request, activeTables, filters=None, lenient=True)
    return chart, seen["table"], seen["truncated"]


def _withTitle(request: ChartRequest, title: Any) -> ChartRequest | None:
    """The request carrying the widget's saved title, or None when a request cannot hold that title."""
    if not isinstance(title, str):
        return None
    try:
        return ChartRequest.model_validate({**request.model_dump(exclude_none=True), "title": title})
    except ValidationError:
        return None


# A valid request, only to ask whether a title fits a request before any model call is spent on the widget.
_TITLE_PROBE = ChartRequest.model_validate({"chartType": "card", "title": "t", "sql": "SELECT 1", "encoding": {"value": "v"}})


# Filter spot checks


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _isText(dataType: pa.DataType) -> bool:
    if pa.types.is_dictionary(dataType):
        return _isText(dataType.value_type)
    return pa.types.is_string(dataType) or pa.types.is_large_string(dataType) or (
        hasattr(pa.types, "is_string_view") and pa.types.is_string_view(dataType)
    )


def _isDateLike(dataType: pa.DataType) -> bool:
    return pa.types.is_date32(dataType) or pa.types.is_date64(dataType) or pa.types.is_timestamp(dataType)


def _probe(job, sql: str) -> list[dict]:
    """The rows of a small read-only query over the project's tables, run like a chart query (sandbox, SQL check)."""
    request = ChartRequest.model_validate({"chartType": "table", "title": "probe", "sql": sql, "encoding": {}})
    return renderChartRequest(job.manager, job.project, request, job.tables, filters=None, lenient=True)["data"]


def _schemasOf(job, tables: list[str]) -> dict[str, pa.Schema]:
    from utils.tableSchemas import tableSchemas

    return tableSchemas(job.project, tables, skipUnreadable=True)


def _configFilterSets(job, pageId: str, widget: dict) -> list[list[dict]]:
    """Every filter list the config defines for the widget: its page's, its own, and its provenance's."""
    page = job.config.get(pageId) if isinstance(job.config, dict) else None
    provenance = widget.get("provenance")
    sources = [
        page.get("filters") if isinstance(page, dict) else None,
        widget.get("filters"),
        provenance.get("filters") if isinstance(provenance, dict) else None,
    ]
    sets = []
    for source in sources:
        entries = [entry for entry in source if isinstance(entry, dict) and entry] if isinstance(source, list) else []
        sets.extend([entry] for entry in entries)
        if len(entries) > 1:
            sets.append(entries)
    return sets


def _filterSets(job, pageId: str, widget: dict, request: ChartRequest) -> list[list[dict]]:
    """
    The filter sets a widget is checked under, one set per re-run.

    Every filter list the config defines, then for each table the widget reads (the tables the request's SQL reads,
    then active tables the Python names in a string literal, in name order): one text column at its most frequent
    value, the column with the fewest distinct values (at least two) among the first 20 text columns, columns the
    request or the Python name first; and one date or timestamp column (named ones first) over the middle half of its
    values (the 25th to the 75th percentile). A table with neither costs no query.
    """
    from nubrix.components.sqlCheck import checkSql

    sets: list[list[dict]] = []
    seen: set[str] = set()

    def add(filters: list[dict]) -> None:
        key = json.dumps(filters, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            sets.append(filters)

    for filters in _configFilterSets(job, pageId, widget):
        add(filters)

    code = widget.get("generatedCode") or ""
    tables = list(checkSql(request.sql, job.tables).tables)
    tables += [name for name in sorted(job.tables) if name not in tables and re.search(r"(['\"])" + re.escape(name) + r"\1", code)]
    tree = _parsed(request.sql)
    named = {column.name for column in tree.find_all(exp.Column)} if tree is not None else set()
    schemas = _schemasOf(job, tables)
    for table in tables:
        schema = schemas.get(table)
        if schema is None:
            continue

        def first(field: pa.Field) -> int:
            return 0 if field.name in named or re.search(r"(['\"])" + re.escape(field.name) + r"\1", code) else 1

        text = sorted((field for field in schema if _isText(field.type)), key=first)[:_TEXT_COLUMNS_PROBED]
        dates = sorted((field for field in schema if _isDateLike(field.type)), key=first)[:1]
        if not text and not dates:
            continue
        parts = [f"COUNT(DISTINCT {_quoted(field.name)}) AS d{index}" for index, field in enumerate(text)]
        if dates:
            date = _quoted(dates[0].name)
            parts += [f"quantile_disc({date}, 0.25) AS lo", f"quantile_disc({date}, 0.75) AS hi"]
        [counts] = _probe(job, f"SELECT {', '.join(parts)} FROM {_quoted(table)}")
        distinct = [(counts.get(f"d{index}"), index) for index in range(len(text))]
        usable = [(count, index) for count, index in distinct if isinstance(count, int) and count >= 2]
        if usable:
            column = text[min(usable)[1]].name
            rows = _probe(job, f"SELECT {_quoted(column)} AS v, COUNT(*) AS n FROM {_quoted(table)} "
                               f"WHERE {_quoted(column)} IS NOT NULL GROUP BY 1 ORDER BY n DESC, v LIMIT 1")
            if rows and rows[0].get("v") is not None:
                add([{f"{table}.{column}": rows[0]["v"]}])
        if dates and counts.get("lo") is not None and counts.get("hi") is not None:
            add([{f"{table}.{dates[0].name}": {"min": counts["lo"], "max": counts["hi"]}}])
    return sets


def _rerunUnderFilter(widget: dict, filters: list, projectId: str, activeTables: frozenset) -> tuple[dict, dict | None]:
    """The dashboard's own filtered re-run of one saved widget (`getData` with filters), in the widget's own form."""
    from api.services.dashboardService import DashboardService

    return DashboardService._rerunWidgetProduced(widget, filters, projectId, activeTables)


def _filterCheck(job, pageId: str, widget: dict, request: ChartRequest, entry: dict) -> None:
    """Re-run the Python and the request under each filter set; every pair must show the same chart."""
    try:
        sets = _filterSets(job, pageId, widget, request)
    except Exception as error:
        entry.update(verdict="failed", step="filter", **_failure(error))
        return
    entry["filters"] = sets
    entry["filtersChecked"] = 0
    if not sets:
        # Nothing showed the two forms agree once the data is narrowed.
        entry.update(verdict="differ", field="filter")
        return
    converted = {**widget, "generatedCode": encodeGeneratedCode(request)}
    for index, filters in enumerate(sets):
        entry["filtersChecked"] = index + 1
        try:
            python, pythonProduced = _rerunUnderFilter(widget, filters, job.project, job.tables)
            sql, sqlProduced = _rerunUnderFilter(converted, filters, job.project, job.tables)
        except Exception as error:
            entry.update(verdict="failed", step="filter", filterIndex=index, **_failure(error))
            return
        if pythonProduced is None and sqlProduced is None:
            # Both fail, and the dashboard shows the same empty chart for both.
            continue
        if pythonProduced is None or sqlProduced is None:
            entry.update(verdict="differ", field="filter", filterIndex=index,
                         filterDetail="python failed" if pythonProduced is None else "request failed")
            return
        verdict, field = verdictOf(python, sql)
        if verdict == "match" and python.get("title") != sql.get("title"):
            verdict, field = "differ", "title"
        if verdict != "match":
            entry.update(verdict="differ", field="filter", filterIndex=index, filterDetail=field)
            return
    entry["verdict"] = "match"


def _convertWidget(job, pageId: str, widget: dict) -> dict:
    """The verdict of one candidate."""
    began = time.monotonic()
    entry: dict = {}
    refusal = _pythonRefusal(widget)
    if refusal is not None:
        entry.update(verdict="differ", field=refusal)
    else:
        replay = getattr(job.engine, "replay", None)
        if replay is not None:
            replay.caseId = widget.get("id")
        generated = _generated(job.engine, job.metadata, _description(widget), job.project)
        if isinstance(generated, dict):
            entry.update(verdict="failed", step="generate", **generated)
        else:
            request, finalOutput = _withTitle(generated[0], widget.get("title")), generated[1]
            _checkRequest(job, pageId, widget, request, finalOutput, entry)
            if entry["verdict"] == "match":
                entry.update(chartRequest=request.model_dump(exclude_none=True), generatedCode=encodeGeneratedCode(request))
    entry["durationSeconds"] = round(time.monotonic() - began, 3)
    return entry


def _checkRequest(job, pageId: str, widget: dict, request: ChartRequest, finalOutput: dict, entry: dict) -> None:
    refusal = sqlRefusal(request.sql)
    if refusal is not None:
        entry.update(verdict="differ", field=refusal)
        return
    try:
        chart, table, truncated = _renderWithResult(job.manager, job.project, request, job.tables)
    except Exception as error:
        entry.update(verdict="failed", step="render", **_failure(error))
        return
    verdict, field = verdictOf(widget, chart)
    if verdict != "match":
        entry.update(verdict=verdict, field=field)
        return
    if verdictOf(widget, {**finalOutput, "title": widget.get("title")})[0] != "match":
        # The workflow's own run of the same request showed something else: the SQL is not repeatable.
        entry.update(verdict="differ", field="nondeterministic")
        return
    refusal = resultRefusal(request, chart, table, truncated)
    if refusal is not None:
        entry.update(verdict="differ", field=refusal)
        return
    _filterCheck(job, pageId, widget, request, entry)


# The job's process state, put back afterwards


@contextlib.contextmanager
def _noLangfuse():
    """No Langfuse for the whole job: every handler lookup answers None and the keys are hidden, then all is put back."""
    import utils.llm as llm

    with mock.patch.dict(os.environ), \
            mock.patch.object(llm, "_langfuseConfigured", lambda: False), \
            mock.patch.object(llm, "getLangfuseHandler", lambda: None), \
            mock.patch.object(llm, "newLangfuseHandler", lambda: None):
        for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            os.environ.pop(name, None)
        yield


@contextlib.contextmanager
def _memoisedSchemas():
    """Each table's schema is read from storage once per job (Redis is cut off), then the reader is put back."""
    import utils.tableSchemas as tableSchemasModule

    read = tableSchemasModule.storedTableSchema
    cache: dict[tuple, pa.Schema] = {}

    def memoised(projectId, tableName, *args, **kwargs):
        key = (projectId, tableName)
        if key not in cache:
            cache[key] = read(projectId, tableName, *args, **kwargs)
        return cache[key]

    with mock.patch.object(tableSchemasModule, "storedTableSchema", memoised):
        yield


# Storage


class StorageStore:
    """The project's folder in the AnalyticsHub bucket, read and written as the service does, and the project row."""

    def __init__(self, project: str, client=None):
        if client is None:
            from supabase import ClientOptions

            from utils.supabaseClient import createSupabaseClient

            client = createSupabaseClient(
                os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"], ClientOptions(auto_refresh_token=False, persist_session=False),
            )
        self.project = project
        self.client = client
        self.bucket = client.storage.from_("AnalyticsHub")

    def read(self, name: str) -> bytes:
        from utils.projectStorage import readBytes

        return readBytes(self.project, name)

    def write(self, name: str, data: bytes, overwrite: bool) -> None:
        # The service's own call. `upsert` becomes the `x-upsert` header: "false" makes storage refuse to replace a file.
        self.bucket.upload(path=f"{self.project}/{name}", file=data, file_options={"upsert": "true" if overwrite else "false"})

    def projectModifiedAt(self):
        """The project row's `modifiedAt`, read only. The service updates it on every dashboard edit."""
        rows = self.client.table("Projects").select("modifiedAt").eq("projectId", self.project).execute().data
        if not rows:
            raise LookupError("the project row was not found")
        return rows[0]["modifiedAt"]


class _ReadOnly:
    """What a dry run is given: reads only."""

    def __init__(self, store):
        self._store = store

    def read(self, name: str) -> bytes:
        return self._store.read(name)

    def write(self, *_args, **_kwargs):
        raise AssertionError("a dry run writes nothing")


def _utcNow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _configBytes(config: dict) -> bytes:
    """The service's JSON settings for dashboardConfig.json (`json.dumps(config, indent=4)`, UTF-8)."""
    return json.dumps(config, indent=4).encode("utf-8")


def _readConfig(store) -> tuple[bytes, dict]:
    try:
        original = bytes(store.read(CONFIG))
        config = json.loads(original)
    except Exception:
        raise _Abort("configUnreadable")
    if not isinstance(config, dict):
        raise _Abort("configUnreadable")
    return original, config


def _write(store, original: bytes, newBytes: bytes, replaced: int, report: dict, save) -> None:
    """Backup, check for a concurrent edit, write, verify. Every abort before the write leaves the live config untouched."""
    backup = f"dashboardConfig.backup-{_utcNow().strftime('%Y%m%dT%H%M%SZ')}.json"
    report["backupAttempted"] = backup
    save()
    try:
        store.write(backup, original, overwrite=False)
        if bytes(store.read(backup)) != original:
            raise ValueError("the backup read back differs")
    except Exception:
        raise _Abort("backupNotVerified")
    # Named only now: an unverified name may not exist, or may belong to another run.
    report["backup"] = backup
    save()
    try:
        live = bytes(store.read(CONFIG))
    except Exception:
        live = None
    if live != original:
        raise _Abort("concurrentEdit")
    try:
        store.write(CONFIG, newBytes, overwrite=True)
        if bytes(store.read(CONFIG)) != newBytes:
            raise ValueError("the config read back differs")
    except Exception:
        # Never restored automatically: the backup could overwrite an edit made meanwhile. Read once, and say what is live.
        try:
            live = bytes(store.read(CONFIG))
            outcome = "writeLanded" if live == newBytes else "writeNotApplied" if live == original else "changedBySomeoneElse"
        except Exception:
            outcome = "unreadable"
        report["writeOutcome"] = outcome
        if outcome == "writeLanded":
            report["written"] = replaced
        raise _Abort("writeNotVerified")


# The job


def _loadStub(path: str) -> dict:
    try:
        return chartEval._loadStub(path, [])
    except chartEval._UsageError as error:
        raise _UsageError(str(error))


def _counts(report: dict) -> dict:
    verdicts = [entry["verdict"] for entry in report["widgets"]]
    return {
        "widgets": len(verdicts),
        "candidates": sum(verdict != "skipped" for verdict in verdicts),
        **{name: verdicts.count(name) for name in ("match", "differ", "failed", "skipped")},
        "filtersChecked": sum(entry.get("filtersChecked", 0) for entry in report["widgets"]),
    }


def _convert(args, store, stub: dict | None, report: dict, save) -> None:
    from utils.codeExecutor import REPLManager
    from utils.modelMetadata import activeTables, metadataForModel
    from nubrix.workflows.sqlChartWorkflow import RUN_TIMEOUT_SECONDS

    if args.write:
        # Read only, and before the config: an edit after this point changes it.
        try:
            modifiedBefore = store.projectModifiedAt()
        except Exception:
            raise _Abort("modifiedAtUnreadable")
        report["modifiedAtBefore"] = str(modifiedBefore)
    report["modifiedAtCheck"] = "notChecked"
    original, config = _readConfig(store)
    raw = json.loads(store.read(METADATA))
    # The metadata of a served request; the row counts for the size hints come from the metadata just read.
    job = SimpleNamespace(
        project=args.project,
        metadata=metadataForModel(copy.deepcopy(raw), args.project, knownMetadata=raw),
        tables=frozenset(activeTables(raw)),
        engine=_Engine(chartEval._Replay(stub) if stub is not None else None),
        manager=REPLManager(timeoutSeconds=RUN_TIMEOUT_SECONDS),
        config=config,
    )

    updated = copy.deepcopy(config)
    replaced = 0
    for pageId, page in config.items():
        widgets = page.get("widgets") if isinstance(page, dict) else None
        if not isinstance(widgets, list):
            continue
        for index, widget in enumerate(widgets):
            entry = {"page": pageId, "index": index, "widgetId": widget.get("id") if isinstance(widget, dict) else None}
            reason = _skipReason(widget)
            if reason is not None:
                entry.update(verdict="skipped", reason=reason)
            else:
                # One widget at a time, so the job never takes more than one sandbox slot.
                entry.update(_convertWidget(job, pageId, widget))
                if entry["verdict"] == "match":
                    updated[pageId]["widgets"][index]["generatedCode"] = entry["generatedCode"]
                    replaced += 1
            report["widgets"].append(entry)
    report["counts"] = _counts(report)

    if not args.write:
        report["outcome"] = "dryRun"
        return
    if not replaced:
        report["outcome"] = "nothingToWrite"
        return
    _write(store, original, _configBytes(updated), replaced, report, save)
    report.update(outcome="written", written=replaced)
    try:
        modifiedAfter = store.projectModifiedAt()
        report["modifiedAtAfter"] = str(modifiedAfter)
        report["modifiedAtCheck"] = "unchanged" if modifiedAfter == modifiedBefore else "changed"
    except Exception:
        report["modifiedAtCheck"] = "unreadable"
    if report["modifiedAtCheck"] != "unchanged":
        raise _Abort("possibleConcurrentEdit")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.convertSavedWidgets", description=__doc__.split("\n\n")[0])
    parser.add_argument("--project", required=True, help="the project id")
    parser.add_argument("--out", required=True, help="path of the local JSON report")
    parser.add_argument("--write", action="store_true", help="replace the matched widgets' generatedCode (default: dry run)")
    parser.add_argument("--model-stub", help="JSONL of recorded model replies by widget id (tests)")
    return parser


def _validate(args) -> dict | None:
    from utils.projectStorage import _pathSegment

    try:
        _pathSegment(args.project)
    except ValueError:
        raise _UsageError("--project is not a valid project id")
    if not Path(args.out).resolve().parent.is_dir():
        raise _UsageError("--out must be in an existing folder")
    return _loadStub(args.model_stub) if args.model_stub else None


def _abortMessage(prog: str, report: dict) -> str:
    reason, backup = report["abortReason"], report["backup"]
    if reason == "possibleConcurrentEdit":
        return (f"{prog}: the new config was written and verified, but the project changed during the job "
                f"(possibleConcurrentEdit, modifiedAt {report['modifiedAtCheck']}); compare the live config with the backup {backup}.")
    if reason == "writeNotVerified":
        return (f"{prog}: aborted (writeNotVerified: {report.get('writeOutcome')}); nothing was restored; "
                f"compare the live config with the backup {backup}.")
    return f"{prog}: aborted ({reason}); the live dashboard config was not changed."


def main(argv: list[str] | None = None, store=None) -> int:
    """Run the conversion. Returns 0 when it finished, 1 when it aborted, 2 for a usage error. `store` is for tests."""
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else 2
    try:
        stub = _validate(args)
    except _UsageError as error:
        print(f"{parser.prog}: error: {error}", file=sys.stderr)
        return 2

    report: dict = {
        "gitSha": chartEval._gitSha(), "project": args.project, "mode": "write" if args.write else "dryRun",
        "modelStub": stub is not None, "startedAt": _utcNow().isoformat(), "outcome": None, "abortReason": None,
        "backup": None, "written": 0, "counts": None, "widgets": [],
    }

    def save() -> None:
        report["counts"] = _counts(report)
        Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    code = 0
    try:
        with chartEval._quietLogs(), chartEval._noRedis(), _noLangfuse(), _memoisedSchemas():
            target = store if store is not None else StorageStore(args.project)
            _convert(args, target if args.write else _ReadOnly(target), stub, report, save)
    except _Abort as abort:
        if report["outcome"] != "written":
            report["outcome"] = "aborted"
        report["abortReason"] = abort.reason
        code = 1
    except Exception as error:
        # The detail stays in the local report: an exception's text can hold table names or data.
        report.update(outcome="aborted", abortReason=f"error:{type(error).__name__}", abortDetail=_clipped(error))
        code = 1
    report["finishedAt"] = _utcNow().isoformat()
    save()
    counts = report["counts"]
    for name in ("widgets", "candidates", "match", "differ", "failed", "skipped"):
        print(f"{name} {counts[name]}")
    print(f"filters {counts['filtersChecked']}")
    print(f"written {report['written']}")
    print(f"backup {report['backup'] or 'none'}")
    if code:
        print(_abortMessage(parser.prog, report), file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
