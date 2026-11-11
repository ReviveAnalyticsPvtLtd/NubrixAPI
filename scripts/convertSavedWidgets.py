"""The verified conversion of saved dashboard widgets: a one-off operator job, run only with its own approval.

    python -m scripts.convertSavedWidgets --project ID --out REPORT            (dry run: writes nothing)
    python -m scripts.convertSavedWidgets --project ID --out REPORT --write
    python -m scripts.convertSavedWidgets ... --model-stub STUB.jsonl          (tests: recorded model replies)

It reads the project's `dashboardConfig.json` and takes every saved widget whose `generatedCode` is model-written
Python. For each one, one at a time, it asks the SQL chart workflow for a chart request, describing the chart by the
widget's stored `query` or, without one, by its Python code, against the project's metadata as a served request sees it
(`utils.modelMetadata`). It then runs that request the way a dashboard refresh runs a saved chart request: unfiltered,
lenient, through `renderChartRequest`. The request carries the widget's saved title, because a refresh shows the
request's title.

A widget is converted only when users would see no change, which needs both:
  - `compareCharts(saved, new, relTol=1e-9)` is "match" (it ignores order, so it is not enough on its own);
  - `visibleDifference(saved, new)` finds nothing: every chart key a refresh replaces (`chartType`, `xLabels`,
    `yLabels`, `label`, `columns`, `data`, `map`) is equal in order, as the widget is shown (map point labels filled).
    The rules are in `visibleDifference`.
Only a converted widget's `generatedCode` changes; its title, layout, id and saved data stay as they are.

Candidates: a widget whose `decodeGeneratedCode` is None, that is not a `panelChart(...)` template, not a
`# Failed to generate code` card, and has code. Every other widget is skipped with a reason. A doubt, a failure, a
timeout or a busy sandbox is that widget's verdict and leaves it as it is. A sandbox slot is waited for as a served
request waits.

`--write` changes the live config only when at least one widget matched, in this order:
  1. the config's exact bytes were kept when it was read;
  2. `dashboardConfig.backup-<UTC time>.json` is uploaded with those bytes (never over an existing file), read back
     and compared byte for byte;
  3. the live config is read again and must still be those bytes, else the dashboard was edited meanwhile;
  4. the new config, written with the service's JSON settings, is uploaded and read back.
A failure at 2 or 3 aborts with the live config untouched. A failure at 4 leaves the live config unverified and the
backup names the original.

Nothing is charged: the workflow runs without the credit callback, and Langfuse traces only when its keys are already
in the environment. No `.env` file is read. Redis is cut off for the run (`scripts.chartEval._noRedis`): table and
schema reads go to storage, the size hints come from the metadata read here. A dry run uploads nothing.

Standard output holds only the counts and the backup name. The report (`--out`, a local JSON file) holds every
widget's page, id and verdict: `match` with the new chart request, `differ` with the first differing field, `failed`
with its step and stage, or `skipped` with its reason, plus the backup name and the counts.

Exit code: 0 when the job finished (also when nothing matched), 1 when it aborted (the config could not be read, the
backup could not be verified, the dashboard was edited meanwhile, or the write could not be verified), 2 for a usage
error (nothing was read).
"""

__all__ = ["main", "verdictOf", "visibleDifference"]

import argparse
import ast
import copy
import datetime as dt
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock

from pydantic import ValidationError

from nubrix.components.chartCompare import compareCharts
from nubrix.components.chartEngine import ChartFailure, renderChartRequest
from nubrix.components.chartRequest import ChartRequest, decodeGeneratedCode, encodeGeneratedCode
from scripts import chartEval
from utils.mapPointLabels import ensureScatterMapPointLabels

CONFIG = "dashboardConfig.json"
METADATA = "metadata.json"
REL_TOL = 1e-9
FAILED_CODE_PREFIX = "# Failed to generate code"
# The chart keys a refresh of a saved chart request replaces (`dashboardService._CHART_KEYS`), title aside: the
# request carries the saved title. `columns` comes before `data` so a table's column order is named first.
VISIBLE_KEYS = ("chartType", "xLabels", "yLabels", "label", "columns", "data", "map")
_PANEL_DEF = re.compile(r"^\s*def\s+panelChart\s*\(", re.MULTILINE)
_DETAIL_CHARS = 2000


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


