"""The chart engine ties the SQL check, the sandbox run and the chart builder together (the sandbox is stubbed)."""

import json
import struct

import pyarrow as pa
import pytest

from nubrix.components.chartEngine import ChartFailure, renderChartRequest
from nubrix.components.chartRequest import ChartRequest
from utils import sqlRunner
from utils.codeExecutor import REPLManager
from utils.sandboxRunner import ResultTooLargeError, SandboxResult
from utils.sandboxSlots import SandboxBusyError

PROJECT = "project-1"
TABLES = ["sales", "Order-Items"]
BAR_SQL = "SELECT state, sum(amount) AS total FROM sales GROUP BY state"


def _bar(sql=BAR_SQL):
    return ChartRequest(chartType="bar", title="Sales by state", sql=sql, encoding={"x": "state", "y": ["total"]})


def _card(sql="SELECT sum(amount) AS total FROM sales"):
    return ChartRequest(chartType="card", title="Total", sql=sql, encoding={"value": "total"})


def _arrowStream(table, **options):
    """The launcher's result format; `options` are IpcWriteOptions a launcher that works never uses."""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema, options=pa.ipc.IpcWriteOptions(**options)) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _resultOf(resultBytes, truncated=False):
    return SandboxResult(json.dumps({"ok": True, "truncated": truncated, "rows": 1}) + "\n", "", 0, resultBytes)


def _success(table, truncated=False):
    return _resultOf(_arrowStream(table), truncated)


def _tableChart(sql="SELECT * FROM sales"):
    return ChartRequest(chartType="table", title="Rows", sql=sql, encoding={})


def _handled(stage, message):
    return SandboxResult(json.dumps({"ok": False, "stage": stage, "message": message}) + "\n", "", 0, None)


class _Manager(REPLManager):
    """A REPLManager whose `runSql` answers with one canned result (or raises it) and records its calls."""

    def __init__(self, answer):
        super().__init__(timeoutSeconds=5)
        self.answer = answer
        self.calls = []

    def runSql(self, request, projectId, waitForSlot=True):
        self.calls.append((request, projectId, waitForSlot))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _failure(manager, request, **kwargs):
    with pytest.raises(ChartFailure) as raised:
        renderChartRequest(manager, PROJECT, request, TABLES, **kwargs)
    return raised.value


def test_successBuildsTheChart():
    manager = _Manager(_success(pa.table({"state": ["SP", "RJ"], "total": [3, 1]})))

    chart = renderChartRequest(manager, PROJECT, _bar(), TABLES)

    assert chart["chartType"] == "bar"
    assert chart["data"]["labels"] == ["SP", "RJ"]
    assert chart["data"]["datasets"][0]["data"] == [3, 1]


def test_childRequestCarriesTheCheckedQueryTheFiltersAndTheCaps():
    manager = _Manager(_success(pa.table({"state": ["SP"], "total": [3]})))
    filters = [{"state": "SP"}]

    renderChartRequest(manager, PROJECT, _bar(), TABLES, filters=filters, waitForSlot=False)

    request, projectId, waitForSlot = manager.calls[0]
    assert request == {"sql": BAR_SQL, "tables": ["sales"], "filters": filters, "requiredColumns": ["state", "total"], "rowLimit": 5000}
    assert (projectId, waitForSlot) == (PROJECT, False)


def test_tablesAreSentWithTheirProjectNames():
    manager = _Manager(_success(pa.table({"n": [1]})))
    request = ChartRequest(chartType="card", title="Items", sql='SELECT count(*) AS n FROM "order-items"', encoding={"value": "n"})

    renderChartRequest(manager, PROJECT, request, TABLES)

    assert manager.calls[0][0]["tables"] == ["Order-Items"]


def test_truncationReachesTheBuilder():
    table = pa.table({"state": [f"label{n}" for n in range(8)], "total": list(range(8))})

    failure = _failure(_Manager(_success(table, truncated=True)), _bar(), lenient=False)

    assert failure.stage == "build"
    assert "more rows" in failure.message


