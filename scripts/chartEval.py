"""The chart evaluation harness.

Runs a set of prompts through the SQL chart workflow (`--engine sql`) or the Python one the service serves while
CHART_ENGINE is `off` (`--engine python`), in this process, and compares each chart with the expected answer.

    python -m scripts.chartEval --cases cases.jsonl --data DATA --project ID --engine sql --runs 3 --out report.json
    python -m scripts.chartEval ... --model-stub modelStub.jsonl      (sql only; replays recorded model replies)

`DATA` is a folder holding `<projectId>/<table>.parquet` and `<projectId>/metadata.json`. It is served through a local
storage server (`scripts/localStorageServer.py`), so the real sandbox path runs: schema reads, spool, channel, launcher.
A table the API wrote carries a marker made from the storage key, and its footer is then read by HEAD and Range
requests, and tables without a valid marker, such as a copy of production tables, are read through the reader child as
the API reads them. The marker derives from the storage key, which is random for every invocation. The environment
variable CHART_EVAL_STORAGE_KEY sets it instead: it exists for the synthetic fixtures, whose markers were made with the
made-up key in `buildFixtures.py`, and must never be a real key (not the service key): it is for synthetic data only.
The workflow gets the metadata the service gives it (`utils.modelMetadata`: active tables, size hints). The hints are
those of a served request with a warm cache: the row counts come from the metadata the harness read.
The harness never reads `.env` files: the Gemini key comes from the process environment, and Langfuse is used only when
its keys are already there. It charges no credits, writes to no database, talks to no storage but the local one, and
reaches no Redis: for the length of a run every Redis pool of the process refuses at once, and is restored afterwards.

Case line (JSONL). A field that is null or missing is not checked. Every line is validated before anything runs; a
mistake is a usage error (exit 2) that names the line:
    {"id": str, "prompt": str, "expect": {"doubt": bool, "chartType": str | null, "labels": [str] | null,
     "values": {datasetLabel: [number | null | point]} | null, "rows": [dict] | null, "card": number | str | null,
     "points": [{"lat": number, "long": number, "id"?: str, "label"?: str}] | null, "relTol": number,
     "columnNames": bool, "expectSql": str | null}}
A case that is not a doubt must check at least one of labels, values, rows, card, points or expectSql; a geoMap needs
points or expectSql. A doubt case checks nothing else.

How values are compared:
  - A JSON integer in the expected answer is compared exactly, whatever relTol is; relTol applies to floats only
    (0 means exact). Write counts as integers. A bool is never a number, a null meets only a null, and a NaN or an
    infinity never matches. Text compares exactly.
  - `labels` and `rows` are compared in order. An extra or a missing label, row or dataset fails.
  - `values` maps a dataset label to the numbers aligned with the labels. With one expected dataset and one in the
    chart, the labels are not compared (the model chooses its own column names). With several, the datasets must be
    exactly the expected ones by label, since legend names are visible to users. A pivot always compares its column
    names: they are data values. For scatter and bubble charts the list holds the point dicts, e.g.
    `{"Series": [{"x": 1, "y": 2}, ...]}`; the dicts must have the same keys.
  - `rows` are compared by position: the values of each row in column order. Column names are compared only when the
    case sets `"columnNames": true`.
  - `card` compares the value only, not the card's label.
  - `points` is for a geoMap: the ordered list of points. Only the keys a point lists are compared (`lat`, `long`,
    `label`, `id`), so leave `id` out when it is the row position.
  - A pivot's row keys are its labels and its column keys its datasets.

Relative periods ("the last 12 months") count back from today, so they have no fixed answer. `expectSql` computes it:
one read-only DuckDB SELECT or WITH statement, run in this process at the start of every run against the case's own
data. The report records each run's date. If the date changed while a run was going (a run that crosses midnight), the
run is marked `stale`, counts as failed, and the exit code is 1 with a line on standard error. Every table file in `<data>/<project>/` is a table of that
name; external access is switched off, so the statement reads nothing else. The result fills the expected fields by
the case's chartType: for a table the rows; for a card the first row's first column; for a geoMap the points (columns
named lat, long, and optionally label and id); otherwise the first column is the labels and every other column a
dataset named by its alias. Values are converted by the chart builder's own helpers, so a DATE label or an integer
label is the text the chart has, and a table cell or a card is the builder's value. `expectSql` cannot be combined with labels, values, rows, card or points, and it has no
form for scatter or bubble charts.

Model stub line (JSONL, one per case id): {"id": str, "rephrased": {"rephrasedOutput": str | null, "doubt": str | null},
"reply": str}. `rephrased` replaces the rephraser, `reply` replaces the generator and every repair.

A case that expects a doubt passes only when the rephraser gave a non-empty doubt and the response is that doubt. The
fallback answer for a rephrase that gave no question, or a response a generated Python program printed, fails it. The
Python engine exposes the rephrased doubt too, so it can pass; a printed response never does.

Standard output is `PASS k/n` per run, `FAILED <id>` for each failed case, and `ALL RUNS PASS`: never a value, a prompt
or a table name. The report file (`--out`) is local and holds the detail of every case: the generated SQL or code, the
final error text and its stage, the repair count, the rephrased query and doubt, and the labels, values, rows, card and
points the chart had next to the expected ones.

Exit code: 0 when every run passes every case, 1 when a case failed, 2 for a usage or input error (nothing was run).
"""

