"""The SQL launcher in the real sandbox: what a checked query returns, what the lock-down refuses even without the check,
and that DuckDB runs under the run limits. The `separateUids` tests need a root parent on Linux, as in test_sandbox_storage_reads."""

import io
import json
import os
from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from nubrix.components.chartEngine import ChartFailure, renderChartRequest
from nubrix.components.chartRequest import ChartRequest
from nubrix.components.sqlCheck import checkSql
from test.test_sandbox_storage_reads import PROJECT, _arrowStream, _liveRunUidProcesses, separateUids, sharedFoldersOpen, storage  # noqa: F401
from utils import codeExecutor, initMethods, sandboxRunner
from utils.sandboxLimits import currentRunLimits
from utils.sqlRunner import runChartQuery

MIB = 1024**2
STATES = ["SP", "SP", "SP", "RJ", "RJ", "MG"]


@pytest.fixture(autouse=True)
def projectSemaphoreIsOpen(monkeypatch):
    monkeypatch.setattr(codeExecutor.creditService, "_redis", lambda: MagicMock(zcard=lambda _key: 0))


def _parquet(table):
    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


def _putTable(storage, name, table):
    storage.put(PROJECT, f"{name}.parquet", _parquet(table))


@pytest.fixture
def orders(storage):
    _putTable(storage, "orders", pa.table({"state": STATES, "amount": [10, 20, 30, 40, 50, 60]}))


def _request(sql, tables=("orders",), filters=None, required=(), rowLimit=100):
    return {"sql": sql, "tables": list(tables), "filters": filters, "requiredColumns": list(required), "rowLimit": rowLimit}


def _run(request, timeoutSeconds=60):
    """Run the launcher directly, bypassing `checkSql`. Returns its reply and, on success, the result table."""
    result = sandboxRunner.runSqlSandbox(PROJECT, request, timeoutSeconds)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert len(lines) == 1, result.stdout
    reply = json.loads(lines[0])
    if not reply["ok"]:
        assert result.resultBytes is None
        return reply, None
    return reply, pa.ipc.open_file(pa.BufferReader(result.resultBytes)).read_all()


def _scalar(sql, **kwargs):
    reply, table = _run(_request(sql, **kwargs))
    assert reply["ok"], reply
    return table.column(0)[0].as_py()


def _settings(*names):
    """What DuckDB reports for each setting, from one run."""
    columns = ", ".join(f"CAST(current_setting('{name}') AS VARCHAR) AS {name}" for name in names)
    reply, table = _run(_request(f"SELECT {columns}", tables=[]))
    assert reply["ok"], reply
    return table.to_pylist()[0]


def test_simpleGroupByReturnsRows(orders):
    reply, table = _run(_request("SELECT state, sum(amount) AS total FROM orders GROUP BY state ORDER BY state", required=["state", "total"]))

    assert reply == {"ok": True, "truncated": False, "rows": 3}
    assert table.to_pylist() == [{"state": "MG", "total": 60}, {"state": "RJ", "total": 90}, {"state": "SP", "total": 60}]


def test_resultFileIsAnArrowIpcFile(orders):
    result = sandboxRunner.runSqlSandbox(PROJECT, _request("SELECT state FROM orders"), 60)

    assert result.resultBytes.startswith(b"ARROW1")


def test_quotedAndMixedCaseTableNamesRun(storage):
    _putTable(storage, "order-items", pa.table({"sku": ["a", "b", "c"]}))

    assert _scalar('SELECT count(*) FROM "Order-Items"', tables=["order-items"]) == 3


def test_tableOfAnArrowStreamIsQueryable(storage, monkeypatch):
    monkeypatch.setattr(initMethods, "_read_redis_bytes", lambda _key: _arrowStream(pa.table({"amount": [7, 8, 9]})))

    assert _scalar("SELECT sum(amount) FROM orders") == 24


def test_aTableTheProjectDoesNotHaveIsARunFailure(orders):
    reply, _ = _run(_request("SELECT * FROM missing", tables=["missing"]))

    assert reply["ok"] is False
    assert reply["stage"] == "run"
    assert "missing" in reply["message"]


@pytest.fixture
def somethingToRead(tmp_path):
    """A path the lock-down must keep the query away from: a real file on a development machine, a system file in the image."""
    if os.name == "posix":
        return "/etc/passwd"
    target = tmp_path / "secret.csv"
    target.write_text("a,b\n1,2\n")
    return target.as_posix()


