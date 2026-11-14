"""
sql_launcher.py — subprocess entry point for one checked SQL chart query.

Reads JSON from stdin: {"projectId": ..., "code": "", "result_path": ..., "request": {...}, "channel": ...} where
request is {"sql": str, "tables": [str], "filters": list | null, "requiredColumns": [str], "rowLimit": int}.
The API checked the query before it got here, but nothing below relies on that: the query runs in a DuckDB with
every table already registered and file access locked off.

Prints exactly one JSON line and exits 0:
  {"ok": true, "truncated": bool, "rows": int} after writing the result to result_path as an Arrow IPC file, or
  {"ok": false, "stage": "bind" | "run", "message": str} for a failure the model can repair.
Anything else (a bad input, a crash) writes to stderr and exits 1.
"""

import json
import os
import sys
import traceback

import duckdb
import pyarrow as pa
import pyarrow.dataset as ds

from utils.runFilters import applyFilters, filterDataset
from utils.sandboxLimits import RunLimits
from utils.sandboxRunner import enterSandbox, tablePath

# DuckDB gets half of what a run may use, which leaves the rest for Arrow, Python and the result.
_MEMORY_PERCENT = 50
_TEMP_DISK_PERCENT = 50
_MAX_MESSAGE_CHARS = 2000


def _reply(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _failure(stage: str, message: str) -> None:
    _reply({"ok": False, "stage": stage, "message": message[:_MAX_MESSAGE_CHARS]})


def _registered(tableName: str, filters: list | None):
    """The table as something DuckDB can scan: a Parquet file stays a dataset, a small Arrow stream is mapped."""
    path = tablePath(tableName)
    if path.endswith(".arrow"):
        return applyFilters(pa.ipc.open_stream(pa.memory_map(path)).read_all(), tableName, filters)
    return filterDataset(ds.dataset(path, format="parquet"), tableName, filters)


def _quoted(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _configure(con: duckdb.DuckDBPyConnection, limits: RunLimits, tempFolder: str) -> None:
    """Size DuckDB from the run's limits (a limit of 0 means none), then lock the connection.

    The order matters: external access goes off before the configuration locks. `disabled_filesystems` is left
    alone on purpose, as it would also block the spill files.
    """
    if limits.threads > 0:
        con.execute(f"SET threads={limits.threads}")
    if limits.maxRunMemory > 0:
        con.execute(f"SET memory_limit='{limits.maxRunMemory * _MEMORY_PERCENT // 100}B'")
    con.execute(f"SET temp_directory={_quoted(tempFolder)}")
    if limits.maxDiskBytes > 0:
        con.execute(f"SET max_temp_directory_size='{limits.maxDiskBytes * _TEMP_DISK_PERCENT // 100}B'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET autoinstall_known_extensions=false")
    con.execute("SET autoload_known_extensions=false")
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")


def _selectStatements(con: duckdb.DuckDBPyConnection, sql: str) -> list | str:
    """The CREATE* + final SELECT DuckDB split the text into, or a bind-failure message.

    A connection runs every statement it is given, so anything except leading CREATEs (DuckDB's own
    dynamic-PIVOT expansion) followed by one SELECT is refused before any of it runs.
    """
    statements = list(con.extract_statements(sql))
    if (
        not statements
        or statements[-1].type != duckdb.StatementType.SELECT
        or any(statement.type != duckdb.StatementType.CREATE for statement in statements[:-1])
    ):
        return "Write exactly one SELECT statement."
    return statements


def _bindFailure(con: duckdb.DuckDBPyConnection, statements: list, requiredColumns: list[str]) -> str | None:
    """Run leading CREATEs once, then bind the final SELECT without fetching rows. Returns why it can't be run, or None."""
    try:
        for statement in statements[:-1]:
            con.execute(statement)
        select = statements[-1]
        if select.query:
            columns = [row[0] for row in con.execute("DESCRIBE " + select.query).fetchall()]
        else:
            # Dynamic PIVOT's rewritten SELECT has no query text; columns come from the bound relation.
            columns = list(con.sql(select).columns)
    except duckdb.Error as error:
        return str(error)
    for name in requiredColumns:
        if name not in columns:
            return f'Column "{name}" is not in the query result; it returns: {", ".join(columns)}'
    return None


def _runQuery(con: duckdb.DuckDBPyConnection, select, rowLimit: int) -> tuple[pa.Table, bool]:
    """Run the final SELECT for at most rowLimit + 1 rows; the extra row only says there were more."""
    table = con.sql(select).limit(rowLimit + 1).fetch_arrow_table()
    if table.num_rows > rowLimit:
        return table.slice(0, rowLimit), True
    return table, False


def _writeResult(table: pa.Table, path: str) -> None:
    with pa.OSFile(path, "wb") as sink:
        with pa.ipc.new_file(sink, table.schema) as writer:
            writer.write_table(table)


def main() -> None:
    try:
        inputData = json.load(sys.stdin)
        request = inputData["request"]
        resultPath = inputData["result_path"]
        channel = inputData["channel"]
        sql = request["sql"]
        tables = request["tables"]
        filters = request.get("filters")
        requiredColumns = request["requiredColumns"]
        rowLimit = int(request["rowLimit"])
    except Exception as error:
        sys.stderr.write(f"Failed to read/parse input JSON from stdin: {error}\n")
        sys.exit(1)

    enterSandbox(channel)
    limits = RunLimits.fromChannel(channel["limits"])
    # Size the pool at connect time: SET threads afterwards does not shrink a CPU-sized pool, and each
    # worker counts against RLIMIT_NPROC. Omit config when threads is 0 (DuckDB rejects config=None).
    con = (
        duckdb.connect(":memory:", config={"threads": limits.threads})
        if limits.threads > 0
        else duckdb.connect(":memory:")
    )
    try:
        for name in tables:
            # The exact name the project gives it, so DuckDB's own case rules decide what the query's text matches.
            con.register(name, _registered(name, filters))
    except RuntimeError as error:
        _failure("run", str(error))
        return
    _configure(con, limits, os.path.join(os.path.dirname(resultPath), "duckdb-tmp"))

    statements = _selectStatements(con, sql)
    if isinstance(statements, str):
        _failure("bind", statements)
        return
    problem = _bindFailure(con, statements, requiredColumns)
    if problem is not None:
        _failure("bind", problem)
        return
    try:
        table, truncated = _runQuery(con, statements[-1], rowLimit)
    except (duckdb.Error, pa.ArrowException) as error:
        _failure("run", str(error))
        return
    _writeResult(table, resultPath)
    _reply({"ok": True, "truncated": truncated, "rows": table.num_rows})


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as error:
        # The first line is the one the API reports, so it says what failed; the traceback follows for the logs.
        sys.stderr.write(f"{type(error).__name__}: {error}\n")
        traceback.print_exc()
        sys.exit(1)