__all__ = ["main", "checkValues"]

import argparse
import contextlib
import copy
import datetime as dt
import decimal
import json
import math
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock

from nubrix.components.chartBuilder import _cell, _levelText, _number, _text
from nubrix.components.chartContract import contractErrors

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_REL_TOL = 0.0
_CHECK_FIELDS = ("labels", "values", "rows", "card", "points")
_EXPECT_KEYS = {"doubt", "chartType", *_CHECK_FIELDS, "relTol", "columnNames", "expectSql"}
_CASE_KEYS = {"id", "prompt", "expect"}
_POINT_KEYS = {"id", "lat", "long", "label"}
_NO_EXPECT_SQL = {"scatter", "bubble"}
_DETAIL_CHARS = 4000


class _UsageError(Exception):
    """A mistake in the arguments or the input files: exit code 2, nothing was run."""


# The value check


def _typeName(value: Any) -> str:
    return type(value).__name__


def _length(value: Any) -> int | None:
    return len(value) if isinstance(value, (list, dict, str)) else None


def _isNumber(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _same(expected: Any, got: Any, relTol: float) -> bool:
    """One value. A JSON integer is exact; a float is within `relTol` (exact at 0); text and the rest compare exactly."""
    if expected is None or got is None:
        return expected is None and got is None
    if isinstance(expected, bool) or isinstance(got, bool):
        return isinstance(expected, bool) and isinstance(got, bool) and expected == got
    if _isNumber(expected):
        if not _isNumber(got) or not (math.isfinite(expected) and math.isfinite(got)):
            return False
        if type(expected) is int or relTol <= 0 or expected == 0:
            return got == expected
        return abs(got - expected) <= relTol * abs(expected)
    if isinstance(expected, dict):
        return isinstance(got, dict) and set(expected) == set(got) and all(_same(expected[k], got[k], relTol) for k in expected)
    if isinstance(expected, list):
        return isinstance(got, list) and len(expected) == len(got) and all(_same(e, g, relTol) for e, g in zip(expected, got))
    return type(expected) is type(got) and expected == got


def _mismatch(field: str, expected: Any, got: Any, **extra) -> dict:
    """A failed check, described by types, lengths and the first position that differs."""
    return {
        "ok": False,
        "field": field,
        "expectedType": _typeName(expected),
        "gotType": _typeName(got),
        "expectedLength": _length(expected),
        "gotLength": _length(got),
        **extra,
    }


def _firstDifference(expected: list, got: list, same) -> int | None:
    """The first position where the lists differ; when one is longer, the length of the shorter."""
    for index, (e, g) in enumerate(zip(expected, got)):
        if not same(e, g):
            return index
    return None if len(expected) == len(got) else min(len(expected), len(got))


def _gotLabels(chart: dict) -> list | None:
    data = chart.get("data")
    if isinstance(data, dict) and isinstance(data.get("labels"), list):
        return data["labels"]
    if chart.get("chartType") == "pivot" and isinstance(data, dict):
        # A pivot's row keys play the labels; every column lists them in the same order.
        rows: dict = {}
        for column in data.values():
            if isinstance(column, dict):
                rows.update(dict.fromkeys(column))
        return list(rows)
    return None


def _gotValues(chart: dict) -> dict | None:
    """Dataset label -> values, aligned with the labels. None when the chart has no such datasets or repeats a label."""
    data = chart.get("data")
    if chart.get("chartType") == "pivot" and isinstance(data, dict):
        labels = _gotLabels(chart)
        return {name: [column.get(row) for row in labels] for name, column in data.items() if isinstance(column, dict)}
    datasets = data.get("datasets") if isinstance(data, dict) else None
    if not isinstance(datasets, list) or not all(isinstance(d, dict) and "label" in d for d in datasets):
        return None
    result = {d["label"]: d.get("data") for d in datasets}
    return result if len(result) == len(datasets) else None


def _gotPoints(chart: dict) -> list | None:
    """A geoMap's points: the builder puts them under `map`, not under `data`."""
    mapData = chart.get("map")
    points = mapData.get("data", {}).get("points") if isinstance(mapData, dict) and isinstance(mapData.get("data"), dict) else None
    return points if isinstance(points, list) else None


def _sameRow(expected: Any, got: Any, relTol: float, columnNames: bool) -> bool:
    """A row by the values in column order; with `columnNames` also by name."""
    if not isinstance(expected, dict) or not isinstance(got, dict):
        return _same(expected, got, relTol)
    if columnNames:
        return _same(expected, got, relTol)
    return len(expected) == len(got) and all(_same(e, g, relTol) for e, g in zip(expected.values(), got.values()))


def _samePoint(expected: Any, got: Any, relTol: float) -> bool:
    """A geoMap point on the keys the expected point lists."""
    if not isinstance(expected, dict) or not isinstance(got, dict):
        return False
    return all(key in got and _same(value, got[key], relTol) for key, value in expected.items())


def _comparedLists(expected: list, got: Any, same) -> dict | None:
    """None when the two lists match under `same`, else where they first differ."""
    if not isinstance(got, list):
        return {"index": None}
    if len(expected) == len(got) and all(same(e, g) for e, g in zip(expected, got)):
        return None
    return {"index": _firstDifference(expected, got, same)}


def checkValues(expect: dict, chart: dict) -> dict:
    """Compare a chart with an expectation. Returns `{"ok": True}` or the first failed field.

    The rules are in the module docstring: integers exact whatever `relTol` is, labels and rows in order, one dataset
    compared without its label, rows by position unless `columnNames`, a card by value, geoMap `points`, and a pivot's
    row keys as labels and column keys as datasets. A null field is not checked.
    """
    relTol = float(expect.get("relTol") or _DEFAULT_REL_TOL)
    labels = expect.get("labels")
    if labels is not None:
        got = _gotLabels(chart)
        difference = _comparedLists(labels, got, lambda e, g: _same(e, g, 0))
        if difference is not None:
            return _mismatch("labels", labels, got, **difference)
    values = expect.get("values")
    if values is not None:
        got = _gotValues(chart)
        if got is None:
            return _mismatch("values", values, got, dataset=None)
        if len(values) == 1 and len(got) == 1 and chart.get("chartType") != "pivot":
            # The model names its own measure column, so a lone dataset is matched by position.
            pairs = [(name, expected, next(iter(got.values()))) for name, expected in values.items()]
        elif set(values) != set(got):
            return _mismatch("values", values, got, dataset=None)
        else:
            pairs = [(name, expected, got[name]) for name, expected in values.items()]
        for name, expected, gotValues in pairs:
            difference = _comparedLists(expected, gotValues, lambda e, g: _same(e, g, relTol))
            if difference is not None:
                return _mismatch("values", expected, gotValues, dataset=name, **difference)
    rows = expect.get("rows")
    if rows is not None:
        got = chart.get("data")
        columnNames = bool(expect.get("columnNames"))
        difference = _comparedLists(rows, got, lambda e, g: _sameRow(e, g, relTol, columnNames))
        if difference is not None:
            return _mismatch("rows", rows, got, **difference)
    card = expect.get("card")
    if card is not None:
        got = chart.get("data")
        if not _same(card, got, relTol):
            return _mismatch("card", card, got)
    points = expect.get("points")
    if points is not None:
        got = _gotPoints(chart)
        difference = _comparedLists(points, got, lambda e, g: _samePoint(e, g, relTol))
        if difference is not None:
            return _mismatch("points", points, got, **difference)
    return {"ok": True}


def _gotFields(chart: dict) -> dict:
    """What the chart holds, in the shape of the expected fields, for the report."""
    got: dict = {}
    chartType = chart.get("chartType")
    labels = _gotLabels(chart)
    if labels is not None:
        got["labels"] = labels
    values = _gotValues(chart)
    data = chart.get("data")
    if values is not None:
        got["values"] = values
    elif isinstance(data, dict) and isinstance(data.get("datasets"), list):
        got["datasets"] = data["datasets"]
    if chartType == "table" and isinstance(data, list):
        got["rows"] = data
    if chartType == "card":
        got["card"] = data
    points = _gotPoints(chart)
    if points is not None:
        got["points"] = points
    return got


# Cases and the model stub


def _readJsonLines(path: str, what: str) -> list[tuple[int, dict]]:
    """`(line number, object)` for each line of a JSONL file. An error names the line only: the file can hold private prompts."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise _UsageError(f"Cannot read the {what} file ({type(error).__name__}).")
    entries = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            raise _UsageError(f"The {what} file has invalid JSON on line {number}.")
        if not isinstance(entry, dict):
            raise _UsageError(f"The {what} file needs a JSON object on line {number}.")
        entries.append((number, entry))
    return entries


def _short(value: Any) -> str:
    return repr(value)[:40]


def _isNumberOrNull(value: Any) -> bool:
    return value is None or _isNumber(value)


def _expectProblems(expect: dict) -> list[str]:
    """What is wrong with an `expect` object, one sentence each."""
    problems = [f"unknown expect key {_short(key)}" for key in sorted(set(expect) - _EXPECT_KEYS, key=str)]
    doubt = expect.get("doubt", False)
    if not isinstance(doubt, bool):
        problems.append("doubt must be true or false")
    chartType = expect.get("chartType")
    if chartType is not None and not (isinstance(chartType, str) and chartType):
        problems.append("chartType must be a non-empty string or null")
    relTol = expect.get("relTol", 0)
    if not _isNumber(relTol) or relTol < 0 or not math.isfinite(relTol):
        problems.append("relTol must be a number of at least 0")
    if not isinstance(expect.get("columnNames", False), bool):
        problems.append("columnNames must be true or false")
    labels = expect.get("labels")
    if labels is not None and not (isinstance(labels, list) and all(isinstance(label, str) for label in labels)):
        problems.append("labels must be a list of strings")
    values = expect.get("values")
    if values is not None:
        if not isinstance(values, dict) or not values:
            problems.append("values must be an object with at least one dataset")
        else:
            for name, series in values.items():
                if not isinstance(series, list) or not all(_isNumberOrNull(item) or isinstance(item, dict) for item in series):
                    problems.append(f"values {_short(name)} must be a list of numbers, nulls or point objects")
    rows = expect.get("rows")
    if rows is not None and not (isinstance(rows, list) and all(isinstance(row, dict) for row in rows)):
        problems.append("rows must be a list of objects")
    card = expect.get("card")
    if card is not None and not (_isNumber(card) or isinstance(card, str)):
        problems.append("card must be a number or a string")
    points = expect.get("points")
    if points is not None:
        if not isinstance(points, list) or not points:
            problems.append("points must be a non-empty list of objects")
        else:
            for point in points:
                if not isinstance(point, dict) or not {"lat", "long"} <= set(point) or set(point) - _POINT_KEYS \
                        or not _isNumber(point.get("lat")) or not _isNumber(point.get("long")) \
                        or not all(isinstance(point.get(key, ""), str) for key in ("id", "label")):
                    problems.append("each point needs a numeric lat and long and may add a text id and label")
                    break
        if chartType != "geoMap":
            problems.append("points needs chartType geoMap")
    expectSql = expect.get("expectSql")
    if expectSql is not None and not (isinstance(expectSql, str) and expectSql.strip()):
        problems.append("expectSql must be a non-empty string")
    return problems


def _caseProblems(case: dict) -> list[str]:
    problems = [f"unknown key {_short(key)}" for key in sorted(set(case) - _CASE_KEYS, key=str)]
    if not (isinstance(case.get("id"), str) and case["id"]):
        problems.append("id must be a non-empty string")
    if not (isinstance(case.get("prompt"), str) and case["prompt"].strip()):
        problems.append("prompt must be a non-empty string")
    expect = case.get("expect")
    if not isinstance(expect, dict):
        return problems + ["expect must be an object"]
    problems += _expectProblems(expect)
    if problems:
        return problems
    checked = [field for field in _CHECK_FIELDS if expect.get(field) is not None]
    if expect.get("doubt"):
        extras = checked + [key for key in ("chartType", "expectSql") if expect.get(key) is not None]
        return [f"a doubt case checks only the question, not {_short(key)}" for key in extras]
    expectSql = expect.get("expectSql")
    if expectSql is not None:
        problems += [f"expectSql cannot be combined with {field}" for field in checked]
        if expect.get("chartType") is None:
            problems.append("expectSql needs a chartType to know how to read its result")
        elif expect["chartType"] in _NO_EXPECT_SQL:
            problems.append(f"expectSql has no form for a {expect['chartType']} chart")
    elif not checked:
        problems.append("a case that is not a doubt must check at least one of labels, values, rows, card, points or expectSql")
    elif expect.get("chartType") == "geoMap" and expect.get("points") is None:
        problems.append("a geoMap case needs points or expectSql")
    return problems


def _loadCases(path: str) -> tuple[list[dict], dict[str, int]]:
    """The validated cases of a file and the line each came from. Every problem found is reported, with its line."""
    entries = _readJsonLines(path, "cases")
    if not entries:
        raise _UsageError("The cases file has no cases.")
    problems, lines = [], {}
    for number, case in entries:
        problems += [f"Cases line {number}: {problem}." for problem in _caseProblems(case)]
        caseId = case.get("id")
        if isinstance(caseId, str) and caseId:
            if caseId in lines:
                problems.append(f"Cases line {number}: id repeats line {lines[caseId]}.")
            else:
                lines[caseId] = number
    if problems:
        raise _UsageError("\n".join(problems))
    return [case for _, case in entries], lines


def _loadStub(path: str, cases: list[dict]) -> dict[str, dict]:
    stub = {}
    for number, entry in _readJsonLines(path, "model stub"):
        if not isinstance(entry.get("id"), str) or not isinstance(entry.get("rephrased"), dict) or not isinstance(entry.get("reply"), str):
            raise _UsageError(f"The model stub needs an id, a rephrased object and a reply string on line {number}.")
        stub[entry["id"]] = entry
    missing = [case["id"] for case in cases if case["id"] not in stub]
    if missing:
        raise _UsageError("The model stub has no entry for: " + ", ".join(missing))
    return stub


# The expected answer of a relative period


def _tableSources(folder: Path):
    """A pyarrow dataset for every table file of the folder, by file name. They are read lazily, and by pyarrow."""
    import pyarrow.dataset as ds

    return {path.stem: ds.dataset(str(path)) for path in sorted(folder.glob("*.parquet"))}


def _expectedFromSql(sql: str, chartType: str, sources: dict) -> dict:
    """The expected fields a read-only DuckDB statement over the case's tables gives, by the chart's shape."""
    import duckdb

    connection = duckdb.connect(":memory:")
    try:
        statements = connection.extract_statements(sql)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise _UsageError("expectSql must be a single SELECT or WITH statement.")
        for name, source in sources.items():
            connection.register(name, source)
        # After the tables are in and before the lock; nothing the statement can name is read from a file.
        connection.execute("SET enable_external_access=false")
        connection.execute("SET lock_configuration=true")
        cursor = connection.execute(sql)
        columns = [description[0] for description in cursor.description]
        rows = [list(row) for row in cursor.fetchall()]
    except _UsageError:
        raise
    except Exception as error:
        raise _UsageError(f"expectSql failed ({type(error).__name__}).")
    finally:
        connection.close()
    # The values are converted by the chart builder's own helpers, so a correct chart matches: dates and integers
    # become the text a label has, numbers are the builder's numbers, and a table or card cell is its `_cell`.
    if chartType == "table":
        return {"rows": [dict(zip(columns, [_cell(value) for value in row])) for row in rows]}
    if chartType == "card":
        if not rows or not columns:
            raise _UsageError("expectSql for a card returned no row.")
        raw = rows[0][0]
        value = "true" if raw is True else "false" if raw is False else _cell(raw)
        return {"card": 0 if value is None else value}
    if chartType == "geoMap":
        if not {"lat", "long"} <= set(columns) or not set(columns) <= _POINT_KEYS:
            raise _UsageError("expectSql for a geoMap needs the columns lat and long, and may add label and id.")
        points = []
        for index, row in enumerate(rows):
            record = dict(zip(columns, row))
            lat, long = _number(record["lat"]), _number(record["long"])
            # The rows the builder drops (`_buildGeoMap`): a missing or non-finite coordinate, or one out of range.
            if lat is None or long is None or not -90 <= lat <= 90 or not -180 <= long <= 180:
                continue
            point = {"lat": lat, "long": long}
            if "id" in record:
                point["id"] = _text(record["id"]) or str(index)
            if "label" in record:
                point["label"] = _text(record["label"]) or f"{lat}, {long}"
            points.append(point)
        return {"points": points}
    if len(columns) < 2:
        raise _UsageError("expectSql needs a label column and at least one value column.")
    labelText = _levelText if chartType == "pivot" else _text
    return {
        "labels": [labelText(row[0]) for row in rows],
        "values": {name: [_number(row[index]) for row in rows] for index, name in enumerate(columns) if index},
    }


def _today() -> dt.date:
    """The date relative periods count back from. The harness reads it at the start and the end of every run."""
    return dt.date.today()


def _resolveCases(cases: list[dict], lines: dict[str, int], folder: Path) -> list[dict]:
    """The cases with every `expectSql` replaced by the expected fields it computes (as of now)."""
    if not any(case["expect"].get("expectSql") for case in cases):
        return cases
    sources = _tableSources(folder)
    resolved = []
    for case in cases:
        sql = case["expect"].get("expectSql")
        if not sql:
            resolved.append(case)
            continue
        try:
            filled = _expectedFromSql(sql, case["expect"]["chartType"], sources)
        except _UsageError as error:
            raise _UsageError(f"Cases line {lines[case['id']]}: {error}")
        resolved.append({**case, "expect": {**case["expect"], **filled}})
    return resolved


class _Replay:
    """The recorded model: one reply per case id. The rephraser, the generator and every repair read the current case."""

    def __init__(self, stub: dict[str, dict]):
        self.stub = stub
        self.caseId: str | None = None
        # `(key, inputs)` of every call, so a test can see what the model would have been shown.
        self.inputs: list[tuple[str, Any]] = []

    def chain(self, key: str):
        replay = self

        class Chain:
            def invoke(self, inputs, _config=None):
                replay.inputs.append((key, inputs))
                value = replay.stub[replay.caseId][key]
                return dict(value) if isinstance(value, dict) else value

        return Chain()

    def rephraserClass(self):
        replay = self

        class Rephraser:
            def __init__(self, *args, **kwargs):
                pass

            def getQueryRephraserChain(self):
                return replay.chain("rephrased")

        return Rephraser


# The run


@contextlib.contextmanager
def _noRedis():
    """No Redis can be reached for the length of a run, and everything is put back afterwards.

    Two layers, both restored in a `finally`:
      - Every Redis pool slot the run path uses is swapped for a pool that raises `redis.exceptions.ConnectionError`
        without connecting (the pools are made lazily and kept for the life of the process, so an environment variable
        could not be relied on: a pool that exists already keeps its host). Every caller of a run already tolerates the
        error: the project slots count it as taken, the table and schema caches read storage instead.
      - `connect` (and `connect_check_health`) of redis-py's connection classes, sync and asyncio, raise the same
        error. A client made during the run, by any code, such as `redis.Redis(host=...)`, or a pool module first
        imported during the run, therefore fails at once without opening a socket.
    """
    import importlib

    import redis
    import redis.connection

    error = "Redis is switched off during an evaluation run."

    class DeadPool(redis.ConnectionPool):
        def get_connection(self, *args, **kwargs):
            raise redis.exceptions.ConnectionError(error)

    def refuse(self, *args, **kwargs):
        raise redis.exceptions.ConnectionError(error)

    async def refuseAsync(self, *args, **kwargs):
        raise redis.exceptions.ConnectionError(error)

    slots = [("utils.initMethods", "_REDIS_POOL", True), ("api.services.credits.creditService", "_redis_pool", True),
             ("nubrix.components.transformationExecutor", "_redis_pool", False), ("api.services.reportingService", "_REPORTING_REDIS_POOL", False)]
    patches = [(redis.connection.AbstractConnection, "connect", refuse), (redis.connection.AbstractConnection, "connect_check_health", refuse)]
    try:
        import redis.asyncio.connection

        patches += [(redis.asyncio.connection.AbstractConnection, "connect", refuseAsync)]
    except ImportError:
        pass
    swapped = []
    try:
        for owner, attribute, replacement in patches:
            swapped.append((owner, attribute, owner.__dict__[attribute]))
            setattr(owner, attribute, replacement)
        for moduleName, attribute, required in slots:
            if required:
                importlib.import_module(moduleName)
            module = sys.modules.get(moduleName)
            if module is not None:
                swapped.append((module, attribute, getattr(module, attribute)))
                setattr(module, attribute, DeadPool())
        yield
    finally:
        for owner, attribute, original in reversed(swapped):
            setattr(owner, attribute, original)


@contextlib.contextmanager
def _quietLogs():
    """The workflow's loggers are off while it runs: they log table names and project ids, and none belongs on stdout.

    Only the harness's own call chain is disabled (nubrix, utils, api, scripts) and the loguru activation state is put
    back exactly as it was, so a logger another library had disabled stays disabled and none is enabled that was off.
    """
    from loguru import logger

    core = logger._core
    with core.lock:
        saved = (list(core.activation_list), dict(core.enabled), core.activation_none)
    try:
        for name in ("nubrix", "utils", "api", "scripts"):
            logger.disable(name)
        yield
    finally:
        with core.lock:
            core.activation_list, core.enabled, core.activation_none = saved[0], saved[1], saved[2]


@contextlib.contextmanager
def _environment(stubbed: bool, dataFolder: str):
    """The process environment of a run, restored afterwards: the local storage server and nothing else to reach.

    Yields the server's request log, a list of `{"method", "file", "range", "status"}`.
    """
    from scripts.localStorageServer import serveFolder

    key = os.environ.get("CHART_EVAL_STORAGE_KEY") or secrets.token_urlsafe(24)
    changed = ["SUPABASE_KEY", "SUPABASE_URL"]
    # Left alone when already set. SANDBOX_ALLOW_SAME_UID is the local-development switch; an image that sets
    # SANDBOX_RUN_ROOT gets its own uids as always. A stubbed model needs no key, only a value for modules that read one.
    defaults = {"SECRET_KEY": "local-eval-only", "SANDBOX_ALLOW_SAME_UID": "1"}
    if stubbed:
        defaults.update(GEMINI_API_KEY="local-eval-placeholder", GROQ_API_KEY="local-eval-placeholder", GOOGLE_API_KEY="local-eval-placeholder")
    saved = {name: os.environ.get(name) for name in changed + list(defaults)}
    log: list = []
    url, stop = serveFolder(dataFolder, key=key, requestLog=log)
    try:
        os.environ.update(SUPABASE_URL=url, SUPABASE_KEY=key)
        for name, value in defaults.items():
            os.environ.setdefault(name, value)
        with _noRedis():
            yield log
    finally:
        stop()
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _gitSha() -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=_REPO, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def _clipped(value: Any) -> str:
    return str(value)[:_DETAIL_CHARS]


class _Engine:
    """Builds the workflow of an engine and runs one prompt through it."""

    def __init__(self, engine: str, replay: _Replay | None):
        self.engine = engine
        self.replay = replay
        self.failure: dict | None = None
        self.knownMetadata: dict | None = None
        if engine == "sql":
            from nubrix.workflows import sqlChartWorkflow

            self.module = sqlChartWorkflow
            if replay is not None:
                with mock.patch.object(sqlChartWorkflow, "buildLlmChain", lambda section, prompt, **kw: replay.chain("reply")), \
                        mock.patch.object(sqlChartWorkflow, "QueryRephaser", replay.rephraserClass()), \
                        mock.patch.object(sqlChartWorkflow, "ParallelQueryRephaser", replay.rephraserClass()):
                    self.workflow = sqlChartWorkflow.buildSqlChartWorkflow()
            else:
                self.workflow = sqlChartWorkflow.buildSqlChartWorkflow()
        else:
            from nubrix.workflows.reportingToolWorkflow import buildReportingWorkflow

            self.workflow = buildReportingWorkflow()

    def _recordFailure(self, **fields) -> None:
        # What the workflow's failure log would say, kept for the report and never printed.
        self.failure = {"stage": fields.get("stage"), "error": _clipped(fields.get("error")), "repairs": fields.get("repairs")}

    def invoke(self, metadata: dict, prompt: str, project: str) -> dict:
        self.failure = None
        config = None
        if self.replay is None:
            from utils.llm import getLangfuseConfig

            config = getLangfuseConfig(
                trace_name="ReportingWorkflow", projectId=project, tags=["reporting", "evaluation"], perRequestHandler=True,
            ) or None
        state = {"metadata": metadata, "inputQuery": prompt, "projectId": project}
        if self.engine == "python" and self.knownMetadata is not None:
            # The Python workflow routes eager reads of large tables to lazy scans by the row counts it finds; a served
            # request finds them in Redis, so the harness hands it the metadata it read.
            state["knownMetadata"] = self.knownMetadata
        if self.engine == "sql":
            with mock.patch.object(self.module, "logChartFailure", self._recordFailure):
                return self.workflow.invoke(state, config=config)
        return self.workflow.invoke(state, config=config)


def _rephrasedDoubt(response: dict) -> str | None:
    rephrased = response.get("rephrasedQuery")
    doubt = rephrased.get("doubt") if isinstance(rephrased, dict) else None
    return doubt if isinstance(doubt, str) and doubt.strip() else None


def _isGenuineDoubt(response: dict, output: dict) -> bool:
    """A question the workflow returned in place of a chart, with no code or chart behind it.

    The question is the rephraser's doubt, or the no-data period question, which the SQL workflow returns in the same
    shape: the text in ``rephrasedQuery["doubt"]`` and ``finalOutput == {"response": doubt}``.
    """
    doubt = _rephrasedDoubt(response)
    return doubt is not None and output == {"response": doubt} and not response.get("generatedCode") and not response.get("codeOutput")


def _expectedFields(expect: dict) -> dict:
    return {field: expect[field] for field in (*_CHECK_FIELDS, "chartType") if expect.get(field) is not None}


def _detail(response: dict, output: Any, failure: dict | None) -> dict:
    """The report's explanation of a case: what the workflow generated, what it said, how hard it tried."""
    rephrased = response.get("rephrasedQuery")
    detail = {
        "generatedCode": response.get("generatedCode"),
        "error": output["error"] if isinstance(output, dict) and "error" in output else None,
        "failureDetail": failure["error"] if failure else None,
        "repairs": response.get("debugAttempts"),
        "doubt": rephrased.get("doubt") if isinstance(rephrased, dict) else None,
        "rephrasedOutput": rephrased.get("rephrasedOutput") if isinstance(rephrased, dict) else (rephrased if isinstance(rephrased, str) else None),
    }
    return detail


def _judge(case: dict, response: dict, failedStage: str | None, failure: dict | None = None, engine: str = "sql") -> dict:
    """The case's result fields from the workflow's response."""
    expect = case["expect"]
    output = response.get("finalOutput")
    result: dict = {"id": case["id"], "ok": False, "stage": None, "chartType": None, "contractErrors": [], "valueCheck": None,
                    **_detail(response, output, failure), "expected": _expectedFields(expect), "got": {}}
    if not isinstance(output, dict):
        result["stage"] = "noOutput"
        return result
    if "error" in output:
        result["stage"] = failedStage or "error"
        return result
    if "response" in output and "chartType" not in output:
        if not expect.get("doubt"):
            result["stage"] = "unexpectedDoubt"
        elif _isGenuineDoubt(response, output):
            result.update(ok=True, stage="doubt")
        else:
            result["stage"] = "notADoubt"
            result["reason"] = (
                "The response is not a question the rephraser asked: "
                + ("the Python engine printed it." if engine == "python" and _rephrasedDoubt(response) is None
                   else "the rephraser gave no doubt, so the workflow's fallback answer came back.")
            )
        return result
    result["chartType"] = output.get("chartType")
    result["got"] = _gotFields(output)
    if expect.get("doubt"):
        result["stage"] = "expectedDoubt"
        return result
    if expect.get("chartType") is not None and output.get("chartType") != expect["chartType"]:
        result["stage"] = "chartType"
        return result
    result["contractErrors"] = list(contractErrors(output))
    if result["contractErrors"]:
        result["stage"] = "contract"
        return result
    result["valueCheck"] = checkValues(expect, output)
    if not result["valueCheck"]["ok"]:
        result["stage"] = "value"
        return result
    result.update(ok=True, stage="ok")
    return result


def _runCase(engine: _Engine, metadata: dict, case: dict, project: str) -> dict:
    if engine.replay is not None:
        engine.replay.caseId = case["id"]
    began = time.monotonic()
    try:
        response = engine.invoke(metadata, case["prompt"], project)
        result = _judge(case, response, (engine.failure or {}).get("stage"), engine.failure, engine.engine)
    except Exception as error:
        # In the report only: an exception's text can hold table names or data.
        result = {"id": case["id"], "ok": False, "stage": f"exception:{type(error).__name__}", "chartType": None,
                  "contractErrors": [], "valueCheck": None, "error": _clipped(f"{type(error).__name__}: {error}"),
                  "expected": _expectedFields(case["expect"]), "got": {}}
    result["durationSeconds"] = round(time.monotonic() - began, 3)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.chartEval", description=__doc__.split("\n\n")[0],
        epilog="Environment: CHART_EVAL_STORAGE_KEY sets the key of the local storage server and so the table markers. "
               "It is for the synthetic fixtures only and must never be a real key; unset, a random key is used.",
    )
    parser.add_argument("--cases", required=True, help="JSONL file of cases")
    parser.add_argument("--data", required=True, help="folder holding <projectId>/<table>.parquet and metadata.json")
    parser.add_argument("--project", required=True, help="project id: the folder under --data")
    parser.add_argument("--engine", choices=("sql", "python"), default="sql")
    parser.add_argument("--runs", type=int, default=1, help="how many times to run every case")
    parser.add_argument("--out", required=True, help="path of the JSON report")
    parser.add_argument("--model-stub", help="JSONL of recorded model replies by case id (sql engine only)")
    parser.add_argument("--only", action="append", metavar="ID", help="run only this case id (repeatable)")
    return parser