READ_ATTEMPTS = (
    "SELECT * FROM read_csv('{path}')",
    "SELECT * FROM read_parquet('{path}')",
    "SELECT * FROM read_text('{path}')",
    "SELECT * FROM read_blob('{path}')",
    "SELECT * FROM glob('{path}')",
    "SELECT * FROM sniff_csv('{path}')",
)
WRITE_ATTEMPTS = (
    "ATTACH '{out}.db' AS other",
    "COPY (SELECT 1) TO '{out}.csv'",
    "SELECT 1; COPY (SELECT 1) TO '{out}.csv'",
    "INSTALL httpfs",
    "SET enable_external_access=true",
    "SET lock_configuration=false",
    "SET disabled_filesystems=''",
    "PRAGMA enable_external_access=true",
)


@pytest.mark.parametrize("template", READ_ATTEMPTS)
def test_lockDownRefusesFileReadsEvenWithoutTheCheck(orders, somethingToRead, template):
    reply, table = _run(_request(template.format(path=somethingToRead)))

    assert reply["ok"] is False
    assert table is None
    assert "disabled by configuration" in reply["message"]


@pytest.mark.parametrize("template", WRITE_ATTEMPTS)
def test_lockDownRefusesWritesAndSettingChangesEvenWithoutTheCheck(orders, tmp_path, template):
    out = (tmp_path / "out").as_posix()

    reply, _ = _run(_request(template.format(out=out)))

    assert reply["ok"] is False
    assert reply["stage"] == "bind"
    assert not [name for name in os.listdir(tmp_path) if name.startswith("out")]


def test_configureLocksExternalAccessAndConfiguration(tmp_path):
    """Unit: after `_configure`, unlocking or turning external access back on must raise (fails if `_configure` is skipped)."""
    import duckdb

    from utils.sql_launcher import _configure

    con = duckdb.connect(":memory:")
    _configure(con, currentRunLimits(threads=0, maxRunMemory=0, maxDiskBytes=0), str(tmp_path / "duckdb-tmp"))

    with pytest.raises(duckdb.Error):
        con.execute("SET enable_external_access=true")
    with pytest.raises(duckdb.Error):
        con.execute("SET lock_configuration=false")


def test_externalAccessStaysOffAndTheConfigurationLocked(storage):
    assert _settings("enable_external_access", "lock_configuration") == {"enable_external_access": "false", "lock_configuration": "true"}


def test_filtersReachTheQuery(orders):
    count = "SELECT count(*) FROM orders"

    assert _scalar(count) == 6
    assert _scalar(count, filters=[{"state": "SP"}]) == 3
    assert _scalar(count, filters=[{"orders.state": ["RJ", "MG"]}, {"amount": {"min": 50}}]) == 2
    assert _scalar("SELECT sum(amount) FROM orders", filters=[{"state": "SP"}]) == 60


def test_aFilterThatMatchesNothingGivesAnEmptyResult(orders):
    reply, table = _run(_request("SELECT state FROM orders", filters=[{"state": "ZZ"}]))

    assert reply == {"ok": True, "truncated": False, "rows": 0}
    assert table.num_rows == 0
    assert table.schema.names == ["state"]


def test_bindErrorNamesTheColumn(orders):
    reply, table = _run(_request("SELECT nope FROM orders"))

    assert reply["ok"] is False
    assert reply["stage"] == "bind"
    assert "nope" in reply["message"]
    assert table is None


def test_requiredColumnMissingIsABindError(orders):
    reply, _ = _run(_request("SELECT state, amount FROM orders", required=["state", "total"]))

    assert reply == {"ok": False, "stage": "bind", "message": 'Column "total" is not in the query result; it returns: state, amount'}


def test_requiredColumnsThatArePresentPass(orders):
    reply, _ = _run(_request("SELECT state, amount FROM orders", required=["amount", "state"]))

    assert reply["ok"] is True


def test_moreThanOneStatementIsRefused(orders):
    reply, _ = _run(_request("SELECT 1; SELECT 2"))

    assert reply["ok"] is False
    assert reply["stage"] == "bind"
    assert reply["message"] == "Write exactly one SELECT statement."


def test_selectThenCopyIsRefusedAtBind(orders, tmp_path):
    out = (tmp_path / "out.csv").as_posix()

    reply, _ = _run(_request(f"SELECT 1; COPY (SELECT 1) TO '{out}'"))

    assert reply == {"ok": False, "stage": "bind", "message": "Write exactly one SELECT statement."}
    assert not os.path.exists(out)


def test_dynamicPivotRuns(storage):
    _putTable(storage, "sales", pa.table({"c": ["a", "b", "a"], "v": [1, 2, 3]}))

    reply, table = _run(_request("SELECT * FROM (PIVOT sales ON c USING sum(v))", tables=["sales"]))

    assert reply["ok"] is True
    assert reply["truncated"] is False
    assert table.num_rows == 1
    assert table.to_pylist() == [{"a": 4, "b": 2}]