def _sameScalar(a: Any, b: Any, relTol: float) -> bool:
    """Integers exactly, other numbers within `relTol`; a bool is never a number; NaN and infinity never match."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _isNumber(a) or _isNumber(b):
        if not (_isNumber(a) and _isNumber(b)) or not (math.isfinite(a) and math.isfinite(b)):
            return False
        if isinstance(a, int) or isinstance(b, int):
            # An integer is exact: 20 equals 20.0, never 20.000000001.
            return float(a).is_integer() and float(b).is_integer() and int(a) == int(b)
        return math.isclose(a, b, rel_tol=relTol, abs_tol=0.0)
    if a is None or b is None:
        return a is None and b is None
    return type(a) is type(b) and a == b


def _difference(a: Any, b: Any, path: str, relTol: float, orderedKeys: bool) -> str | None:
    """The path of the first difference, or None. Lists compare in order; dicts by their keys, in order when `orderedKeys`."""
    if isinstance(a, dict) or isinstance(b, dict):
        if not (isinstance(a, dict) and isinstance(b, dict)):
            return path
        keysA, keysB = list(a), list(b)
        if (keysA != keysB) if orderedKeys else (set(keysA) != set(keysB)):
            return path
        for key in keysA:
            found = _difference(a[key], b[key], f"{path}.{key}", relTol, orderedKeys)
            if found:
                return found
        return None
    if isinstance(a, list) or isinstance(b, list):
        if not (isinstance(a, list) and isinstance(b, list)) or len(a) != len(b):
            return path
        for index, (x, y) in enumerate(zip(a, b)):
            found = _difference(x, y, f"{path}[{index}]", relTol, orderedKeys)
            if found:
                return found
        return None
    return None if _sameScalar(a, b, relTol) else path


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
    when it has one, else its first row's keys. Integers are exact, other numbers within `relTol`, a bool is never a
    number, NaN and infinity never match, and text is exact. The title, id, layout and keys a refresh does not
    replace are not compared.

    Returns:
        str | None: A path such as `data.datasets[0].data[3]` or `xLabels`. It can name a table column or a pivot
            key, so it belongs in the local report only.
    """
    saved, new = _asShown(saved), _asShown(new)
    chartType = saved.get("chartType")
    for key in VISIBLE_KEYS:
        a, b = saved.get(key), new.get(key)
        if key == "columns" and chartType == "table":
            a, b = _tableColumns(saved), _tableColumns(new)
        found = _difference(a, b, key, relTol, orderedKeys=key == "data" and chartType in ("table", "pivot"))
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


# Candidates


def _isPanelTemplate(code: str) -> bool:
    """A manual Panel widget: its code defines and calls `panelChart` at the top level (fences allowed)."""
    body = "\n".join(line for line in code.splitlines() if not line.strip().startswith("```"))
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


# Generation and the run


class _Engine(chartEval._Engine):
    """The SQL chart workflow, built like the evaluation harness builds it (with its model stub when given)."""

    def __init__(self, replay):
        super().__init__("sql", replay)

    def invoke(self, metadata: dict, prompt: str, project: str) -> dict:
        """One workflow run with the Langfuse handler when its keys are set, and never the credit callback."""
        self.failure = None
        config = None
        if self.replay is None:
            from utils.llm import getLangfuseConfig

            config = getLangfuseConfig(
                trace_name="ReportingWorkflow", projectId=project, tags=["reporting", "widgetConversion"], perRequestHandler=True,
            ) or None
        state = {"metadata": metadata, "inputQuery": prompt, "projectId": project}
        with mock.patch.object(self.module, "logChartFailure", self._recordFailure):
            return self.workflow.invoke(state, config=config)


def _clipped(value: Any) -> str:
    return str(value)[:_DETAIL_CHARS]


def _generated(engine, metadata: dict, description: str, project: str) -> ChartRequest | dict:
    """The chart request the workflow made, or the failure `{"stage", "detail"}`."""
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
    return request


def _rendered(manager, project: str, request: ChartRequest, activeTables: frozenset) -> dict:
    """The chart a dashboard refresh of the saved request would show, or the failure `{"stage", "detail"}`."""
    from utils.sandboxSlots import SandboxBusyError, isBusy

    try:
        return {"chart": renderChartRequest(manager, project, request, activeTables, filters=None, lenient=True)}
    except ChartFailure as error:
        return {"stage": error.stage, "detail": _clipped(error.message)}
    except SandboxBusyError as error:
        return {"stage": "busy", "detail": _clipped(error)}
    except Exception as error:
        return {"stage": "busy" if isBusy(error) else "run", "detail": _clipped(f"{type(error).__name__}: {error}")}


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