def _storageSummary(log: list) -> dict:
    """How the tables were read, from the server's request log. Reads of `metadata.json` are left out."""
    tables = [entry for entry in log if entry["file"] and entry["file"].endswith(".parquet") and entry["status"] in (200, 206)]
    return {
        "requests": len(log),
        "tableHead": sum(entry["method"] == "HEAD" for entry in tables),
        "tableRangeReads": sum(entry["method"] == "GET" and entry["range"] is not None for entry in tables),
        "tableFullReads": sum(entry["method"] == "GET" and entry["range"] is None for entry in tables),
    }


def _prepare(args: argparse.Namespace) -> tuple[list[dict], dict[str, int], dict | None, Path]:
    """The cases to run, their lines, the stub and the data folder; every mistake in the input is a `_UsageError`.

    `expectSql` is resolved once here to fail fast; every run resolves it again.
    """
    if args.model_stub and args.engine != "sql":
        raise _UsageError("--model-stub works with --engine sql only")
    if args.runs < 1:
        raise _UsageError("--runs must be at least 1")
    folder = Path(args.data) / args.project
    if not folder.is_dir():
        raise _UsageError("--data has no folder for --project")
    cases, lines = _loadCases(args.cases)
    if args.only:
        unknown = sorted(set(args.only) - {case["id"] for case in cases})
        if unknown:
            raise _UsageError("unknown case id: " + ", ".join(unknown))
        cases = [case for case in cases if case["id"] in args.only]
    stub = _loadStub(args.model_stub, cases) if args.model_stub else None
    _resolveCases(cases, lines, folder)
    return cases, lines, stub, folder


