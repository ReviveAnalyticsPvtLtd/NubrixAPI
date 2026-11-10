"""The chart evaluation harness.

Runs a set of prompts through the SQL chart workflow (`--engine sql`) or the Python one the service serves while
CHART_ENGINE is `off` (`--engine python`), in this process, and compares each chart with the expected answer.

    python -m scripts.chartEval --cases cases.jsonl --data DATA --project ID --engine sql --runs 3 --out report.json
    python -m scripts.chartEval ... --model-stub modelStub.jsonl      (sql only; replays recorded model replies)

`DATA` is a folder holding `<projectId>/<table>.parquet` and `<projectId>/metadata.json`. It is served through a local
storage server (`scripts/localStorageServer.py`), so the real sandbox path runs: schema reads, spool, channel, launcher.
The harness never reads `.env` files: the Gemini key comes from the process environment, and Langfuse is used only when
its keys are already there. It charges no credits, writes to no database and talks to no storage but the local one.

Case line (JSONL), fields left null are not checked:
    {"id": str, "prompt": str, "expect": {"doubt": bool, "chartType": str | null, "labels": [str] | null,
     "values": {datasetLabel: [number | null]} | null, "rows": [dict] | null, "card": number | str | null,
     "relTol": float}}

Model stub line (JSONL, one per case id): {"id": str, "rephrased": {"rephrasedOutput": str | null, "doubt": str | null},
"reply": str}. `rephrased` replaces the rephraser, `reply` replaces the generator and every repair.

Standard output is `PASS k/n` per run, the ids of failed cases, and `ALL RUNS PASS`. Everything else, values included,
goes to the report file. The exit code is 0 only when every run passes every case.
"""

__all__ = ["main", "checkValues"]

import argparse
import contextlib
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

from nubrix.components.chartContract import contractErrors

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_REL_TOL = 0.0


# The value check


def _typeName(value: Any) -> str:
    return type(value).__name__


def _length(value: Any) -> int | None:
    return len(value) if isinstance(value, (list, dict, str)) else None


