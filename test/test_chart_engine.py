"""The chart engine ties the SQL check, the sandbox run and the chart builder together (the sandbox is stubbed)."""

import json

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


def _arrowFile(table):
    sink = pa.BufferOutputStream()
    with pa.ipc.new_file(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _success(table, truncated=False):
    stdout = json.dumps({"ok": True, "truncated": truncated, "rows": table.num_rows}) + "\n"
    return SandboxResult(stdout, "", 0, _arrowFile(table))


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

    chart = renderChartRequest(_Manager(_success(table, truncated=True)), PROJECT, _bar(), TABLES, lenient=True)

    assert len(chart["data"]["labels"]) == 8


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


def test_successWithoutAResultFileIsARunFailure():
    failure = _failure(_Manager(SandboxResult('{"ok": true, "truncated": false, "rows": 1}\n', "", 0, None)), _bar())

    assert failure.stage == "run"


def test_resultThatIsNotArrowIsARunFailure():
    failure = _failure(_Manager(SandboxResult('{"ok": true, "truncated": false, "rows": 1}\n', "", 0, b"not arrow")), _bar())

    assert failure.stage == "run"


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
