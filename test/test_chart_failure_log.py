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
        ("column col_2 and x2 are redacted", "column ? and ? are redacted"),
    ],
)
def test_failureLogRedactsNumberShapes(records, message, expected):
    assert logOne(records, message)["error"] == expected


def test_failureLogHandlesEscapedQuoteInsideLiteral(records):
    record = logOne(records, "Could not convert string 'O''Brien' to INT32")
    assert record["error"] == "Could not convert string '?' to INT32"


def test_failureLogKeepsOnlyKnownTableNamesInDoubleQuotes(records):
    message = 'Catalog Error: Table with name "sales_2024" does not exist, near "North"'
    record = logOne(records, message, tableNames=["sales_2024"])
    assert record["error"] == 'Catalog Error: Table with name "sales_2024" does not exist, near "?"'


C1_CASES = [
    'Binder Error: Referenced column "North" not found in FROM clause!',
    'Binder Error: Referenced column "Acme Corp" not found in FROM clause!',
    "Parser Error: syntax error at or near \"'Smith'\"",
    'Parser Error: syntax error at or near "5000"',
    'Parser Error: syntax error at or near "North"',
    'Conversion Error: date field value out of range: "2024-13-01"',
    'Conversion Error: Could not parse string "abc" according to format specifier',
    'Conversion Error: invalid timestamp field format: "2024-13-45 25:00:00", expected format is (YYYY-MM-DD hh:mm:ss)',
    'Conversion Error: Could not convert string "SP" to INT32',
    'unterminated "ident Alice 55',
]


@pytest.mark.parametrize("message", C1_CASES)
def test_failureLogRedactsEveryDoubleQuotedTokenWithoutTableNames(records, message):
    error = logOne(records, message)["error"]
    for leaked in ("North", "Acme", "Smith", "5000", "2024", "abc", "SP", "Alice", "55", "25"):
        assert leaked not in error
    assert '"?"' in error


@pytest.mark.parametrize("message", C1_CASES)
def test_failureLogRedactsDoubleQuotedValuesEvenWhenTableNamesAreGiven(records, message):
    error = logOne(records, message, tableNames=["sales", "orders_2024"])["error"]
    for leaked in ("North", "Acme", "Smith", "5000", "abc", "SP", "Alice"):
        assert leaked not in error


def test_failureLogDoesNotKeepATableNameThatOnlyAppearsInsideAValue(records):
    record = logOne(records, 'near "sales Smith"', tableNames=["sales"])
    assert record["error"] == 'near "?"'


@pytest.mark.parametrize(
    "message, expected",
    [
        ("Could not convert string 'D'Souza' to INT32", "Could not convert string '?' to INT32"),
        ("Could not convert string 'Mary's Shop' to INT32", "Could not convert string '?' to INT32"),
        ("Could not convert string 'x'y' to DATE", "Could not convert string '?' to DATE"),
        ("a 'abc' and 'def' b", "a '?' and '?' b"),
        ("it's value 'abc' it's", "it's value '?' it's"),
    ],
)
def test_failureLogRedactsLiteralsWithApostrophes(records, message, expected):
    assert logOne(records, message)["error"] == expected


def test_failureLogRedactsALiteralThatNeverCloses(records):
    assert logOne(records, "Could not convert string 'a'b to INT32")["error"] == "Could not convert string '?'"


@pytest.mark.parametrize(
    "message, leaked",
    [
        ("value 0xFF bad", "FF"),
        ("value 0xDEADBEEF bad", "DEADBEEF"),
        ("uuid 123e4567-e89b-12d3-a456-426614174000 bad", "e89b"),
        ("date 2024-01-05T10:30:00Z bad", "10"),
        ("weight 5kg bad", "kg"),
        ("weight 5.5kg bad", "kg"),
        ("ID 4411223344abc", "abc"),
        ("phone +91-98765-43210 x", "98765"),
        ("ip 192.168.1.10 bad", "168"),
    ],
)
def test_failureLogLeavesNoDigitTokenFragments(records, message, leaked):
    error = logOne(records, message)["error"]
    assert not any(character.isdigit() for character in error)
    assert leaked not in error


@pytest.mark.parametrize(
    "typeName",
    ["INT8", "INT16", "INT32", "INT64", "INT128", "UINT8", "UINT32", "UINT64", "FLOAT4", "FLOAT8", "TINYINT", "SMALLINT", "BIGINT", "HUGEINT", "UHUGEINT"],
)
def test_failureLogKeepsDuckdbTypeNames(records, typeName):
    assert logOne(records, f"cannot cast {typeName} here")["error"] == f"cannot cast {typeName} here"


def test_failureLogDecimalTypeKeepsItsShapeNotItsNumbers(records):
    assert logOne(records, "cannot cast to DECIMAL(18,3)")["error"] == "cannot cast to DECIMAL(?,?)"


def test_failureLogRedactsUnterminatedLiteral(records):
    record = logOne(records, "Parser Error: unterminated 'secret value")
    assert "secret" not in record["error"]


def test_moduleDocstringDoesNotOverclaim():
    assert "No SQL, no user text, no data values" not in module.__doc__


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


def test_errorWithALogMessageIsLoggedThroughIt(records):
    from nubrix.components.chartBuilder import ChartBuildError

    error = ChartBuildError("more than one row for pivot row Alice and column North", logMessage="more than one row for a pair")
    record = logOne(records, error, stage="build")
    assert record["error"] == "more than one row for a pair"
    assert record["errorClass"] == "ChartBuildError"


def test_logMessageIsStillRedacted(records):
    from nubrix.components.chartBuilder import ChartBuildError

    error = ChartBuildError("full", logMessage="x 'Alice' and 42")
    assert logOne(records, error)["error"] == "x '?' and ?"


@pytest.mark.parametrize(
    "message, secret",
    [
        ("near E'secret' here", "secret"),
        ("near x'Smith' here", "Smith"),
        ("near b'abc' here", "abc"),
        ("near LIKE'%Smith%' here", "Smith"),
        ("near N'Jones' here", "Jones"),
    ],
)
def test_failureLogRedactsLiteralsGluedToALetter(records, message, secret):
    error = logOne(records, message)["error"]
    assert secret not in error
    assert "'?'" in error


@pytest.mark.parametrize(
    "message",
    ["can't cast this", "doesn't match", "it's wrong", "they're missing", "we've seen I'm sure", "you'll see", "he'd go", "Can't Stop"],
)
def test_failureLogKeepsContractions(records, message):
    assert logOne(records, message)["error"] == message


def test_requireNumericLogMessageNamesNoStructFields():
    import pyarrow as pa

    from nubrix.components.chartBuilder import ChartBuildError, buildChart
    from nubrix.components.chartRequest import ChartRequest

    request = ChartRequest(chartType="bar", title="t", sql="SELECT 1", encoding={"x": "a", "y": "b"})
    table = pa.table({"a": ["x"], "b": [{"SecretField": 1}]})
    with pytest.raises(ChartBuildError) as caught:
        buildChart(request, table)
    assert "SecretField" not in caught.value.logMessage