def main(argv: list[str] | None = None) -> int:
    """Run the evaluation. Returns 0 when every run passes every case, 1 when a case failed, 2 for a usage or input error."""
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else 2
    try:
        cases, lines, stub, folder = _prepare(args)
    except _UsageError as error:
        print(f"{parser.prog}: error: {error}", file=sys.stderr)
        return 2

    runs = []
    try:
        with _quietLogs(), _environment(stub is not None, args.data) as storageLog:
            from utils import projectStorage
            from utils.modelMetadata import metadataForModel

            # The metadata of a served request: active tables only, with the size hints.
            raw = projectStorage.readJson(args.project, "metadata.json")
            # The row counts come from the metadata just read, as a served request's come from its Redis copy.
            metadata = metadataForModel(copy.deepcopy(raw), args.project, knownMetadata=raw)
            engine = _Engine(args.engine, _Replay(stub) if stub is not None else None)
            engine.knownMetadata = raw
            for number in range(1, args.runs + 1):
                began = _today()
                runCases = _resolveCases(cases, lines, folder)
                results = [_runCase(engine, metadata, case, args.project) for case in runCases]
                passed = sum(result["ok"] for result in results)
                stale = _today() != began
                runs.append({"run": number, "date": began.isoformat(), "stale": stale, "passed": passed, "total": len(results), "cases": results})
                if stale:
                    print(f"{parser.prog}: the date changed during run {number}.", file=sys.stderr)
                print(f"PASS {passed}/{len(results)}")
                for result in results:
                    if not result["ok"]:
                        print(f"FAILED {result['id']}")
            storage = _storageSummary(storageLog)
    except Exception as error:
        print(f"{parser.prog}: error: the run could not start or continue ({type(error).__name__}).", file=sys.stderr)
        return 2
    allPass = all(run["passed"] == run["total"] and not run["stale"] for run in runs)
    report = {
        "gitSha": _gitSha(),
        "engine": args.engine,
        "project": args.project,
        "modelStub": bool(stub),
        "runCount": args.runs,
        "allRunsPass": allPass,
        "storage": storage,
        "runs": runs,
    }
    Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    if allPass:
        print("ALL RUNS PASS")
    return 0 if allPass else 1


if __name__ == "__main__":
    sys.exit(main())