def test_trailingSemicolonAndCommentRun(orders):
    assert _scalar("SELECT count(*) FROM orders; -- done") == 6
    assert _scalar("SELECT count(*) FROM orders -- done") == 6


def test_aFailureWhileRunningIsARunFailure(orders):
    reply, _ = _run(_request("SELECT CAST(state AS INTEGER) AS n FROM orders"))

    assert reply["ok"] is False
    assert reply["stage"] == "run"
    assert reply["message"]


def test_truncationFlag(storage):
    _putTable(storage, "orders", pa.table({"n": list(range(25))}))

    reply, table = _run(_request("SELECT n FROM orders ORDER BY n", rowLimit=10))

    assert reply == {"ok": True, "truncated": True, "rows": 10}
    assert table.column("n").to_pylist() == list(range(10))


def test_aResultOfExactlyTheLimitIsNotTruncated(storage):
    _putTable(storage, "orders", pa.table({"n": list(range(10))}))

    reply, table = _run(_request("SELECT n FROM orders", rowLimit=10))

    assert reply == {"ok": True, "truncated": False, "rows": 10}
    assert table.num_rows == 10


def test_resultOverTheCapRaisesResultTooLarge(storage, monkeypatch):
    _putTable(storage, "orders", pa.table({"n": list(range(50000))}))
    monkeypatch.setattr(sandboxRunner, "SQL_RESULT_CAP_BYTES", 4096)

    with pytest.raises(sandboxRunner.ResultTooLargeError):
        sandboxRunner.runSqlSandbox(PROJECT, _request("SELECT n FROM orders", rowLimit=50000), 60)


def test_millionRowJoinFinishes(storage):
    rows = range(1_000_000)
    _putTable(storage, "big", pa.table({"k": pa.array([i % 1000 for i in rows], pa.int32()), "v": pa.array([float(i % 7) for i in rows])}))
    _putTable(storage, "small", pa.table({"k": pa.array(range(1000), pa.int32()), "w": pa.array([float(i % 5) for i in range(1000)])}))
    sql = "SELECT s.k % 10 AS g, count(*) AS n, sum(b.v * s.w) AS total FROM big b JOIN small s ON b.k = s.k GROUP BY 1 ORDER BY 1"

    reply, table = _run(_request(sql, tables=["big", "small"]), timeoutSeconds=35)

    assert reply == {"ok": True, "truncated": False, "rows": 10}
    assert sum(table.column("n").to_pylist()) == 1_000_000


GROUP_BY_BIGGER_THAN_THE_MEMORY_LIMIT = (
    "SELECT count(*) AS n, sum(c) AS s FROM "
    "(SELECT k, count(*) AS c, sum(k * 3) AS t FROM (SELECT range AS k FROM range(20000000)) GROUP BY k)"
)


def test_spillsUnderTheRunMemoryLimit(storage, monkeypatch):
    # Test-only limits: DuckDB gets half the memory limit, which the 20M-group hash table is well over.
    monkeypatch.setenv("SANDBOX_MAX_RUN_MEMORY_BYTES", str(1024 * MIB))
    monkeypatch.setenv("SANDBOX_MAX_DISK_BYTES", str(8 * 1024 * MIB))

    reply, table = _run(_request(GROUP_BY_BIGGER_THAN_THE_MEMORY_LIMIT, tables=[]), timeoutSeconds=60)

    assert reply["ok"], reply
    assert table.to_pylist() == [{"n": 20_000_000, "s": 20_000_000}]
    assert _settings("temp_directory")["temp_directory"].replace("\\", "/").endswith("/duckdb-tmp")