def _isNumber(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _same(expected: Any, got: Any, relTol: float) -> bool:
    """One value: numbers within `relTol` (exact at 0), text and everything else exactly, a null only to a null."""
    if expected is None or got is None:
        return expected is None and got is None
    if isinstance(expected, bool) or isinstance(got, bool):
        return isinstance(expected, bool) and isinstance(got, bool) and expected == got
    if _isNumber(expected):
        if not _isNumber(got) or not (math.isfinite(expected) and math.isfinite(got)):
            return False
        if relTol <= 0 or expected == 0:
            return got == expected
        return abs(got - expected) <= relTol * abs(expected)
    if isinstance(expected, dict):
        return isinstance(got, dict) and set(expected) == set(got) and all(_same(expected[k], got[k], relTol) for k in expected)
    if isinstance(expected, list):
        return isinstance(got, list) and len(expected) == len(got) and all(_same(e, g, relTol) for e, g in zip(expected, got))
    return type(expected) is type(got) and expected == got


def _mismatch(field: str, expected: Any, got: Any, **extra) -> dict:
    """A failed check described by types and lengths; the values themselves stay out of it."""
    return {
        "ok": False,
        "field": field,
        "expectedType": _typeName(expected),
        "gotType": _typeName(got),
        "expectedLength": _length(expected),
        "gotLength": _length(got),
        **extra,
    }


def _firstDifference(expected: list, got: list, relTol: float) -> int | None:
    for index, (e, g) in enumerate(zip(expected, got)):
        if not _same(e, g, relTol):
            return index
    return None


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


def checkValues(expect: dict, chart: dict) -> dict:
    """Compare a chart with an expectation. Returns `{"ok": True}` or the first failed field.

    `labels` and `rows` are compared in order. `values` maps a dataset label to a list aligned with `labels`; the
    datasets must be exactly the expected ones, matched by label. Numbers use `relTol` (exact at 0), text compares
    exactly, and a null expected field is not checked. A pivot's row keys are its labels and its column keys its datasets.
    """
    relTol = float(expect.get("relTol") or _DEFAULT_REL_TOL)
    labels = expect.get("labels")
    if labels is not None:
        got = _gotLabels(chart)
        if not _same(labels, got, 0):
            index = _firstDifference(labels, got, 0) if isinstance(got, list) else None
            return _mismatch("labels", labels, got, index=index)
    values = expect.get("values")
    if values is not None:
        got = _gotValues(chart)
        if got is None or set(values) != set(got):
            return _mismatch("values", values, got, dataset=None)
        for name, expected in values.items():
            if not isinstance(got[name], list) or not _same(expected, got[name], relTol):
                index = _firstDifference(expected, got[name], relTol) if isinstance(got[name], list) else None
                return _mismatch("values", expected, got[name], dataset=name, index=index)
    rows = expect.get("rows")
    if rows is not None:
        got = chart.get("data")
        if not isinstance(got, list) or not _same(rows, got, relTol):
            index = _firstDifference(rows, got, relTol) if isinstance(got, list) else None
            return _mismatch("rows", rows, got, index=index)
    card = expect.get("card")
    if card is not None:
        got = chart.get("data")
        if not _same(card, got, relTol):
            return _mismatch("card", card, got)
    return {"ok": True}


# Cases and the model stub


def _readJsonLines(path: str, what: str) -> list[dict]:
    """The objects of a JSONL file. An error names the line only: the files can hold private prompts and answers."""
    entries = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise SystemExit(f"Cannot read the {what} file ({type(error).__name__}).")
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            raise SystemExit(f"The {what} file has invalid JSON on line {number}.")
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise SystemExit(f"The {what} file needs an object with a string id on line {number}.")
        entries.append(entry)
    return entries


def _loadCases(path: str) -> list[dict]:
    cases = _readJsonLines(path, "cases")
    seen = set()
    for number, case in enumerate(cases, 1):
        if not isinstance(case.get("prompt"), str) or not isinstance(case.get("expect"), dict):
            raise SystemExit(f"Case {number} needs a string prompt and an expect object.")
        if case["id"] in seen:
            raise SystemExit(f"Case id repeated: {case['id']}")
        seen.add(case["id"])
    return cases


def _loadStub(path: str, cases: list[dict]) -> dict[str, dict]:
    stub = {}
    for entry in _readJsonLines(path, "model stub"):
        if not isinstance(entry.get("rephrased"), dict) or not isinstance(entry.get("reply"), str):
            raise SystemExit(f"The model stub entry {entry['id']} needs a rephrased object and a reply string.")
        stub[entry["id"]] = entry
    missing = [case["id"] for case in cases if case["id"] not in stub]
    if missing:
        raise SystemExit("The model stub has no entry for: " + ", ".join(missing))
    return stub


class _Replay:
    """The recorded model: one reply per case id. The rephraser, the generator and every repair read the current case."""

    def __init__(self, stub: dict[str, dict]):
        self.stub = stub
        self.caseId: str | None = None

    def chain(self, key: str):
        replay = self

        class Chain:
            def invoke(self, _inputs, _config=None):
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
def _environment(stubbed: bool, dataFolder: str):
    """The process environment of a run, restored afterwards: the local storage server and nothing else to reach."""
    from scripts.localStorageServer import serveFolder

    key = secrets.token_urlsafe(24)
    changed = ["SUPABASE_KEY", "REDIS_HOST", "REDIS_PORT", "REDIS_PASSWORD", "SUPABASE_URL"]
    # Left alone when already set. SANDBOX_ALLOW_SAME_UID is the local-development switch; an image that sets
    # SANDBOX_RUN_ROOT gets its own uids as always. A stubbed model needs no key, only a value for modules that read one.
    defaults = {"SECRET_KEY": "local-eval-only", "SANDBOX_ALLOW_SAME_UID": "1"}
    if stubbed:
        defaults.update(GEMINI_API_KEY="local-eval-placeholder", GROQ_API_KEY="local-eval-placeholder", GOOGLE_API_KEY="local-eval-placeholder")
    saved = {name: os.environ.get(name) for name in changed + list(defaults)}
    url, stop = serveFolder(dataFolder, key=key)
    try:
        # No Redis: the schema cache and the project slots are the cache-less path, and nothing of a run lands in a real one.
        os.environ.update(SUPABASE_URL=url, SUPABASE_KEY=key, REDIS_HOST="127.0.0.1", REDIS_PORT="1", REDIS_PASSWORD="")
        for name, value in defaults.items():
            os.environ.setdefault(name, value)
        yield
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


class _Engine:
    """Builds the workflow of an engine and runs one prompt through it."""

    def __init__(self, engine: str, replay: _Replay | None):
        self.engine = engine
        self.replay = replay
        self.failedStage: str | None = None
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
        self.failedStage = fields.get("stage")

    def invoke(self, metadata: dict, prompt: str, project: str) -> dict:
        self.failedStage = None
        config = None
        if self.replay is None:
            from utils.llm import getLangfuseConfig

            config = getLangfuseConfig(
                trace_name="ReportingWorkflow", projectId=project, tags=["reporting", "evaluation"], perRequestHandler=True,
            ) or None
        state = {"metadata": metadata, "inputQuery": prompt, "projectId": project}
        if self.engine == "sql":
            with mock.patch.object(self.module, "logChartFailure", self._recordFailure):
                return self.workflow.invoke(state, config=config)
        return self.workflow.invoke(state, config=config)


def _judge(case: dict, response: dict, failedStage: str | None) -> dict:
    """The case's result fields from the workflow's response."""
    expect = case["expect"]
    output = response.get("finalOutput")
    result: dict = {"id": case["id"], "ok": False, "stage": None, "chartType": None, "contractErrors": [], "valueCheck": None}
    if not isinstance(output, dict):
        result["stage"] = "noOutput"
        return result
    if "error" in output:
        result["stage"] = failedStage or "error"
        return result
    if "response" in output and "chartType" not in output:
        result["stage"] = "doubt" if expect.get("doubt") else "unexpectedDoubt"
        result["ok"] = bool(expect.get("doubt"))
        return result
    result["chartType"] = output.get("chartType")
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
        result = _judge(case, response, engine.failedStage)
    except Exception as error:
        # The class only: an exception's text can hold table names or data.
        result = {"id": case["id"], "ok": False, "stage": f"exception:{type(error).__name__}", "chartType": None,
                  "contractErrors": [], "valueCheck": None}
    result["durationSeconds"] = round(time.monotonic() - began, 3)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.chartEval", description=__doc__.split("\n\n")[0])
    parser.add_argument("--cases", required=True, help="JSONL file of cases")
    parser.add_argument("--data", required=True, help="folder holding <projectId>/<table>.parquet and metadata.json")
    parser.add_argument("--project", required=True, help="project id: the folder under --data")
    parser.add_argument("--engine", choices=("sql", "python"), default="sql")
    parser.add_argument("--runs", type=int, default=1, help="how many times to run every case")
    parser.add_argument("--out", required=True, help="path of the JSON report")
    parser.add_argument("--model-stub", help="JSONL of recorded model replies by case id (sql engine only)")
    parser.add_argument("--only", action="append", metavar="ID", help="run only this case id (repeatable)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.model_stub and args.engine != "sql":
        parser.error("--model-stub works with --engine sql only")
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if not (Path(args.data) / args.project).is_dir():
        parser.error("--data has no folder for --project")
    cases = _loadCases(args.cases)
    if args.only:
        unknown = sorted(set(args.only) - {case["id"] for case in cases})
        if unknown:
            parser.error("unknown case id: " + ", ".join(unknown))
        cases = [case for case in cases if case["id"] in args.only]
    stub = _loadStub(args.model_stub, cases) if args.model_stub else None

    from loguru import logger

    runs = []
    # The workflow logs table names and project ids; none of that belongs on standard output.
    logger.disable("")
    try:
        with _environment(stub is not None, args.data):
            from utils import projectStorage

            metadata = projectStorage.readJson(args.project, "metadata.json")
            engine = _Engine(args.engine, _Replay(stub) if stub is not None else None)
            for number in range(1, args.runs + 1):
                results = [_runCase(engine, metadata, case, args.project) for case in cases]
                passed = sum(result["ok"] for result in results)
                runs.append({"run": number, "passed": passed, "total": len(results), "cases": results})
                print(f"PASS {passed}/{len(results)}")
                for result in results:
                    if not result["ok"]:
                        print(f"FAILED {result['id']}")
    finally:
        logger.enable("")
    allPass = all(run["passed"] == run["total"] for run in runs)
    report = {
        "gitSha": _gitSha(),
        "engine": args.engine,
        "project": args.project,
        "modelStub": bool(stub),
        "runCount": args.runs,
        "allRunsPass": allPass,
        "runs": runs,
    }
    Path(args.out).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    if allPass:
        print("ALL RUNS PASS")
    return 0 if allPass else 1


if __name__ == "__main__":
    sys.exit(main())