def test_lenientIsPassedToTheBuilder():
    table = pa.table({"state": [f"label{n}" for n in range(501)], "total": list(range(501))})

    assert _failure(_Manager(_success(table)), _bar()).stage == "build"
    chart = renderChartRequest(_Manager(_success(table)), PROJECT, _bar(), TABLES, lenient=True)

    assert len(chart["data"]["labels"]) == 500


def test_eachStageMapsToChartFailure():
    timedOut = "Execution timed out after 35 seconds."
    cases = {
        "check": (_Manager(None), _bar("SELECT * FROM read_csv('/etc/passwd')")),
        "bind": (_Manager(_handled("bind", 'Referenced column "nope" not found')), _bar()),
        "run": (_Manager(_handled("run", "Out of Memory Error")), _bar()),
        "timeout": (_Manager(SandboxResult("", timedOut + "\n", -9, None, None, timedOut)), _bar()),
        "build": (_Manager(_success(pa.table({"total": [1, 2]}))), _card()),
    }
    for stage, (manager, request) in cases.items():
        failure = _failure(manager, request)
        assert failure.stage == stage, stage
        assert failure.message, stage
        assert str(failure) == failure.message


def test_runnerAndBuilderMessagesReachTheFailure():
    timedOut = "Execution timed out after 35 seconds."

    assert "nope" in _failure(_Manager(_handled("bind", 'Referenced column "nope" not found')), _bar()).message
    assert _failure(_Manager(SandboxResult("", "", -9, None, None, timedOut)), _bar()).message == timedOut
    assert "add LIMIT 1" in _failure(_Manager(_success(pa.table({"total": [1, 2]}))), _card()).message


def test_runIsNotCalledWhenTheCheckFails():
    manager = _Manager(_success(pa.table({"n": [1]})))

    failure = _failure(manager, _bar("SELECT * FROM secrets"))

    assert failure.stage == "check"
    assert manager.calls == []


def test_busyPropagates():
    busy = SandboxBusyError("no slot")

    with pytest.raises(SandboxBusyError) as raised:
        renderChartRequest(_Manager(busy), PROJECT, _bar(), TABLES)

    assert raised.value is busy


def test_nonZeroExitIsARunFailureWithTheFirstStderrLine():
    manager = _Manager(SandboxResult("", "\nOSError: disk gone\nTraceback ...\n", 1, None))

    failure = _failure(manager, _bar())

    assert (failure.stage, failure.message) == ("run", "OSError: disk gone")


def test_nonZeroExitWithoutStderrStillSaysWhy():
    failure = _failure(_Manager(SandboxResult("", "", 3, None)), _bar())

    assert failure.stage == "run"
    assert "3" in failure.message


def test_otherStopsAreRunFailures():
    stopped = "The run was stopped: the server ran out of memory."

    failure = _failure(_Manager(SandboxResult("", stopped + "\n", -9, None, None, stopped)), _bar())

    assert (failure.stage, failure.message) == ("run", stopped)


@pytest.mark.parametrize("stdout", ["", "not json\n", "[1]\n", '{"stage": "bind"}\n', '{"ok": "yes"}\n'])
def test_missingOrGarbledReplyIsARunFailure(stdout):
    failure = _failure(_Manager(SandboxResult(stdout, "", 0, None)), _bar())

    assert failure.stage == "run"
    assert failure.message == "The query run returned no readable answer."


def test_missingOrGarbledReplyIncludesTheFirstStderrLine():
    failure = _failure(_Manager(SandboxResult("not json\n", "\nOSError: disk gone\nTraceback ...\n", 0, None)), _bar())

    assert failure.stage == "run"
    assert failure.message == "The query run returned no readable answer. OSError: disk gone"


def test_successWithoutAResultFileIsARunFailure():
    failure = _failure(_Manager(SandboxResult('{"ok": true, "truncated": false, "rows": 1}\n', "", 0, None)), _bar())

    assert failure.stage == "run"


