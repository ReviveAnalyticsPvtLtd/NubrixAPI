import json
from types import SimpleNamespace

import pytest

from nubrix.components import chartFailureLog as module


@pytest.fixture
def records(monkeypatch):
    lines = []
    monkeypatch.setattr(module.logger, "warning", lambda message, *a, **k: lines.append(message))
    return lines


def logOne(records, error, **extra):
    args = dict(stage="run", error=error, traceId="t1", projectId="p1", chartType="bar", repairs=1)
    args.update(extra)
    module.logChartFailure(**args)
    assert len(records) == 1
    assert records[0].startswith("chart_failure ")
    return json.loads(records[0][len("chart_failure "):])


def test_failureLogRedactsLiterals(records):
    record = logOne(records, "Conversion Error: Could not convert string 'SP' to INT32\nLINE 1: ...")
    assert record["error"] == "Conversion Error: Could not convert string '?' to INT32"


def test_failureLogRedactsUnquotedNumbers(records):
    record = logOne(
        records,
        "Conversion Error: Type INT64 with value 3000000000 can't be cast because the value is out of range for the destination type INT32",
    )
    assert record["error"] == (
        "Conversion Error: Type INT64 with value ? can't be cast because the value is out of range for the destination type INT32"
    )


@pytest.mark.parametrize(
    "message, expected",
    [
        ("bad value 12.5 here", "bad value ? here"),
        ("date/time field value out of range: 2024-13-01", "date/time field value out of range: ?"),
        ("bad time 10:30:00 here", "bad time ? here"),
        ("Could not cast FLOAT8 1e5 to INT32", "Could not cast FLOAT8 ? to INT32"),
        ("column col_2 and x2 stay", "column col_2 and x2 stay"),
    ],
)
def test_failureLogRedactsNumberShapes(records, message, expected):
    assert logOne(records, message)["error"] == expected


def test_failureLogHandlesEscapedQuoteInsideLiteral(records):
    record = logOne(records, "Could not convert string 'O''Brien' to INT32")
    assert record["error"] == "Could not convert string '?' to INT32"


def test_failureLogKeepsDoubleQuotedIdentifiers(records):
    record = logOne(records, 'Binder Error: Referenced column "revenue 2024" not found, value \'x\'')
    assert record["error"] == 'Binder Error: Referenced column "revenue 2024" not found, value \'?\''


def test_failureLogRedactsUnterminatedLiteral(records):
    record = logOne(records, "Parser Error: unterminated 'secret value")
    assert "secret" not in record["error"]


def test_failureLogTruncatesToThreeHundredAfterRedaction(records):
    record = logOne(records, "x" * 290 + " '" + "y" * 100 + "' tail")
    assert len(record["error"]) <= 300
    assert "y" not in record["error"]


def test_failureLogCutNeverLeavesHalfALiteral(records):
    record = logOne(records, "z" * 296 + " 'abcdefghij' end")
    assert len(record["error"]) == 300
    assert "abc" not in record["error"]


def test_failureLogHasNoSqlOrQuestion(records):
    record = logOne(records, ValueError("boom"), shadow=True)
    assert sorted(record) == sorted(
        ["stage", "errorClass", "error", "traceId", "projectId", "chartType", "repairs", "shadow"]
    )
    assert record["shadow"] is True
    assert record["stage"] == "run"


def test_errorClassIsTheExceptionClassName(records):
    record = logOne(records, KeyError("secret"))
    assert record["errorClass"] == "KeyError"


def test_errorClassForStringIsFixed(records):
    record = logOne(records, "plain text 'a'")
    assert record["errorClass"] == "str"


def test_traceIdOf():
    assert module.traceIdOf(None) is None
    assert module.traceIdOf({}) is None
    assert module.traceIdOf({"callbacks": []}) is None
    first = SimpleNamespace()
    empty = SimpleNamespace(last_trace_id=None)
    withId = SimpleNamespace(last_trace_id="abc")
    other = SimpleNamespace(last_trace_id="zzz")
    assert module.traceIdOf({"callbacks": [first, empty, withId, other]}) == "abc"
    assert module.traceIdOf({"callbacks": [first]}) is None