def test_spillUsesTheTempFolderAndItsDiskLimit(storage, monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_RUN_MEMORY_BYTES", str(1024 * MIB))
    monkeypatch.setenv("SANDBOX_MAX_DISK_BYTES", str(16 * MIB))

    reply, _ = _run(_request(GROUP_BY_BIGGER_THAN_THE_MEMORY_LIMIT, tables=[]), timeoutSeconds=60)

    assert reply["ok"] is False
    assert reply["stage"] == "run"
    assert "max_temp_directory_size" in reply["message"]


def test_limitsSizeTheDuckDbSettings(storage, monkeypatch):
    monkeypatch.setenv("SANDBOX_RUN_THREADS", "2")
    monkeypatch.setenv("SANDBOX_MAX_RUN_MEMORY_BYTES", str(512 * MIB))
    monkeypatch.setenv("SANDBOX_MAX_DISK_BYTES", str(256 * MIB))

    assert _settings("threads", "memory_limit", "max_temp_directory_size", "preserve_insertion_order", "autoinstall_known_extensions", "autoload_known_extensions") == {
        "threads": "2",
        "memory_limit": "256.0 MiB",
        "max_temp_directory_size": "128.0 MiB",
        "preserve_insertion_order": "false",
        "autoinstall_known_extensions": "false",
        "autoload_known_extensions": "false",
    }


def test_aZeroLimitLeavesTheDuckDbSettingAlone(storage, monkeypatch):
    monkeypatch.setenv("SANDBOX_RUN_THREADS", "0")
    monkeypatch.setenv("SANDBOX_MAX_RUN_MEMORY_BYTES", "0")
    monkeypatch.setenv("SANDBOX_MAX_DISK_BYTES", "0")

    settings = _settings("threads", "memory_limit", "max_temp_directory_size")

    assert int(settings["threads"]) > 0
    assert not settings["memory_limit"].startswith("0")
    assert not settings["max_temp_directory_size"].startswith("0")


def test_timeoutIsReported(storage, monkeypatch):
    _putTable(storage, "orders", pa.table({"v": pa.array(range(20000), pa.int64())}))
    manager = codeExecutor.REPLManager(timeoutSeconds=2)
    request = ChartRequest(chartType="card", title="Heavy", sql="SELECT sum(a.v * b.v * c.v) AS total FROM orders a, orders b, orders c", encoding={"value": "total"})

    with pytest.raises(ChartFailure) as raised:
        renderChartRequest(manager, PROJECT, request, ["orders"])

    assert raised.value.stage == "timeout"
    assert "timed out" in raised.value.message
    if os.name == "posix" and os.geteuid() == 0:
        assert _liveRunUidProcesses() == []


def test_engineRendersARealChart(orders):
    manager = codeExecutor.REPLManager(timeoutSeconds=60)
    request = ChartRequest(
        chartType="bar", title="Sales by state", sql="SELECT state, sum(amount) AS total FROM Orders GROUP BY state ORDER BY state", encoding={"x": "state", "y": ["total"]},
    )

    chart = renderChartRequest(manager, PROJECT, request, ["orders"], filters=[{"state": ["SP", "RJ"]}])

    assert chart["data"]["labels"] == ["RJ", "SP"]
    assert chart["data"]["datasets"][0]["data"] == [90, 60]


def test_engineReportsAMissingColumnAsABindFailure(orders):
    manager = codeExecutor.REPLManager(timeoutSeconds=60)
    request = ChartRequest(chartType="card", title="Total", sql="SELECT sum(amount) AS total FROM orders", encoding={"value": "sum"})

    with pytest.raises(ChartFailure) as raised:
        renderChartRequest(manager, PROJECT, request, ["orders"])

    assert raised.value.stage == "bind"
    assert raised.value.message == 'Column "sum" is not in the query result; it returns: total'


def test_runChartQueryReturnsTheTableAndTheTruncationFlag(storage):
    _putTable(storage, "orders", pa.table({"n": list(range(5001))}))
    manager = codeExecutor.REPLManager(timeoutSeconds=60)
    request = ChartRequest(chartType="bar", title="Counts", sql="SELECT n, n AS m FROM orders", encoding={"x": "n", "y": ["m"]})

    result = runChartQuery(manager, PROJECT, checkSql(request.sql, ["orders"]), request, None)

    assert result.truncated is True
    assert result.table.num_rows == 5000


@separateUids
def test_underSeccomp(orders):
    assert currentRunLimits().blockNetwork is True

    reply, table = _run(_request("SELECT state, sum(amount) AS total FROM orders GROUP BY state ORDER BY state"))

    assert reply == {"ok": True, "truncated": False, "rows": 3}
    assert table.column("total").to_pylist() == [60, 90, 60]
    assert _liveRunUidProcesses() == []


@separateUids
def test_duckdbThreadPoolFitsUnderATightProcessLimit(orders, monkeypatch):
    """Connect-time threads must be capped: a CPU-sized pool would exceed NPROC; the run's capped pool must fit."""
    threads = 2
    cpus = os.cpu_count() or 1
    # Room for the process and the other capped pools (Arrow etc.); still below a CPU-sized DuckDB pool.
    # Measured on the image: capped needs threads+9; plain connect peaks near `cpus` workers before SET threads.
    overhead = 9
    maxProcesses = threads + overhead
    if cpus <= maxProcesses:
        pytest.skip("os.cpu_count() is too small to tell a CPU-sized DuckDB pool from the capped one")
    monkeypatch.setenv("SANDBOX_RUN_THREADS", str(threads))
    monkeypatch.setenv("SANDBOX_MAX_RUN_PROCESSES", str(maxProcesses))

    reply, table = _run(_request("SELECT state, sum(amount) AS total FROM orders GROUP BY state ORDER BY state"))

    assert reply == {"ok": True, "truncated": False, "rows": 3}
    assert table.column("total").to_pylist() == [60, 90, 60]
    assert _liveRunUidProcesses() == []