def test_resultThatIsNotArrowIsARunFailure():
    failure = _failure(_Manager(SandboxResult('{"ok": true, "truncated": false, "rows": 1}\n', "", 0, b"not arrow")), _bar())

    assert failure.stage == "run"


def test_emptyResultFileIsARunFailure():
    assert _failure(_Manager(_resultOf(b"")), _bar()).stage == "run"


def test_resultWithNoRowsIsAnEmptyChart():
    table = pa.table({"state": pa.array([], pa.string()), "total": pa.array([], pa.int64())})

    assert renderChartRequest(_Manager(_success(table)), PROJECT, _bar(), TABLES)["data"]["labels"] == []


@pytest.mark.parametrize("codec", ["zstd", "lz4"])
def test_compressedResultIsRefusedBeforeItIsInflated(codec, monkeypatch):
    """The child wrote the file. A compressed buffer is inflated to the size it declares, far past the file's cap."""
    zeros = pa.table({"state": pa.array([""] * 400_000), "total": pa.array([0] * 400_000, pa.int64())})
    packed = _arrowStream(zeros, compression=codec)
    assert len(packed) * 20 < zeros.nbytes
    decoded = []
    monkeypatch.setattr(sqlRunner.pa.ipc, "read_record_batch", lambda *args, **kwargs: decoded.append(args))

    failure = _failure(_Manager(_resultOf(packed)), _bar())

    assert (failure.stage, failure.message) == ("run", "The query run wrote a result that could not be read.")
    assert decoded == []


def test_olderMetadataVersionIsRefused():
    """Version 4 can name a codec outside the batch header, where the compression check does not look."""
    old = _arrowStream(pa.table({"state": ["SP"], "total": [1]}), metadata_version=pa.ipc.MetadataVersion.V4)

    assert _failure(_Manager(_resultOf(old)), _bar()).message == "The query run wrote a result that could not be read."


def _denseUnion():
    return pa.UnionArray.from_dense(pa.array([0, 0], pa.int8()), pa.array([0, 0], pa.int32()), [pa.array(["shared"])])


@pytest.mark.parametrize("column", [
    pa.array(["a", "b"]).dictionary_encode(),
    pa.array(["a", "b"], pa.string_view()),
    pa.array([b"a", b"b"], pa.binary_view()),
    pa.array([[1], [1]], pa.list_view(pa.int64())),
    pa.RunEndEncodedArray.from_arrays([2], ["a"]),
    _denseUnion(),
    pa.array([["a"], ["b"]], pa.list_(pa.dictionary(pa.int8(), pa.string()))),
    pa.array([{"inner": "a"}, {"inner": "b"}], pa.struct([("inner", pa.dictionary(pa.int8(), pa.string()))])),
], ids=["dictionary", "stringView", "binaryView", "listView", "runEnds", "union", "dictionaryInList", "dictionaryInStruct"])
def test_resultTypesWhoseRowsShareBytesAreRefusedWithTheFix(column, monkeypatch):
    table = pa.table({"state": column, "total": [1, 2]})
    decoded = []
    monkeypatch.setattr(sqlRunner.pa.ipc, "read_record_batch", lambda *args, **kwargs: decoded.append(args))

    failure = _failure(_Manager(_success(table)), _bar())

    assert failure.stage == "run"
    assert failure.message == "The query returns a column of a type a chart cannot read. Cast ENUM and UNION columns to VARCHAR."
    assert decoded == []