def _convertWidget(engine, manager, metadata: dict, activeTables: frozenset, project: str, widget: dict) -> dict:
    """The verdict of one candidate."""
    began = time.monotonic()
    entry: dict = {}
    if _withTitle(_TITLE_PROBE, widget.get("title")) is None:
        # A refresh would show the request's title, and no request can hold this one.
        entry.update(verdict="differ", field="title")
    else:
        replay = getattr(engine, "replay", None)
        if replay is not None:
            replay.caseId = widget.get("id")
        generated = _generated(engine, metadata, _description(widget), project)
        if isinstance(generated, dict):
            entry.update(verdict="failed", step="generate", **generated)
        else:
            request = _withTitle(generated, widget.get("title"))
            run = _rendered(manager, project, request, activeTables)
            if "chart" not in run:
                entry.update(verdict="failed", step="render", **run)
            else:
                verdict, field = verdictOf(widget, run["chart"])
                entry["verdict"] = verdict
                if verdict == "match":
                    entry.update(chartRequest=request.model_dump(exclude_none=True), generatedCode=encodeGeneratedCode(request))
                else:
                    entry["field"] = field
    entry["durationSeconds"] = round(time.monotonic() - began, 3)
    return entry


# Storage


class StorageStore:
    """The project's folder in the AnalyticsHub bucket, read and written as the service does."""

    def __init__(self, project: str):
        import os

        from supabase import ClientOptions

        from utils.supabaseClient import createSupabaseClient

        self.project = project
        client = createSupabaseClient(
            os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"], ClientOptions(auto_refresh_token=False, persist_session=False),
        )
        self.bucket = client.storage.from_("AnalyticsHub")

    def read(self, name: str) -> bytes:
        from utils.projectStorage import readBytes

        return readBytes(self.project, name)

    def write(self, name: str, data: bytes, overwrite: bool) -> None:
        # The service's own call; without `upsert` storage refuses to replace an existing file.
        self.bucket.upload(path=f"{self.project}/{name}", file=data, file_options={"upsert": "true" if overwrite else "false"})


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


def _write(store, original: bytes, newBytes: bytes, report: dict, save) -> None:
    """Backup, check for a concurrent edit, write, verify. Every abort before the write leaves the live config untouched."""
    backup = f"dashboardConfig.backup-{_utcNow().strftime('%Y%m%dT%H%M%SZ')}.json"
    report["backup"] = backup
    save()
    try:
        store.write(backup, original, overwrite=False)
        if bytes(store.read(backup)) != original:
            raise ValueError("the backup read back differs")
    except Exception:
        raise _Abort("backupNotVerified")
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
    }


def _convert(args, store, stub: dict | None, report: dict, save) -> None:
    from utils.codeExecutor import REPLManager
    from utils.modelMetadata import activeTables, metadataForModel
    from nubrix.workflows.sqlChartWorkflow import RUN_TIMEOUT_SECONDS

    original, config = _readConfig(store)
    raw = json.loads(store.read(METADATA))
    # The metadata of a served request; the row counts for the size hints come from the metadata just read.
    metadata = metadataForModel(copy.deepcopy(raw), args.project, knownMetadata=raw)
    tables = frozenset(activeTables(raw))
    engine = _Engine(chartEval._Replay(stub) if stub is not None else None)
    manager = REPLManager(timeoutSeconds=RUN_TIMEOUT_SECONDS)

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
                entry.update(_convertWidget(engine, manager, metadata, tables, args.project, widget))
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
    _write(store, original, _configBytes(updated), report, save)
    report.update(outcome="written", written=replaced)


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
        with chartEval._quietLogs(), chartEval._noRedis():
            target = store if store is not None else StorageStore(args.project)
            _convert(args, target if args.write else _ReadOnly(target), stub, report, save)
    except _Abort as abort:
        report.update(outcome="aborted", abortReason=abort.reason)
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
    print(f"written {report['written']}")
    print(f"backup {report['backup'] or 'none'}")
    if code:
        reason = report["abortReason"]
        print(f"{parser.prog}: aborted ({reason}); the live dashboard config "
              + ("may hold the new config: check it against the backup." if reason == "writeNotVerified" else "was not changed."),
              file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