def test_plainAndNestedResultTypesAreRead():
    import datetime
    from decimal import Decimal

    table = pa.table({
        "text": ["a"], "big": pa.array(["b"], pa.large_string()), "raw": [b"\x01"], "flag": [True], "n": [1], "f": [1.5],
        "price": [Decimal("1.50")], "day": [datetime.date(2024, 1, 2)], "at": [datetime.datetime(2024, 1, 2, 3)],
        "clock": [datetime.time(10)], "span": pa.array([(1, 2, 3)], pa.month_day_nano_interval()), "nothing": pa.nulls(1),
        "items": [[1, 2]], "pair": [{"a": 1, "b": ["x"]}], "lookup": pa.array([[("k", 1)]], pa.map_(pa.string(), pa.int64())),
        "fixed": pa.array([[1, 2]], pa.list_(pa.int64(), 2)),
    })

    chart = renderChartRequest(_Manager(_success(table)), PROJECT, _tableChart(), TABLES)

    assert chart["data"][0]["items"] == [1, 2]
    assert chart["data"][0]["pair"] == {"a": 1, "b": ["x"]}
    assert list(chart["data"][0]) == table.column_names


def test_moreRowsThanAskedForIsRefused():
    """The launcher stops at the row limit; a file with more did not come from it."""
    table = pa.table({"total": [1, 2, 3]})

    failure = _failure(_Manager(_success(table)), _card())

    assert (failure.stage, "too large" in failure.message) == ("run", True)


def test_rowsAreCountedAcrossBatches():
    sink = pa.BufferOutputStream()
    batch = pa.record_batch({"total": [1, 2]})
    with pa.ipc.new_stream(sink, batch.schema) as writer:
        writer.write_batch(batch)
        writer.write_batch(batch)

    assert "too large" in _failure(_Manager(_resultOf(sink.getvalue().to_pybytes())), _card()).message


def test_moreValuesThanAChartHoldsIsRefused(monkeypatch):
    """A null or boolean child takes next to no bytes, so the file's size does not bound what the builder would make of it."""
    monkeypatch.setattr(sqlRunner, "MAX_RESULT_VALUES", 50)
    wide = pa.table({"nothing": pa.array([[None] * 60], pa.list_(pa.null()))})
    nested = pa.table({"pair": pa.array([{"deep": [[True] * 30, [False] * 30]}])})
    fits = pa.table({"items": pa.array([[1] * 40])})

    for table in (wide, nested):
        failure = _failure(_Manager(_success(table)), _tableChart())
        assert (failure.stage, "too large" in failure.message) == ("run", True)
    assert renderChartRequest(_Manager(_success(fits)), PROJECT, _tableChart(), TABLES)["data"][0]["items"] == [1] * 40


def test_resultWithOffsetsPastItsBytesIsRefused():
    """Nothing checks a file's offsets when it is opened, so the table is validated before the builder walks it."""
    stream = _arrowStream(pa.table({"state": ["abc", "d"], "total": [1, 2]}))
    offsets = struct.pack("<3i", 0, 3, 4)
    assert stream.count(offsets) == 1
    broken = stream.replace(offsets, struct.pack("<3i", 0, 3, 4000))

    assert _failure(_Manager(_resultOf(broken)), _bar()).message == "The query run wrote a result that could not be read."


def test_theOldFileFormatIsNotRead():
    sink = pa.BufferOutputStream()
    table = pa.table({"state": ["SP"], "total": [1]})
    with pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)

    assert _failure(_Manager(_resultOf(sink.getvalue().to_pybytes())), _bar()).stage == "run"


def test_resultOverTheCapIsARunFailure():
    failure = _failure(_Manager(ResultTooLargeError("Sandbox result is too large: 99 bytes.")), _bar())

    assert failure.stage == "run"
    assert "too large" in failure.message


def test_handledFailureKeepsItsStageAndMessage():
    failure = _failure(_Manager(_handled("bind", 'Column "x" is not in the query result; it returns: a, b')), _bar())

    assert (failure.stage, failure.message) == ("bind", 'Column "x" is not in the query result; it returns: a, b')


def test_runnerErrorExposesStageAndMessage():
    error = sqlRunner.ChartRunError("timeout", "slow")

    assert (error.stage, error.message, str(error)) == ("timeout", "slow", "slow")
