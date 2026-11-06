"""Stored-table marker, bounded footer read and the API's writer."""

import io
import json
import os

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from utils import sandboxRunner, storedTables
from utils.sandboxSlots import SandboxBusyError
from utils.storedTables import (
    MARKER_KEY,
    TableTooLargeError,
    encodedUncompressedBytes,
    markerIsValid,
    readFooter,
    tableMarker,
    writeStoredTable,
)


@pytest.fixture(autouse=True)
def _cleanEnv(monkeypatch):
    monkeypatch.setenv("SUPABASE_KEY", "unit-test-key")
    monkeypatch.delenv("STORED_TABLE_NORMALISE", raising=False)
    monkeypatch.delenv("STORED_TABLE_MAX_DECODED_BYTES", raising=False)
    monkeypatch.delenv("TABLE_READER_MEMORY_BYTES", raising=False)
    storedTables._missingKeyWarned = False


def _frame():
    return pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", None]})


def _footer(data):
    metadata = readFooter(data)
    assert metadata is not None
    return metadata


def test_writtenTableCarriesAValidMarker():
    data = writeStoredTable(_frame(), "p1", "orders")
    assert markerIsValid(_footer(data), "p1", "orders")
    assert pd.read_parquet(io.BytesIO(data)).equals(_frame())


@pytest.mark.parametrize(
    "project, table",
    [("p1", "other"), ("p2", "orders"), ("p1", "a-b_c"), ("p1", "../x/y.z/..")],
)
def test_markerFromAnotherTableIsInvalid(project, table):
    data = writeStoredTable(_frame(), "p1", "orders")
    assert markerIsValid(_footer(data), project, table) is False


def test_oddNamesRoundTripAndNeverRaise():
    for name in ("a-b_c", "a.b/c\\d..", "", "ünï", "x" * 5000):
        data = writeStoredTable(_frame(), "p1", name)
        assert markerIsValid(_footer(data), "p1", name)
        assert not markerIsValid(_footer(data), "p1", name + "x")


def test_copiedMarkerKeyIsDroppedOnWrite():
    table = pa.Table.from_pandas(_frame(), preserve_index=False)
    forged = table.replace_schema_metadata({MARKER_KEY: tableMarker("p1", "other")})
    data = writeStoredTable(forged, "p1", "orders")
    metadata = _footer(data)
    assert metadata.metadata[MARKER_KEY] == tableMarker("p1", "orders")
    assert not markerIsValid(metadata, "p1", "other")


def test_inputMetadataIsNotCopied():
    table = pa.Table.from_pandas(_frame(), preserve_index=False)
    table = table.replace_schema_metadata({b"pandas": b"{bad", b"extra": b"1"})
    metadata = _footer(writeStoredTable(table, "p1", "orders"))
    assert b"extra" not in metadata.metadata
    assert b"pandas" not in metadata.metadata


def test_honestDataFrameRoundTripsLikeBefore():
    df = pd.DataFrame(
        {
            "cat": pd.Categorical(["a", "b", "a"]),
            "tz": pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03"]).tz_localize("Asia/Kolkata"),
            "naive": pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03"]),
            "s": ["x", "y", None],
            "nullable": pd.array([1, None, 3], dtype="Int64"),
            "f": [1.5, np.nan, 2.0],
        }
    )
    back = pd.read_parquet(io.BytesIO(writeStoredTable(df, "p1", "t")))
    pd.testing.assert_frame_equal(back, df)


def test_footerOverOneMiBIsNotParsedInProcess():
    wide = pa.table({f"c{i}": pa.array([1], pa.int8()) for i in range(20_000)})
    sink = io.BytesIO()
    pq.write_table(wide, sink)
    data = sink.getvalue()
    footerLength = int.from_bytes(data[-8:-4], "little")
    assert footerLength > 1 << 20  # the file really has a big footer
    assert readFooter(data) is None


def test_doctoredLengthBytesGiveNone():
    data = writeStoredTable(_frame(), "p1", "orders")
    for bad in (len(data), 1 << 31, (1 << 32) - 1, 0):
        doctored = bytearray(data)
        doctored[-8:-4] = bad.to_bytes(4, "little")
        assert readFooter(bytes(doctored)) is None


def test_notParquetGivesNone():
    junk = (b"", b"PAR1", b"PAR1PAR1", b"hello world, not a table", b"PAR1" + b"\0" * 20 + b"PAR1", b"\xff" * 100)
    for item in junk:
        assert readFooter(item) is None


def test_truncatedAndBadMagicGiveNone():
    data = writeStoredTable(_frame(), "p1", "orders")
    assert readFooter(data[:-1]) is None
    assert readFooter(data[: len(data) // 2]) is None
    assert readFooter(b"XXXX" + data[4:]) is None
    assert readFooter(data[:-4] + b"XXXX") is None


def test_markerIsValidNeverRaisesOnOddMetadata():
    plain = io.BytesIO()
    pq.write_table(pa.table({"a": [1]}), plain)
    metadata = _footer(plain.getvalue())  # no marker key at all
    assert markerIsValid(metadata, "p", "t") is False
    assert markerIsValid(None, "p", "t") is False  # type: ignore[arg-type]


def test_writeRefusesATableOverTheLimit(monkeypatch):
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", "1024")
    big = pd.DataFrame({"a": np.arange(10_000, dtype="int64")})
    with pytest.raises(TableTooLargeError):
        writeStoredTable(big, "p1", "orders")


def test_pathWriterMatchesTheBytesWriter(tmp_path):
    from utils.storedTables import writeStoredTableToPath

    path = tmp_path / "orders.parquet"
    writeStoredTableToPath(_frame(), "p1", "orders", str(path))
    data = path.read_bytes()

    assert markerIsValid(_footer(data), "p1", "orders")
    assert data == writeStoredTable(_frame(), "p1", "orders")
    assert _footer(data).row_group(0).column(0).compression == "ZSTD"


def test_pathWriterRefusesATableOverTheLimitAndWritesNothing(monkeypatch, tmp_path):
    from utils.storedTables import writeStoredTableToPath

    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", "1024")
    path = tmp_path / "orders.parquet"
    with pytest.raises(TableTooLargeError):
        writeStoredTableToPath(pd.DataFrame({"a": np.arange(10_000, dtype="int64")}), "p1", "orders", str(path))
    assert not path.exists()


def test_writerRefusalHasItsOwnCopy(monkeypatch):
    from utils.storedTables import tableReadException, tableWriteException

    refusal = TableTooLargeError("Table is 99 bytes decoded, over the limit of 1.")
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", "1073741824")
    mapped = tableWriteException(refusal)

    assert mapped is not None
    assert (mapped.statusCode, mapped.errorCode) == (413, "TABLE_TOO_LARGE")
    assert mapped.uiMessage == "This table is too large to load (over 1 GiB)."
    assert tableWriteException(ValueError("other")) is None
    assert tableReadException(refusal).uiMessage != mapped.uiMessage


def test_zeroLimitMeansNoLimit(monkeypatch):
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", "0")
    assert storedTables.maxDecodedBytes() == 0
    writeStoredTable(pd.DataFrame({"a": np.arange(10_000)}), "p1", "orders")


def test_badLimitValuesFallBackToDefaults(monkeypatch):
    for raw in ("abc", "-5", ""):
        monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", raw)
        assert storedTables.maxDecodedBytes() == 2147483648
    monkeypatch.delenv("STORED_TABLE_MAX_DECODED_BYTES")
    assert storedTables.maxDecodedBytes() == 2147483648
    monkeypatch.setenv("STORED_TABLE_NORMALISE", "yes")
    assert storedTables.normaliseEnabled() is True


def test_writerUsesSmallPagesAndRowGroups(monkeypatch):
    seen = {}
    real = pq.write_table

    def spy(table, where, **kwargs):
        seen.update(kwargs)
        return real(table, where, **kwargs)

    monkeypatch.setattr(storedTables.pq, "write_table", spy)
    n = 3_000_000
    values = np.random.default_rng(0).integers(0, 1 << 62, n)
    data = writeStoredTable(pd.DataFrame({"a": values}), "p1", "t")
    assert seen["compression"] == "zstd"
    assert seen["data_page_size"] == 1 << 20
    assert seen["row_group_size"] == 1_048_576
    metadata = _footer(data)
    assert metadata.num_rows == n
    assert metadata.num_row_groups == 3
    column = metadata.row_group(0).column(0)
    assert column.compression == "ZSTD"
    # Random int64 values do not dictionary-encode, so a row group holds about
    # 8 MiB of plain values: with 1 MiB pages that is several pages, not one.
    assert 7 * (1 << 20) < column.total_uncompressed_size < 9 * (1 << 20)


def test_encodedUncompressedBytesSumsEveryChunkOfEveryRowGroup():
    df = pd.DataFrame({"a": np.arange(2000, dtype="int64"), "b": np.arange(2000, dtype="int64")})
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df), sink, row_group_size=500)
    metadata = _footer(sink.getvalue())
    expected = sum(
        metadata.row_group(g).column(c).total_uncompressed_size
        for g in range(metadata.num_row_groups)
        for c in range(metadata.num_columns)
    )
    assert metadata.num_row_groups == 4
    assert encodedUncompressedBytes(metadata) == expected > 0


def test_normaliseOffMakesEveryMarkerInvalid(monkeypatch):
    data = writeStoredTable(_frame(), "p1", "orders")
    assert markerIsValid(_footer(data), "p1", "orders")
    monkeypatch.setenv("STORED_TABLE_NORMALISE", "0")
    assert storedTables.normaliseEnabled() is False
    assert markerIsValid(_footer(data), "p1", "orders") is False


class _Warnings:
    def __init__(self):
        self.messages = []

    def warning(self, message, *args, **kwargs):
        self.messages.append(str(message))


@pytest.fixture
def warningLog(monkeypatch):
    recorder = _Warnings()
    monkeypatch.setattr(storedTables, "logger", recorder)
    return recorder


def test_missingKeyMarksNothingAndNeverRaises(monkeypatch, warningLog):
    valid = _footer(writeStoredTable(_frame(), "p1", "orders"))
    for setter in (lambda: monkeypatch.delenv("SUPABASE_KEY"), lambda: monkeypatch.setenv("SUPABASE_KEY", "")):
        setter()
        storedTables._missingKeyWarned = False
        assert tableMarker("p1", "orders") == b""
        # a marker written under a real key is not trusted without a key
        assert markerIsValid(valid, "p1", "orders") is False
        forged = pa.Table.from_pandas(_frame(), preserve_index=False).replace_schema_metadata(
            {MARKER_KEY: b"anything"}
        )
        keyless = writeStoredTable(forged, "p1", "orders")
        metadata = _footer(keyless)
        assert MARKER_KEY not in (metadata.metadata or {})
        assert markerIsValid(metadata, "p1", "orders") is False
        assert pd.read_parquet(io.BytesIO(keyless)).equals(_frame())


def test_missingKeyWarnsOnce(monkeypatch, warningLog):
    monkeypatch.delenv("SUPABASE_KEY")
    storedTables._missingKeyWarned = False
    for _ in range(5):
        tableMarker("p", "t")
    assert len([m for m in warningLog.messages if "SUPABASE_KEY" in m]) == 1


TRUSTED_ECHO = "trusted_echo_launcher.py"
separateUids = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() != 0,
    reason="only a root parent on Linux can run a child as another uid",
)


@pytest.fixture
def trustedEcho(monkeypatch):
    """The test launcher is allowed for one test only: production code has no way to name it."""
    assert TRUSTED_ECHO not in sandboxRunner._TRUSTED_LAUNCHERS
    monkeypatch.setitem(sandboxRunner._TRUSTED_LAUNCHERS, TRUSTED_ECHO, "test/trusted_echo_launcher.py")


def test_trustedRunReadsItsInputAndReturnsItsResult(trustedEcho):
    data = bytes(range(256)) * 40
    result = sandboxRunner.runTrustedSandbox(TRUSTED_ECHO, "p1", data, {"mode": "echo"}, 120, 1 << 20)
    assert result.returncode == 0, result.stderr
    assert result.resultBytes == data
    assert result.previewBytes is None
    assert result.stopped is None


@separateUids
def test_trustedRunCannotWriteOrReplaceItsInput(trustedEcho):
    result = sandboxRunner.runTrustedSandbox(TRUSTED_ECHO, "p1", b"abc", {"mode": "probe", "marker": "m-1"}, 120, 1 << 20)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.resultBytes)
    assert sandboxRunner._FIRST_RUN_UID <= report["uid"] < sandboxRunner._FIRST_RUN_UID + sandboxRunner._RUN_UID_COUNT
    assert report["inputUid"] == 0
    assert report["inputGid"] == report["uid"]
    assert report["inputMode"] == "0o640"
    assert report["writable"] is False
    for attempt in ("append", "replace", "unlink", "createBeside"):
        assert report[attempt] == "PermissionError", attempt
    # The same lock-down as a code run: process limit and the seccomp filter.
    assert report["nproc"] == sandboxRunner.currentRunLimits().maxProcesses
    assert report["network"] == "PermissionError"


def test_trustedRunHoldsNoSecretAndNoTableAccess(trustedEcho, monkeypatch):
    monkeypatch.setenv("SUPABASE_KEY", "unit-test-key")
    result = sandboxRunner.runTrustedSandbox(TRUSTED_ECHO, "p1", b"abc", {"mode": "probe", "marker": "m-1"}, 120, 1 << 20)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.resultBytes)
    assert report["request"] == {"mode": "probe", "marker": "m-1"}
    assert {"input_path", "request", "result_path", "channel"} <= set(report["payloadKeys"])
    assert "unit-test-key" not in report["seen"]
    assert "This run has no table access." in report["tableRead"]
    assert report["tables"] == ["input"]


def test_trustedRunRefusesALauncherOutsideTheAllowList():
    for name in ("sandbox_launcher.py", "../test/trusted_echo_launcher.py", TRUSTED_ECHO, ""):
        with pytest.raises(ValueError):
            sandboxRunner.runTrustedSandbox(name, "p1", b"abc", {}, 5, 1024)


def test_trustedRunRaisesBusyWhenNoSlotIsFree(trustedEcho, monkeypatch):
    def busy(*_args):
        raise SandboxBusyError()

    monkeypatch.setattr(sandboxRunner, "acquireRunSlot", busy)
    with pytest.raises(SandboxBusyError):
        sandboxRunner.runTrustedSandbox(TRUSTED_ECHO, "p1", b"abc", {"mode": "echo"}, 5, 1024)


def test_trustedRunTimesOutLikeAnyRun(trustedEcho):
    result = sandboxRunner.runTrustedSandbox(TRUSTED_ECHO, "p1", b"abc", {"mode": "hang"}, 2, 1024)
    assert result.stopped == "Execution timed out after 2 seconds."
    assert result.resultBytes is None


def test_trustedResultOverTheCapIsRefused(trustedEcho):
    with pytest.raises(sandboxRunner.ResultTooLargeError, match="too large"):
        sandboxRunner.runTrustedSandbox(TRUSTED_ECHO, "p1", b"x" * 5000, {"mode": "echo"}, 120, 1000)


def test_trustedRunChecksItsArgumentsBeforeTakingASlot(trustedEcho, monkeypatch):
    def noSlot(*_args):
        raise AssertionError("took a slot")

    monkeypatch.setattr(sandboxRunner, "acquireRunSlot", noSlot)
    with pytest.raises(TypeError):
        sandboxRunner.runTrustedSandbox(TRUSTED_ECHO, "p1", b"abc", ["not", "a", "dict"], 5, 1024)
    with pytest.raises(TypeError):
        sandboxRunner.runTrustedSandbox(TRUSTED_ECHO, "p1", b"abc", {"x": object()}, 5, 1024)
    for cap in (0, -1):
        with pytest.raises(ValueError):
            sandboxRunner.runTrustedSandbox(TRUSTED_ECHO, "p1", b"abc", {}, 5, cap)


def test_unsafeTrustedResultIsNotATooLargeError(tmp_path):
    result = tmp_path / "result"
    result.write_bytes(b"abc")
    with pytest.raises(ValueError) as caught:  # a file the run's uid does not own
        sandboxRunner.readResult(str(result), 12345, 10)
    assert not isinstance(caught.value, sandboxRunner.ResultTooLargeError)


# ---- The normaliser and the reader ----

PROJECT_ID = "11111111-2222-4333-8444-555555555555"
READER_MEMORY = 268435456
SAVE_FAILED = "The transformation's result could not be read safely."
READ_FAILED = "This table could not be read safely."


def _runOutput(df: pd.DataFrame, metadata: dict | None = None) -> bytes:
    """Bytes the way transform_launcher writes a pandas result."""
    table = pa.Table.from_pandas(df, preserve_index=False)
    if metadata:
        table = table.replace_schema_metadata({**(table.schema.metadata or {}), **metadata})
    sink = io.BytesIO()
    pq.write_table(table, sink, compression="snappy")
    return sink.getvalue()


def _honestFrame(rows: int = 3) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "cat": pd.Categorical([("a", "b", "a")[i % 3] for i in range(rows)]),
            "nullable": pd.array([None if i % 3 == 1 else i for i in range(rows)], dtype="Int64"),
            "s": [None if i % 3 == 2 else f"v{i}" for i in range(rows)],
            "f": np.arange(rows, dtype="float64") / 2,
            "when": pd.date_range("2020-01-01", periods=rows, freq="h"),
        }
    )


def _plain(df: pd.DataFrame) -> pd.DataFrame:
    """An unmarked categorical comes back from the reader as plain strings."""
    return df.assign(cat=df["cat"].astype(object))


def _expectedPage(df: pd.DataFrame, offset: int, limit: int):
    import polars as pl

    return pl.from_pandas(_plain(df).iloc[offset : offset + limit].reset_index(drop=True))


def _expectedInfo(data: bytes) -> dict:
    """What managementService._attributeInfoFunc reads today, as the info dict."""
    parquet = pq.ParquetFile(io.BytesIO(data))
    sample = next(parquet.iter_batches(batch_size=1), None)
    return {
        "columns": [[field.name, str(field.type)] for field in parquet.schema_arrow],
        "rows": parquet.metadata.num_rows,
        "cols": parquet.metadata.num_columns,
        "sample": None if sample is None else sample.to_pandas().to_string(),
        "sampleFailed": False,
    }


def _grouped(df: pd.DataFrame, rows: int) -> bytes:
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), sink, row_group_size=rows)
    return sink.getvalue()


@pytest.fixture
def noChild(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("started a child")

    monkeypatch.setattr(sandboxRunner, "runTrustedSandbox", refuse)


def test_normaliserRoundTripsAnHonestTable():
    df = _honestFrame()
    out = storedTables.normaliseRunOutput("p1", "orders", _runOutput(df))
    back = pd.read_parquet(io.BytesIO(out))
    pd.testing.assert_frame_equal(back, df)
    assert str(back["nullable"].dtype) == "Int64"
    assert str(back["cat"].dtype) == "category"
    metadata = _footer(out)
    assert markerIsValid(metadata, "p1", "orders")
    assert set(metadata.metadata) <= {b"pandas", b"ARROW:schema", MARKER_KEY}
    assert metadata.row_group(0).column(0).compression == "ZSTD"


def test_normaliserRoundTripsAPolarsResult():
    import polars as pl
    from polars.testing import assert_frame_equal

    frame = pl.DataFrame({"a": [1, None, 3], "b": ["x", "y", None]})
    sink = io.BytesIO()
    pq.write_table(frame.to_arrow(), sink, compression="snappy")
    out = storedTables.normaliseRunOutput("p1", "orders", sink.getvalue())
    assert_frame_equal(pl.read_parquet(io.BytesIO(out)), frame)
    assert markerIsValid(_footer(out), "p1", "orders")


def test_normaliserWritesInTheStoredTableLayout():
    n = 2_500_000
    data = _runOutput(pd.DataFrame({"a": np.random.default_rng(1).integers(0, 1 << 62, n)}))
    metadata = _footer(storedTables.normaliseRunOutput("p1", "t", data))
    assert metadata.num_rows == n
    assert [metadata.row_group(i).num_rows for i in range(metadata.num_row_groups)] == [1_048_576, 1_048_576, 402_848]
    assert metadata.row_group(0).column(0).compression == "ZSTD"


def test_normaliserDropsACopiedMarker():
    copied = tableMarker("p1", "other")
    data = _runOutput(_frame(), {MARKER_KEY: copied, b"nubrix.other": b"1", b"extra": b"2"})
    metadata = _footer(storedTables.normaliseRunOutput("p1", "orders", data))
    assert metadata.metadata[MARKER_KEY] == tableMarker("p1", "orders")
    assert not markerIsValid(metadata, "p1", "other")
    assert [key for key in metadata.metadata if key.startswith(b"nubrix.")] == [MARKER_KEY]
    assert b"extra" not in metadata.metadata


def test_normaliserDropsFieldMetadata():
    table = pa.Table.from_pandas(_frame(), preserve_index=False)
    field = table.schema.field("a").with_metadata({MARKER_KEY: tableMarker("p1", "other")})
    table = pa.Table.from_arrays(table.columns, schema=table.schema.set(0, field))
    sink = io.BytesIO()
    pq.write_table(table, sink)
    assert pq.read_schema(io.BytesIO(sink.getvalue())).field("a").metadata  # the input really carries it
    out = storedTables.normaliseRunOutput("p1", "orders", sink.getvalue())
    assert all(not field.metadata for field in pq.read_schema(io.BytesIO(out)))


@pytest.mark.parametrize(
    "pandasEntry",
    [
        b"{bad",
        b"[1, 2]",
        json.dumps({"columns": [{"name": "missing", "field_name": "missing"}]}).encode(),
        json.dumps({"columns": "a"}).encode(),
        json.dumps({"columns": [{"name": "a", "field_name": "a"}], "pad": "x" * (64 * 1024)}).encode(),
    ],
    ids=["notJson", "notAnObject", "unknownColumn", "columnsNotAList", "over64KiB"],
)
def test_normaliserDropsAPandasEntryThatDoesNotFit(pandasEntry):
    data = _runOutput(_frame(), {b"pandas": pandasEntry})
    out = storedTables.normaliseRunOutput("p1", "orders", data)
    assert b"pandas" not in _footer(out).metadata
    assert pq.read_table(io.BytesIO(out)).to_pydict() == _frame().to_dict(orient="list")


def test_normaliserKeepsAValidPandasEntryReserialised():
    data = _runOutput(_honestFrame())
    original = json.loads(_footer(data).metadata[b"pandas"])
    kept = _footer(storedTables.normaliseRunOutput("p1", "orders", data)).metadata[b"pandas"]
    assert json.loads(kept) == original


def test_normaliserRefusesOverTheLimit(monkeypatch):
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", "1048576")
    data = _runOutput(pd.DataFrame({"a": np.arange(1_000_000, dtype="int64")}))
    with pytest.raises(ValueError) as caught:
        storedTables.normaliseRunOutput("p1", "orders", data)
    assert str(caught.value) == "The transformation's result is too large to save (over 1 MiB)."


def test_normaliserFailureGivesTheCouldNotReadCopy():
    for junk in (b"", b"not parquet at all", b"PAR1" + b"\0" * 64 + b"PAR1"):
        with pytest.raises(ValueError) as caught:
            storedTables.normaliseRunOutput("p1", "orders", junk)
        assert str(caught.value) == SAVE_FAILED


def test_normaliseOffReturnsTheInputUnchanged(monkeypatch, noChild):
    monkeypatch.setenv("STORED_TABLE_NORMALISE", "0")
    data = _runOutput(_frame(), {MARKER_KEY: b"copied"})
    assert storedTables.normaliseRunOutput("p1", "orders", data) is data


def test_normaliserWithoutAKeyWritesNoMarker(monkeypatch, warningLog):
    data = _runOutput(_frame(), {MARKER_KEY: tableMarker("p1", "orders")})
    monkeypatch.delenv("SUPABASE_KEY")
    out = storedTables.normaliseRunOutput("p1", "orders", data)
    metadata = _footer(out)
    assert MARKER_KEY not in metadata.metadata
    assert not markerIsValid(metadata, "p1", "orders")
    pd.testing.assert_frame_equal(pd.read_parquet(io.BytesIO(out)), _frame())


@pytest.fixture
def childSees(monkeypatch):
    """Everything a child is started with: its environment and the payload on its stdin."""
    seen = {"env": [], "payload": []}
    realPopen = sandboxRunner.subprocess.Popen
    realWait = sandboxRunner._waitForChild

    def popen(*args, **kwargs):
        seen["env"].append(dict(kwargs.get("env") or {}))
        return realPopen(*args, **kwargs)

    def wait(proc, payload, timeoutSeconds):
        seen["payload"].append(payload)
        return realWait(proc, payload, timeoutSeconds)

    monkeypatch.setattr(sandboxRunner.subprocess, "Popen", popen)
    monkeypatch.setattr(sandboxRunner, "_waitForChild", wait)
    return seen


def test_normaliserChildGetsTheMarkerButNotTheKey(childSees):
    out = storedTables.normaliseRunOutput("p1", "orders", _runOutput(_frame()))
    assert markerIsValid(_footer(out), "p1", "orders")
    assert len(childSees["payload"]) == 1
    # On a run uid the reaper is started too; the launcher is the one given PYTHONPATH.
    assert len([env for env in childSees["env"] if "PYTHONPATH" in env]) == 1
    request = json.loads(childSees["payload"][0])["request"]
    assert request["marker"] == tableMarker("p1", "orders").decode("ascii")
    for text in childSees["payload"] + [json.dumps(env) for env in childSees["env"]]:
        assert "unit-test-key" not in text


def test_readerChildGetsNoKey(childSees):
    storedTables.readUntrustedTable("p1", "orders", _runOutput(_frame()))
    assert len(childSees["payload"]) == 1
    for text in childSees["payload"] + [json.dumps(env) for env in childSees["env"]]:
        assert "unit-test-key" not in text


def test_normaliserAndReaderUseTheirTimeoutsLimitsAndCaps(monkeypatch):
    calls = []

    def record(launcherName, projectId, inputBytes, request, timeoutSeconds, maxResultBytes, limits=None):
        calls.append((launcherName, timeoutSeconds, maxResultBytes, limits))
        raise SandboxBusyError()

    monkeypatch.setattr(sandboxRunner, "runTrustedSandbox", record)
    monkeypatch.setenv("TABLE_READER_MEMORY_BYTES", str(READER_MEMORY))
    for call in (
        lambda: storedTables.normaliseRunOutput("p1", "t", b"x"),
        lambda: storedTables.readUntrustedTable("p1", "t", b"x"),
        lambda: storedTables.readUntrustedPage("p1", "t", b"x", 0, 10),
        lambda: storedTables.readUntrustedInfo("p1", "t", b"x"),
    ):
        with pytest.raises(SandboxBusyError):
            call()
    normalise, *readers = calls
    assert normalise[:2] == ("normalise_launcher.py", 120)
    assert normalise[2] > 0
    assert normalise[3] is None
    assert len(readers) == 3
    # Table and page answers are capped at the decoded-size limit; an info answer has its own 4 MiB cap.
    for (name, timeout, cap, limits), expectedCap in zip(readers, (2147483648, 2147483648, 4 * 1024 * 1024)):
        assert (name, timeout, cap) == ("table_reader_launcher.py", 60, expectedCap)
        assert limits == sandboxRunner.currentRunLimits(maxProcessMemory=READER_MEMORY)
    # With the decoded-size limit off, a table answer is never capped below what it was with the limit on.
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", "0")
    for readerMemory, expectedCap in ((READER_MEMORY, 2147483648), (8 * 1024**3, 8 * 1024**3)):
        monkeypatch.setenv("TABLE_READER_MEMORY_BYTES", str(readerMemory))
        with pytest.raises(SandboxBusyError):
            storedTables.readUntrustedTable("p1", "t", b"x")
        assert calls[-1][2] == expectedCap
    with pytest.raises(SandboxBusyError):
        storedTables.normaliseRunOutput("p1", "t", b"x")
    assert calls[-1][2] > 0


def test_busyIsNeverConverted(monkeypatch):
    def busy(*_args):
        raise SandboxBusyError()

    monkeypatch.setattr(sandboxRunner, "acquireRunSlot", busy)
    data = _runOutput(_frame())
    for call in (
        lambda: storedTables.normaliseRunOutput("p1", "t", data),
        lambda: storedTables.readUntrustedTable("p1", "t", data),
        lambda: storedTables.readUntrustedPage("p1", "t", data, 0, 10),
        lambda: storedTables.readUntrustedInfo("p1", "t", data),
    ):
        with pytest.raises(SandboxBusyError):
            call()


def test_readerMemoryDefaultsTo4GiB(monkeypatch):
    assert storedTables.readerMemoryBytes() == 4294967296
    for raw in ("abc", "-1", ""):
        monkeypatch.setenv("TABLE_READER_MEMORY_BYTES", raw)
        assert storedTables.readerMemoryBytes() == 4294967296
    monkeypatch.setenv("TABLE_READER_MEMORY_BYTES", "0")
    assert storedTables.readerMemoryBytes() == 0


def test_readerReturnsTablePageAndInfo():
    from polars.testing import assert_frame_equal

    df = _honestFrame(30)
    data = _grouped(df, 7)
    table = storedTables.readUntrustedTable("p1", "orders", data)
    pd.testing.assert_frame_equal(table.to_pandas(), _plain(df))
    assert str(table.to_pandas()["nullable"].dtype) == "Int64"
    assert set(table.schema.metadata or {}) <= {b"pandas"}
    for offset, limit in ((0, 5), (5, 10), (28, 10)):
        page, totalRows = storedTables.readUntrustedPage("p1", "orders", data, offset, limit)
        assert totalRows == 30
        assert_frame_equal(page, _expectedPage(df, offset, limit))
    page, totalRows = storedTables.readUntrustedPage("p1", "orders", data, 40, 5)
    assert (page.height, page.columns, totalRows) == (0, list(df.columns), 30)
    assert storedTables.readUntrustedInfo("p1", "orders", data) == _expectedInfo(data)


def test_readerInfoForAnEmptyTableAndAFailedSample():
    empty = io.BytesIO()
    pq.write_table(pa.table({"a": pa.array([], pa.int64())}), empty)
    info = storedTables.readUntrustedInfo("p1", "t", empty.getvalue())
    assert info == {"columns": [["a", "int64"]], "rows": 0, "cols": 1, "sample": None, "sampleFailed": False}
    broken = _runOutput(_frame(), {b"pandas": json.dumps({"index_columns": ["nowhere"], "columns": []}).encode()})
    with pytest.raises(Exception):  # the sample really fails today
        next(pq.ParquetFile(io.BytesIO(broken)).iter_batches(batch_size=1)).to_pandas()
    info = storedTables.readUntrustedInfo("p1", "t", broken)
    assert info == {"columns": [["a", "int64"], ["b", "string"]], "rows": 3, "cols": 2, "sample": None, "sampleFailed": True}


def test_readerDecodesDictionariesAndStripsMetadata():
    table = pa.table({"d": pa.array(["x", "y", "x"]).dictionary_encode(), "n": [1, 2, 3]})
    table = table.replace_schema_metadata({MARKER_KEY: b"copied", b"extra": b"1"})
    sink = io.BytesIO()
    pq.write_table(table, sink)
    assert pa.types.is_dictionary(pq.read_table(io.BytesIO(sink.getvalue())).schema.field("d").type)
    out = storedTables.readUntrustedTable("p1", "t", sink.getvalue())
    assert out.schema.field("d").type == pa.string()
    assert out.column("d").to_pylist() == ["x", "y", "x"]
    assert not out.schema.metadata
    page, _ = storedTables.readUntrustedPage("p1", "t", sink.getvalue(), 0, 2)
    assert page["d"].to_list() == ["x", "y"]


def test_readerRefusesOverTheLimit(monkeypatch):
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", "1048576")
    data = _runOutput(pd.DataFrame({"a": np.arange(1_000_000, dtype="int64")}))
    for call in (
        lambda: storedTables.readUntrustedTable("p1", "t", data),
        lambda: storedTables.readUntrustedPage("p1", "t", data, 0, 10),
    ):
        with pytest.raises(TableTooLargeError) as caught:
            call()
        assert str(caught.value) == "This table is too large to open (over 1 MiB)."


def test_readerFailureGivesTheUnreadableCopy():
    for junk in (b"", b"not parquet at all", b"PAR1" + b"\0" * 64 + b"PAR1"):
        for call in (
            lambda: storedTables.readUntrustedTable("p1", "t", junk),
            lambda: storedTables.readUntrustedPage("p1", "t", junk, 0, 10),
            lambda: storedTables.readUntrustedInfo("p1", "t", junk),
        ):
            with pytest.raises(storedTables.TableUnreadableError) as caught:
                call()
            assert str(caught.value) == READ_FAILED


def _fakeRun(monkeypatch, outcome):
    def run(*_args, **_kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(sandboxRunner, "runTrustedSandbox", run)


def _result(returncode=0, resultBytes=b"", stopped=None, stderr="child said: too large"):
    return sandboxRunner.SandboxResult("", stderr, returncode, resultBytes, None, stopped)


OUT_OF_MEMORY = "The run was stopped: the server ran out of memory."


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (sandboxRunner.ResultTooLargeError("too large"), "too large"),
        (_result(returncode=storedTables.EXIT_TOO_LARGE, resultBytes=None), "too large"),
        (_result(returncode=storedTables.EXIT_TOO_LARGE, resultBytes=None, stopped=OUT_OF_MEMORY), "unreadable"),
        (_result(returncode=1, resultBytes=None), "unreadable"),
        (_result(returncode=-9, resultBytes=None, stopped=OUT_OF_MEMORY), "unreadable"),
        (_result(returncode=0, resultBytes=None), "unreadable"),
        (_result(returncode=0, resultBytes=b"not ipc or json"), "unreadable"),
        (_result(returncode=0, resultBytes=b'{"columns": "x"}'), "unreadable"),
        (ValueError("Sandbox result is not a regular file."), "unreadable"),
        (IsADirectoryError(21, "Is a directory"), "unreadable"),
    ],
)
def test_readerMapsEveryChildOutcome(monkeypatch, warningLog, outcome, expected):
    _fakeRun(monkeypatch, outcome)
    for name, call in (
        ("table", lambda: storedTables.readUntrustedTable("p1", "t", b"x")),
        ("page", lambda: storedTables.readUntrustedPage("p1", "t", b"x", 0, 1)),
        ("info", lambda: storedTables.readUntrustedInfo("p1", "t", b"x")),
    ):
        with pytest.raises((TableTooLargeError, storedTables.TableUnreadableError)) as caught:
            call()
        # An info answer over its own cap is not one the child builds, so it is unreadable, not too large.
        infoOverItsCap = name == "info" and isinstance(outcome, sandboxRunner.ResultTooLargeError)
        if expected == "too large" and not infoOverItsCap:
            assert type(caught.value) is TableTooLargeError
            assert str(caught.value) == "This table is too large to open (over 2 GiB)."
        else:
            assert type(caught.value) is storedTables.TableUnreadableError
            assert str(caught.value) == READ_FAILED


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (sandboxRunner.ResultTooLargeError("too large"), "The transformation's result is too large to save (over 512 MiB)."),
        (_result(returncode=storedTables.EXIT_TOO_LARGE, resultBytes=None), "The transformation's result is too large to save (over 2 GiB)."),
        (_result(returncode=storedTables.EXIT_TOO_LARGE, resultBytes=None, stopped="Execution timed out after 120 seconds."), SAVE_FAILED),
        (_result(returncode=1, resultBytes=None), SAVE_FAILED),
        (_result(returncode=0, resultBytes=None), SAVE_FAILED),
        (ValueError("Sandbox result is not a regular file."), SAVE_FAILED),
        (IsADirectoryError(21, "Is a directory"), SAVE_FAILED),
    ],
)
def test_normaliserMapsEveryChildOutcome(monkeypatch, warningLog, outcome, expected):
    _fakeRun(monkeypatch, outcome)
    with pytest.raises(ValueError) as caught:
        storedTables.normaliseRunOutput("p1", "t", b"x")
    assert type(caught.value) is ValueError
    assert str(caught.value) == expected


def test_failureLogsTheChildsStderrTail(monkeypatch, warningLog):
    _fakeRun(monkeypatch, _result(returncode=1, resultBytes=None, stderr="x" * 10_000 + "the real reason"))
    with pytest.raises(storedTables.TableUnreadableError) as unreadable:
        storedTables.readUntrustedTable("p1", "t", b"x")
    with pytest.raises(ValueError) as unsaved:
        storedTables.normaliseRunOutput("p1", "t", b"x")
    logged = [m for m in warningLog.messages if "the real reason" in m]
    assert len(logged) == 2
    assert all(len(m) < 5000 for m in logged)
    assert "real reason" not in str(unreadable.value) + str(unsaved.value)


def test_readerRevertDecodesInThisProcess(monkeypatch, noChild):
    from polars.testing import assert_frame_equal

    monkeypatch.setenv("TABLE_READER_MEMORY_BYTES", "0")
    df = _honestFrame(30)
    data = _grouped(df, 7)
    table = storedTables.readUntrustedTable("p1", "t", data)
    assert table.equals(pq.read_table(io.BytesIO(data)), check_metadata=True)
    page, totalRows = storedTables.readUntrustedPage("p1", "t", data, 5, 10)
    assert totalRows == 30
    import polars as pl

    assert page.schema["cat"] == pl.Categorical  # decoded here exactly as today
    assert_frame_equal(page.with_columns(pl.col("cat").cast(pl.String)), _expectedPage(df, 5, 10))
    assert storedTables.readUntrustedInfo("p1", "t", data) == _expectedInfo(data)
    # The decoded-size limit stays on its own switch.
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", "1048576")
    big = _runOutput(pd.DataFrame({"a": np.arange(1_000_000, dtype="int64")}))
    for call in (
        lambda: storedTables.readUntrustedTable("p1", "t", big),
        lambda: storedTables.readUntrustedPage("p1", "t", big, 0, 10),
    ):
        with pytest.raises(TableTooLargeError) as caught:
            call()
        assert str(caught.value) == "This table is too large to open (over 1 MiB)."


# ---- Under a 256 MiB process limit, with run uids ----
# These need a file that is small on disk and decodes past that limit.

_EXPANDING_SCRIPT = (
    "import sys, pyarrow as pa, pyarrow.parquet as pq\n"
    "n = 512 * 1024 * 1024 // 8\n"
    'pq.write_table(pa.table({"c0": pa.repeat(pa.scalar(0, pa.int64()), n)}), sys.argv[1], compression="zstd",'
    " use_dictionary=False, data_page_size=1 << 30, row_group_size=n)\n"
)
EXPANDING_SECONDS = 30


@pytest.fixture(scope="module")
def expandingFile(tmp_path_factory):
    """A small file that decodes past the 256 MiB process limit.

    Written by a separate process, so this one never holds it decoded.
    """
    import subprocess
    import sys

    path = tmp_path_factory.mktemp("expanding") / "expanding.parquet"
    subprocess.run([sys.executable, "-c", _EXPANDING_SCRIPT, str(path)], check=True, timeout=300)
    data = path.read_bytes()
    metadata = _footer(data)
    column = metadata.row_group(0).column(0)
    assert metadata.num_row_groups == 1 and column.total_uncompressed_size > 512 * (1 << 20)
    assert len(data) < 4 * (1 << 20)
    return data


@pytest.fixture
def smallRunMemory(monkeypatch):
    monkeypatch.setenv("SANDBOX_MAX_PROCESS_MEMORY_BYTES", str(READER_MEMORY))
    monkeypatch.setenv("TABLE_READER_MEMORY_BYTES", str(READER_MEMORY))


def _peakMib() -> float:
    with open("/proc/self/status") as status:
        for line in status:
            if line.startswith("VmHWM"):
                return int(line.split()[1]) / 1024
    raise AssertionError("no VmHWM")


def _currentMib() -> float:
    with open("/proc/self/status") as status:
        for line in status:
            if line.startswith("VmRSS"):
                return int(line.split()[1]) / 1024
    raise AssertionError("no VmRSS")


def _resetPeak() -> None:
    """Start this process's high-water mark again from its current size, so growth is measured, not hidden by an old peak."""
    with open("/proc/self/clear_refs", "w") as clear:
        clear.write("5")
    assert _peakMib() - _currentMib() < 16


def _failedOnALargeAllocation(logged: str) -> bool:
    """The child got as far as decoding the page, not stopped on a small allocation such as an import's."""
    import re

    sizes = [int(size) for size in re.findall(r"malloc of size (\d+) failed", logged)]
    return bool(sizes) and max(sizes) >= 64 * (1 << 20)


@separateUids
def test_honestTablePassesUnderTheSameLimit(smallRunMemory):
    from polars.testing import assert_frame_equal

    df = _honestFrame(30_000)
    out = storedTables.normaliseRunOutput(PROJECT_ID, "orders", _runOutput(df))
    pd.testing.assert_frame_equal(pd.read_parquet(io.BytesIO(out)), df)
    assert markerIsValid(_footer(out), PROJECT_ID, "orders")
    pd.testing.assert_frame_equal(storedTables.readUntrustedTable(PROJECT_ID, "orders", out).to_pandas(), _plain(df))
    page, totalRows = storedTables.readUntrustedPage(PROJECT_ID, "orders", out, 100, 50)
    assert totalRows == 30_000
    assert_frame_equal(page, _expectedPage(df, 100, 50))
    assert storedTables.readUntrustedInfo(PROJECT_ID, "orders", out) == _expectedInfo(out)


@separateUids
def test_normaliserStopsAnExpandingFile(smallRunMemory, expandingFile, warningLog):
    import time

    _resetPeak()
    before = _peakMib()
    started = time.monotonic()
    with pytest.raises(ValueError) as caught:
        storedTables.normaliseRunOutput(PROJECT_ID, "orders", expandingFile)
    elapsed = time.monotonic() - started
    grew = _peakMib() - before
    print(f"normaliser: {elapsed:.1f} s, parent VmHWM grew {grew:.1f} MiB")
    assert str(caught.value) == SAVE_FAILED
    assert _failedOnALargeAllocation(warningLog.messages[-1])
    assert grew < 64
    assert elapsed < EXPANDING_SECONDS


@separateUids
def test_readerStopsAnExpandingFile(smallRunMemory, expandingFile, warningLog):
    import time

    for name, call in (
        ("table", lambda: storedTables.readUntrustedTable(PROJECT_ID, "orders", expandingFile)),
        ("page", lambda: storedTables.readUntrustedPage(PROJECT_ID, "orders", expandingFile, 0, 1)),
        ("info", lambda: storedTables.readUntrustedInfo(PROJECT_ID, "orders", expandingFile)),
    ):
        started = time.monotonic()
        try:
            result = call()
        except storedTables.TableUnreadableError as error:
            result = error
        elapsed = time.monotonic() - started
        print(f"reader {name}: {elapsed:.1f} s")
        if name == "info":
            # The shape comes from the footer; only the sample needs the page.
            assert result["sample"] is None and result["sampleFailed"] is True, result
        else:
            assert isinstance(result, storedTables.TableUnreadableError) and str(result) == READ_FAILED
            assert _failedOnALargeAllocation(warningLog.messages[-1])
        assert elapsed < EXPANDING_SECONDS


# ---- Fix round 1: whole-table size check, nested dictionaries, info cap ----


def _rowGroups(rows: int = 200_000, groupRows: int = 20_000) -> bytes:
    """Ten small row groups, about 160 KiB each encoded, so only the whole table is over 1 MiB."""
    sink = io.BytesIO()
    pq.write_table(pa.table({"a": pa.array(np.arange(rows, dtype="int64"))}), sink, row_group_size=groupRows, use_dictionary=False)
    data = sink.getvalue()
    metadata = _footer(data)
    assert metadata.num_row_groups == rows // groupRows
    assert metadata.row_group(0).column(0).total_uncompressed_size < 1 << 20 < encodedUncompressedBytes(metadata)
    return data


@pytest.mark.parametrize("readerMemory", [None, "0"], ids=["child", "inProcess"])
def test_pageAndInfoCheckTheWholeTable(monkeypatch, readerMemory):
    if readerMemory is not None:
        monkeypatch.setenv("TABLE_READER_MEMORY_BYTES", readerMemory)
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", "1048576")
    data = _rowGroups()
    with pytest.raises(TableTooLargeError) as caught:
        storedTables.readUntrustedPage("p1", "t", data, 0, 10)
    assert str(caught.value) == "This table is too large to open (over 1 MiB)."
    batchesRead = []
    realBatches = pq.ParquetFile.iter_batches

    def countingBatches(self, *args, **kwargs):
        batchesRead.append(1)
        return realBatches(self, *args, **kwargs)

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", countingBatches)
    info = storedTables.readUntrustedInfo("p1", "t", data)
    assert info == {"columns": [["a", "int64"]], "rows": 200_000, "cols": 1, "sample": None, "sampleFailed": True}
    assert batchesRead == []  # in this process; the child path decodes nothing in this process either


def _nestedDictionaries() -> "pa.Table":
    values = pa.array(["x", "y", "x", "z"]).dictionary_encode()
    return pa.table(
        {
            "l": pa.ListArray.from_arrays(pa.array([0, 2, 3, 3, 4]), values),
            "s": pa.StructArray.from_arrays([values, pa.array([1, 2, 3, 4])], names=["v", "n"]),
            "ll": pa.LargeListArray.from_arrays(pa.array([0, 1, 2, 3, 4], pa.int64()), values),
        }
    )


def _parquet(table: "pa.Table") -> bytes:
    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


def _anyDictionary(dataType) -> bool:
    return "dictionary" in str(dataType)


def test_readerDecodesNestedDictionaries():
    table = _nestedDictionaries()
    data = _parquet(table)
    assert all(_anyDictionary(field.type) for field in pq.read_schema(io.BytesIO(data)))  # they really are stored nested
    out = storedTables.readUntrustedTable("p1", "t", data)
    assert not any(_anyDictionary(field.type) for field in out.schema)
    assert out.to_pylist() == table.to_pylist()
    page, totalRows = storedTables.readUntrustedPage("p1", "t", data, 1, 2)
    assert totalRows == 4
    assert page.select("l", "s").to_dicts() == [{"l": ["x"], "s": {"v": "y", "n": 2}}, {"l": [], "s": {"v": "x", "n": 3}}]


@pytest.mark.parametrize(
    "table",
    [
        pa.table({"l": pa.ListArray.from_arrays(pa.array([0, 1]), pa.array(["x"]).dictionary_encode())}),
        pa.table({"s": pa.StructArray.from_arrays([pa.array(["x"]).dictionary_encode()], names=["v"])}),
        pa.table({"d": pa.array(["x"]).dictionary_encode()}),
    ],
    ids=["listOfDictionary", "structOfDictionary", "dictionary"],
)
def test_parentRefusesAnAnswerHoldingADictionary(monkeypatch, warningLog, table):
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    _fakeRun(monkeypatch, _result(resultBytes=sink.getvalue()))
    with pytest.raises(storedTables.TableUnreadableError) as caught:
        storedTables.readUntrustedTable("p1", "t", b"x")
    assert str(caught.value) == READ_FAILED


def test_normaliserCountsDictionariesAtTheirDecodedSize(monkeypatch):
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", "1048576")
    long = "v" * 20_000
    data = _runOutput(pd.DataFrame({"cat": pd.Categorical([long] * 200)}))
    compact = next(pq.ParquetFile(io.BytesIO(data)).iter_batches(batch_size=65536))
    assert pa.types.is_dictionary(compact.schema.field("cat").type) and compact.nbytes < 1 << 20
    with pytest.raises(ValueError) as caught:
        storedTables.normaliseRunOutput("p1", "t", data)
    assert str(caught.value) == "The transformation's result is too large to save (over 1 MiB)."


def test_normaliserWritesNestedDictionariesPlainAndKeepsTopLevelOnes():
    table = _nestedDictionaries().append_column("top", pa.array(["a", "b", "a", "b"]).dictionary_encode())
    out = storedTables.normaliseRunOutput("p1", "t", _parquet(table))
    back = pq.read_table(io.BytesIO(out))
    for name in ("l", "s", "ll"):
        assert not _anyDictionary(back.schema.field(name).type), back.schema
    assert pa.types.is_dictionary(back.schema.field("top").type)
    assert back.to_pylist() == table.to_pylist()


def test_infoAnswerHasItsOwnCapAndATruncatedSample(monkeypatch):
    data = _parquet(pa.table({"big": ["x" * (8 * 1024 * 1024)], "n": [1]}))
    seen = []
    realRun = sandboxRunner.runTrustedSandbox

    def spy(*args, **kwargs):
        result = realRun(*args, **kwargs)
        seen.append((args[5], len(result.resultBytes or b"")))
        return result

    monkeypatch.setattr(sandboxRunner, "runTrustedSandbox", spy)
    info = storedTables.readUntrustedInfo("p1", "t", data)
    full = next(pq.ParquetFile(io.BytesIO(data)).iter_batches(batch_size=1)).to_pandas().to_string()
    assert info["sample"] == full[: 64 * 1024]
    assert info["sampleFailed"] is False and info["rows"] == 1
    [(cap, answered)] = seen
    assert cap == 4 * 1024 * 1024
    assert answered <= cap


def test_tooLargeExitCodeIsNoRuntimesOwn(monkeypatch, warningLog):
    assert storedTables.EXIT_TOO_LARGE not in (0, 1, 2, 3, 120, 126, 127)
    _fakeRun(monkeypatch, _result(returncode=3, resultBytes=None))  # MSVC abort()
    with pytest.raises(storedTables.TableUnreadableError):
        storedTables.readUntrustedTable("p1", "t", b"x")
    with pytest.raises(ValueError) as caught:
        storedTables.normaliseRunOutput("p1", "t", b"x")
    assert str(caught.value) == SAVE_FAILED


@pytest.mark.parametrize(
    "outcome, call, limit",
    [
        (sandboxRunner.ResultTooLargeError("big"), "table", "2147483648"),
        (_result(returncode=storedTables.EXIT_TOO_LARGE, resultBytes=None), "table", "2147483648"),
        (sandboxRunner.ResultTooLargeError("big"), "normalise", str(512 * 1024 * 1024)),
        (_result(returncode=storedTables.EXIT_TOO_LARGE, resultBytes=None), "normalise", "2147483648"),
        (AssertionError("started a child"), "inProcessTable", "1048576"),
        (AssertionError("started a child"), "inProcessPage", "1048576"),
    ],
    ids=["readerResultCap", "readerExitCode", "normaliserResultCap", "normaliserExitCode", "inProcessTable", "inProcessPage"],
)
def test_everyTooLargeBranchLogsOneLine(monkeypatch, warningLog, outcome, call, limit):
    _fakeRun(monkeypatch, outcome)
    data = b"x"
    if call.startswith("inProcess"):
        monkeypatch.setenv("TABLE_READER_MEMORY_BYTES", "0")
        monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", limit)
        data = _rowGroups()
    with pytest.raises(ValueError):
        if call in ("table", "inProcessTable"):
            storedTables.readUntrustedTable("p1", "orders", data)
        elif call == "inProcessPage":
            storedTables.readUntrustedPage("p1", "orders", data, 0, 10)
        else:
            storedTables.normaliseRunOutput("p1", "orders", data)
    assert len(warningLog.messages) == 1, warningLog.messages
    assert "'orders'" in warningLog.messages[0] and limit in warningLog.messages[0]


@pytest.mark.parametrize("error", [RuntimeError("No sandbox uid is free."), RuntimeError("Could not clear the sandbox run's uid."), TypeError("bad request")])
def test_sandboxErrorsPropagateUnchanged(monkeypatch, error):
    _fakeRun(monkeypatch, error)
    for call in (
        lambda: storedTables.normaliseRunOutput("p1", "t", b"x"),
        lambda: storedTables.readUntrustedTable("p1", "t", b"x"),
        lambda: storedTables.readUntrustedPage("p1", "t", b"x", 0, 1),
        lambda: storedTables.readUntrustedInfo("p1", "t", b"x"),
    ):
        with pytest.raises(type(error)) as caught:
            call()
        assert caught.value is error


# ---- The choke point: every API decode of a stored table ----

import asyncio  # noqa: E402
import hashlib  # noqa: E402
import pickle  # noqa: E402
import re  # noqa: E402
import types  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from test.privateStorageFixtures import SERVICE_KEY, FakePrivateStorage  # noqa: E402
from utils.exceptionHandler import CustomException  # noqa: E402
from utils.sandboxSlots import BUSY_CODE, BUSY_MESSAGE  # noqa: E402

REPOSITORY = Path(__file__).resolve().parent.parent
TOO_LARGE_COPY = "This table is too large to open (over 2 GiB)."
READER = "table_reader_launcher.py"


@pytest.fixture
def storage(monkeypatch):
    fake = FakePrivateStorage()
    monkeypatch.setenv("SUPABASE_URL", fake.url)
    monkeypatch.setenv("SUPABASE_KEY", SERVICE_KEY)
    yield fake
    fake.close()


def _refuseReader(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("went to the reader")

    for name in ("readUntrustedTable", "readUntrustedPage", "readUntrustedInfo"):
        monkeypatch.setattr(storedTables, name, refuse)


def _spyReader(monkeypatch) -> tuple[list, dict]:
    """Record what reaches the reader and answer with a sentinel per call."""
    calls = []
    answers = {"table": pa.table({"sentinel": [1]}), "page": ("page", 7), "info": {"sentinel": True}}

    def recorder(kind):
        def record(projectId, tableName, data, *rest):
            calls.append((kind, projectId, tableName, bytes(data), rest))
            return answers[kind]

        return record

    monkeypatch.setattr(storedTables, "readUntrustedTable", recorder("table"))
    monkeypatch.setattr(storedTables, "readUntrustedPage", recorder("page"))
    monkeypatch.setattr(storedTables, "readUntrustedInfo", recorder("info"))
    return calls, answers


def _polars(df: pd.DataFrame):
    import polars as pl

    return pl.from_pandas(df)


def test_markedTableDecodesInProcess(monkeypatch, storage, noChild):
    df = _honestFrame(30)
    data = writeStoredTable(df, PROJECT_ID, "orders")
    _refuseReader(monkeypatch)

    table = storedTables.decodeStoredTable(PROJECT_ID, "orders", data)
    assert table.equals(pq.read_table(io.BytesIO(data)), check_metadata=True)
    assert storedTables.storedTableInfo(PROJECT_ID, "orders", data) == _expectedInfo(data)

    storage.put(PROJECT_ID, "orders.parquet", data)
    page, totalRows = storedTables.readStoredTablePage(PROJECT_ID, "orders", 5, 10)
    assert totalRows == 30
    assert page.to_dicts() == _polars(df).slice(5, 10).to_dicts()
    reads = [r for r in storage.requests if r["method"] == "GET"]
    assert reads and all(r["range"] for r in reads)  # footer and row groups only, as the data grid read before


def test_unmarkedTableGoesToTheReader(monkeypatch, storage):
    calls, answers = _spyReader(monkeypatch)
    cases = {
        "unmarked": _runOutput(_frame()),
        "anotherTablesMarker": writeStoredTable(_frame(), PROJECT_ID, "other"),
        "anotherProjectsMarker": writeStoredTable(_frame(), "p2", "orders"),
        "forgedMarker": _runOutput(_frame(), {MARKER_KEY: b"0" * 64}),
        "notParquet": b"not a parquet file at all",
    }
    for case, data in cases.items():
        calls.clear()
        assert storedTables.decodeStoredTable(PROJECT_ID, "orders", data) is answers["table"], case
        assert storedTables.storedTableInfo(PROJECT_ID, "orders", data) is answers["info"], case
        storage.put(PROJECT_ID, "orders.parquet", data)
        assert storedTables.readStoredTablePage(PROJECT_ID, "orders", 3, 4) == answers["page"], case
        assert calls == [
            ("table", PROJECT_ID, "orders", data, ()),
            ("info", PROJECT_ID, "orders", data, ()),
            ("page", PROJECT_ID, "orders", data, (3, 4)),
        ], case


def test_markedTableWithAFooterOverOneMiBGoesToTheReader(monkeypatch, storage):
    wide = pa.table({f"c{i}": pa.array([1], pa.int8()) for i in range(20_000)})
    data = writeStoredTable(wide, PROJECT_ID, "orders")
    assert int.from_bytes(data[-8:-4], "little") > storedTables.MAX_INPROCESS_FOOTER_BYTES
    calls, _ = _spyReader(monkeypatch)
    storedTables.decodeStoredTable(PROJECT_ID, "orders", data)
    storedTables.storedTableInfo(PROJECT_ID, "orders", data)
    storage.put(PROJECT_ID, "orders.parquet", data)
    storedTables.readStoredTablePage(PROJECT_ID, "orders", 0, 1)
    assert [call[0] for call in calls] == ["table", "info", "page"]


def test_oversizedMarkedTableIsRefusedBeforeDecoding(monkeypatch, storage, noChild):
    data = writeStoredTable(pd.DataFrame({"a": np.arange(10_000, dtype="int64")}), PROJECT_ID, "orders")
    storage.put(PROJECT_ID, "orders.parquet", data)
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", "1024")
    _refuseReader(monkeypatch)

    def refuse(*_args, **_kwargs):
        raise AssertionError("decoded a table over the limit")

    monkeypatch.setattr(pq, "read_table", refuse)
    monkeypatch.setattr(pq.ParquetFile, "iter_batches", refuse)
    monkeypatch.setattr(pq.ParquetFile, "read", refuse)
    with pytest.raises(TableTooLargeError):
        storedTables.decodeStoredTable(PROJECT_ID, "orders", data)
    with pytest.raises(TableTooLargeError):
        storedTables.readStoredTablePage(PROJECT_ID, "orders", 0, 10)
    info = storedTables.storedTableInfo(PROJECT_ID, "orders", data)
    assert info == {"columns": [["a", "int64"]], "rows": 10_000, "cols": 1, "sample": None, "sampleFailed": True}


def test_normaliseOffIgnoresMarkers(monkeypatch, storage):
    data = writeStoredTable(_frame(), PROJECT_ID, "orders")
    storage.put(PROJECT_ID, "orders.parquet", data)
    monkeypatch.setenv("STORED_TABLE_NORMALISE", "0")
    calls, _ = _spyReader(monkeypatch)
    storedTables.decodeStoredTable(PROJECT_ID, "orders", data)
    storedTables.storedTableInfo(PROJECT_ID, "orders", data)
    storedTables.readStoredTablePage(PROJECT_ID, "orders", 0, 1)
    assert [call[0] for call in calls] == ["table", "info", "page"]


def test_tailFooterReadsOnlyAFooterItMayParse(storage):
    from utils import projectStorage

    small = writeStoredTable(_frame(), PROJECT_ID, "orders")
    storage.put(PROJECT_ID, "orders.parquet", small)
    with projectStorage.openTableFile(PROJECT_ID, "orders") as tableFile:
        metadata = storedTables.readTailFooter(tableFile)
    assert markerIsValid(metadata, PROJECT_ID, "orders")

    wide = pa.table({f"c{i}": pa.array([1], pa.int8()) for i in range(20_000)})
    big = writeStoredTable(wide, PROJECT_ID, "wide")
    storage.put(PROJECT_ID, "wide.parquet", big)
    storage.requests.clear()
    with projectStorage.openTableFile(PROJECT_ID, "wide") as tableFile:
        assert storedTables.readTailFooter(tableFile) is None
    ranges = [r["range"] for r in storage.requests if r["method"] == "GET"]
    assert sum(int(end) - int(start) + 1 for start, end in (r.split("=")[1].split("-") for r in ranges)) <= 64 * 1024

    doctored = bytearray(small)
    doctored[-8:-4] = len(small).to_bytes(4, "little")
    for name, body in (("short", b"PAR1"), ("junk", b"x" * 100), ("doctored", bytes(doctored))):
        storage.put(PROJECT_ID, f"{name}.parquet", body)
        with projectStorage.openTableFile(PROJECT_ID, name) as tableFile:
            assert storedTables.readTailFooter(tableFile) is None, name


def test_redisParquetFallbackUsesTheChokePoint(monkeypatch):
    from utils import initMethods

    blob = _runOutput(_frame())
    monkeypatch.setattr(initMethods, "_read_redis_bytes", lambda _key: blob)
    monkeypatch.setattr(initMethods, "_store_to_redis", lambda *_args: None)
    seen = []

    def choke(projectId, tableName, data):
        seen.append((projectId, tableName, data))
        return pa.Table.from_pandas(_frame(), preserve_index=False)

    def refuse(*_args, **_kwargs):
        raise AssertionError("decoded around the choke point")

    monkeypatch.setattr(initMethods, "decodeStoredTable", choke)
    monkeypatch.setattr(pq, "read_table", refuse)
    initMethods.invalidate_data_cache()
    try:
        assert initMethods._fetch_data_from_redis(f"{PROJECT_ID}::orders", PROJECT_ID, "orders").num_rows == 3
        pd.testing.assert_frame_equal(initMethods.fetch_data(PROJECT_ID, "orders"), _frame())
        assert seen == [(PROJECT_ID, "orders", blob)] * 2

        initMethods.invalidate_data_cache()
        monkeypatch.setattr(initMethods, "decodeStoredTable", _raiser(SandboxBusyError))
        with pytest.raises(SandboxBusyError):
            initMethods.fetch_data(PROJECT_ID, "orders")

        # Arrow IPC the API wrote itself is still read directly.
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, pa.table({"a": [1, 2]}).schema) as writer:
            writer.write_table(pa.table({"a": [1, 2]}))
        monkeypatch.setattr(initMethods, "_read_redis_bytes", lambda _key: sink.getvalue().to_pybytes())
        monkeypatch.setattr(initMethods, "decodeStoredTable", refuse)
        assert initMethods._fetch_data_from_redis(f"{PROJECT_ID}::orders", PROJECT_ID, "orders").num_rows == 2
    finally:
        initMethods.invalidate_data_cache()


def test_coldReadUsesTheChokePoint(monkeypatch):
    from utils import initMethods

    blob = _runOutput(_frame())
    monkeypatch.setattr(initMethods, "_fetch_data_from_redis", lambda *_args: None)
    monkeypatch.setattr(initMethods, "_store_to_redis", lambda *_args: None)
    monkeypatch.setattr(initMethods, "readTableBytes", lambda projectId, tableName: blob)
    seen = []
    monkeypatch.setattr(initMethods, "decodeStoredTable", lambda *args: seen.append(args) or pa.table({"a": [1]}))
    initMethods.invalidate_data_cache()
    try:
        assert initMethods.fetch_data_pl(PROJECT_ID, "orders").height == 1
    finally:
        initMethods.invalidate_data_cache()
    assert seen == [(PROJECT_ID, "orders", blob)]


def test_servingARunsTablesNeverDecodesThem(monkeypatch, storage, tmp_path):
    """A run holds a slot while its tables are served, so serving only moves bytes: a reader would need a slot too."""
    from utils import initMethods

    def refuse(*_args, **_kwargs):
        raise AssertionError("decoded while serving a run")

    for name in ("decodeStoredTable", "readStoredTablePage", "storedTableInfo", "readUntrustedTable", "readUntrustedPage", "readUntrustedInfo"):
        monkeypatch.setattr(storedTables, name, refuse, raising=False)
    monkeypatch.setattr(initMethods, "decodeStoredTable", refuse, raising=False)
    monkeypatch.setattr(sandboxRunner, "runTrustedSandbox", refuse)
    monkeypatch.setattr(initMethods, "_store_file_in_redis", lambda *_args: None)
    data = _runOutput(_frame())
    storage.put(PROJECT_ID, "orders.parquet", data)

    monkeypatch.setattr(initMethods, "_read_redis_bytes", lambda _key: None)
    with open(initMethods.spool_table(PROJECT_ID, "orders", str(tmp_path / "0")), "rb") as spooled:
        assert spooled.read() == data
    monkeypatch.setattr(initMethods, "_read_redis_bytes", lambda _key: data)
    with open(initMethods.spool_table(PROJECT_ID, "orders", str(tmp_path / "1")), "rb") as spooled:
        assert spooled.read() == data


# ---- Each site's answer to a refusal ----

REFUSALS = [
    pytest.param(lambda: SandboxBusyError(), 503, BUSY_CODE, BUSY_MESSAGE, id="busy"),
    pytest.param(lambda: TableTooLargeError(TOO_LARGE_COPY), 413, "TABLE_TOO_LARGE", TOO_LARGE_COPY, id="tooLarge"),
    pytest.param(lambda: storedTables.TableUnreadableError(READ_FAILED), 422, "TABLE_UNREADABLE", READ_FAILED, id="unreadable"),
]


def _raiser(make):
    def raiseIt(*_args, **_kwargs):
        raise make()

    return raiseIt


def _assertAnswer(exception, status, code, message):
    assert isinstance(exception, CustomException), repr(exception)
    assert (exception.statusCode, exception.errorCode, exception.uiMessage) == (status, code, message)


@pytest.mark.parametrize("make, status, code, message", REFUSALS)
def test_tableReadExceptionMapsEachRefusal(make, status, code, message):
    _assertAnswer(storedTables.tableReadException(make()), status, code, message)
    existing = CustomException(ValueError("x"), statusCode=409, uiMessage="taken")
    assert storedTables.tableReadException(existing) is existing
    busyAnswer = CustomException(ValueError("x"), statusCode=503, uiMessage=BUSY_MESSAGE, errorCode=BUSY_CODE)
    assert storedTables.tableReadException(busyAnswer) is busyAnswer
    assert storedTables.tableReadException(RuntimeError("other")) is None
    assert storedTables.tableReadException(ValueError("other")) is None


@pytest.mark.parametrize("make, status, code, message", REFUSALS)
def test_dataGridAnswersEachRefusal(monkeypatch, make, status, code, message):
    from api.routers import manager

    monkeypatch.setattr(manager, "readStoredTablePage", _raiser(make), raising=False)
    monkeypatch.setattr(manager, "readTablePage", _raiser(make), raising=False)
    monkeypatch.setattr(manager, "requireProjectOwner", lambda *_args: None)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(manager.viewTable(PROJECT_ID, "orders", page=1, pageSize=2, user=MagicMock(userId="user_1")))
    assert caught.value.status_code == status
    assert (caught.value.detail.get("errorCode"), caught.value.detail["message"]) == (code, message)


@pytest.mark.parametrize("make, status, code, message", REFUSALS)
def test_getAllColumnsAnswersEachRefusal(monkeypatch, make, status, code, message):
    from api.services import dashboardService as dashboardModule

    monkeypatch.setattr(dashboardModule, "listProjectFiles", lambda *_args: [{"name": "orders.parquet"}, {"name": "items.parquet"}])
    monkeypatch.setattr(dashboardModule, "readTableBytes", lambda *_args: _runOutput(_frame()))
    monkeypatch.setattr(dashboardModule, "decodeStoredTable", _raiser(make), raising=False)
    monkeypatch.setattr(storedTables, "readUntrustedTable", _raiser(make))
    with pytest.raises(CustomException) as caught:
        dashboardModule.dashboardService.getAllColumns(PROJECT_ID)
    _assertAnswer(caught.value, status, code, message)


def _infoReturns(monkeypatch, outcome):
    from api.services import managementService as managementModule

    monkeypatch.setattr(managementModule, "readTableBytes", lambda *_args: b"bytes")
    monkeypatch.setattr(managementModule, "storedTableInfo", outcome if callable(outcome) else (lambda *_args: outcome), raising=False)
    return managementModule.managementService


@pytest.mark.parametrize(
    "make",
    [lambda: TableTooLargeError(TOO_LARGE_COPY), lambda: storedTables.TableUnreadableError(READ_FAILED), lambda: RuntimeError("No sandbox uid is free.")],
    ids=["tooLarge", "unreadable", "sandboxError"],
)
def test_attributeInfoKeepsItsFallbackForATableItCannotRead(monkeypatch, make):
    service = _infoReturns(monkeypatch, _raiser(make))
    assert service._attributeInfoFunc(PROJECT_ID, "orders") == ("DATAFRAME NAME: orders\n(Metadata read failed)\n", None)


def test_attributeInfoReRaisesBusy(monkeypatch):
    service = _infoReturns(monkeypatch, _raiser(SandboxBusyError))
    with pytest.raises(SandboxBusyError):
        service._attributeInfoFunc(PROJECT_ID, "orders")


@pytest.mark.parametrize(
    "info, sampleLine",
    [
        ({"sample": "   a\n0  1", "sampleFailed": False}, "SAMPLE ROW:\n   a\n0  1\n"),
        ({"sample": None, "sampleFailed": False}, "SAMPLE ROW: (no rows)\n"),
        ({"sample": None, "sampleFailed": True}, "SAMPLE ROW: (failed to load)\n"),
    ],
    ids=["sample", "noRows", "sampleFailed"],
)
def test_attributeInfoKeepsItsFormat(monkeypatch, info, sampleLine):
    service = _infoReturns(monkeypatch, {"columns": [["a", "int64"], ["b", "string"]], "rows": 4, "cols": 2, **info})
    text, shape = service._attributeInfoFunc(PROJECT_ID, "orders")
    assert text == "DATAFRAME NAME: orders\n- a (int64)\n- b (string)\nSHAPE: (4, 2)\n" + sampleLine
    assert shape == [4, 2]


def test_metadataGenerationAnswersBusyWithoutAskingTheModel(monkeypatch):
    from api.services import managementService as managementModule

    service = _infoReturns(monkeypatch, _raiser(SandboxBusyError))
    model = MagicMock()
    model.getMetadataChain.side_effect = AssertionError("asked the model")
    monkeypatch.setattr(service, "metadataGenerator", model)
    monkeypatch.setattr(managementModule, "listProjectFiles", lambda *_args: [{"name": "orders.parquet", "metadata": {"lastModified": "2026-11-01T00:00:00Z"}}])
    for call in (
        lambda: service._generateMetadata(PROJECT_ID, tableNames=["orders"]),
        lambda: service.generateMetadataForTables(PROJECT_ID, ["orders"]),
        lambda: service.generateMetadata(PROJECT_ID),
    ):
        with pytest.raises(CustomException) as caught:
            call()
        _assertAnswer(caught.value, 503, BUSY_CODE, BUSY_MESSAGE)


@pytest.mark.parametrize("make, status, code, message", REFUSALS)
def test_profilingReportAnswersEachRefusal(monkeypatch, make, status, code, message):
    from api.services import managementService as managementModule

    error = make()
    copy = pickle.loads(pickle.dumps(error))  # it comes back from a process pool this way
    assert (type(copy), str(copy)) == (type(error), str(error))
    service = managementModule.managementService
    generator = MagicMock()
    generator.getAllTables.return_value = ["orders"]
    generator.getProfilingReport.side_effect = _raiser(lambda: copy)
    monkeypatch.setattr(service, "reportGenerator", generator)
    monkeypatch.setattr(managementModule, "ProcessPoolExecutor", ThreadPoolExecutor)
    with pytest.raises(CustomException) as caught:
        service.generateReport(PROJECT_ID)
    _assertAnswer(caught.value, status, code, message)


def test_profilingRefusalComesBackFromARealProcessPool(monkeypatch, storage):
    from api.services import managementService as managementModule
    from nubrix.components.reportGenerator import ReportGenerator

    storage.putJson(PROJECT_ID, "metadata.json", {"orders": {}})
    storage.put(PROJECT_ID, "orders.parquet", writeStoredTable(pd.DataFrame({"a": np.arange(1_000_000, dtype="int64")}), PROJECT_ID, "orders"))
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", "1048576")
    service = managementModule.managementService
    monkeypatch.setattr(service, "reportGenerator", ReportGenerator())
    with pytest.raises(CustomException) as caught:
        service.generateReport(PROJECT_ID)
    _assertAnswer(caught.value, 413, "TABLE_TOO_LARGE", "This table is too large to open (over 1 MiB).")


def test_profilingDecodesThroughTheChokePoint(monkeypatch):
    from nubrix.components import reportGenerator as reportGeneratorModule

    seen = []
    monkeypatch.setattr(reportGeneratorModule, "readTableBytes", lambda *_args: b"bytes")
    monkeypatch.setattr(reportGeneratorModule, "decodeStoredTable", lambda *args: seen.append(args) or pa.table({"a": [1, 2]}), raising=False)

    class Report:
        def __init__(self, frame, title):
            seen.append(len(frame.index))

        def to_html(self):
            return (
                '<button id="tab-overview-reproduction"></button><div id="tab-pane-overview-reproduction"></div>'
                '<p class="text-body-secondary text-end"></p><footer></footer><main>report</main>'
            )

    monkeypatch.setattr(reportGeneratorModule, "ProfileReport", Report)
    generator = reportGeneratorModule.ReportGenerator()
    assert "report" in generator.getProfilingReport(PROJECT_ID, "orders")
    assert seen == [(PROJECT_ID, "orders", b"bytes"), 2]


def _panelChart(projectId=PROJECT_ID):
    return types.SimpleNamespace(
        projectId=projectId, dataSource="orders", chartType="bar", xAxis="a", yAxis="b", aggregationMetric="sum",
        index=None, columns=None, values=None, selectedColumns=None, mapType=None, isFilterApplied=False, filters=None,
        zipCodeColumn=None,
    )


@pytest.mark.parametrize("sizeClass", ["small", "large"])
@pytest.mark.parametrize("make, status, code, message", REFUSALS)
def test_panelChartAnswersEachRefusal(monkeypatch, sizeClass, make, status, code, message):
    from api.services import reportingService as reportingModule
    from utils import initMethods

    service = object.__new__(reportingModule.ReportingService)
    bucket = MagicMock()
    bucket.list.return_value = [{"name": "orders.parquet"}]
    service.client = MagicMock()
    service.client.storage.from_.return_value = bucket
    monkeypatch.setattr(initMethods, "classify_table_size", lambda *_args: {"size_class": sizeClass})
    monkeypatch.setattr(initMethods, "_load_table", _raiser(make))
    with pytest.raises(CustomException) as caught:
        asyncio.run(service.generatePanelChart(_panelChart()))
    _assertAnswer(caught.value, status, code, message)


# ---- A run's output is normalised once, at preview ----


def _executorWithRun(monkeypatch, parquetBytes: bytes):
    from nubrix.components import transformationExecutor as executorModule

    preview = json.dumps([{"row": 1}]).encode()
    monkeypatch.setattr(executorModule, "runSandbox", lambda *_args, **_kwargs: ("", "", 0, parquetBytes, preview, None))
    executor = object.__new__(executorModule.TransformationExecutor)
    executor.timeoutSeconds = 5
    bucket = MagicMock()
    executor.client = MagicMock()
    executor.client.storage.from_.return_value = bucket
    executor._redis_client = MagicMock()
    return executorModule, executor, bucket


def test_previewCachesNormalisedBytes(monkeypatch):
    from api.services.transformationService import TransformationService

    df = _honestFrame(5)
    # A run can copy another table's marker into its output; normalising replaces it with this table's.
    raw = _runOutput(df, {MARKER_KEY: tableMarker(PROJECT_ID, "sales")})
    _, executor, bucket = _executorWithRun(monkeypatch, raw)

    rows, out = executor.executeAndPreview(PROJECT_ID, "code", "summary")
    assert rows == [{"row": 1}]
    assert out != raw
    assert markerIsValid(_footer(out), PROJECT_ID, "summary")
    assert not markerIsValid(_footer(out), PROJECT_ID, "sales")
    pd.testing.assert_frame_equal(pd.read_parquet(io.BytesIO(out)), df)

    service = object.__new__(TransformationService)
    service._executor = executor
    cached = {}
    redisFake = MagicMock()
    redisFake.set.side_effect = lambda name, value, ex: cached.__setitem__(name, value)
    service._redis_client = lambda: redisFake
    message = {"message_id": "m1", "role": "assistant", "python_code": "code", "artifact": {"code": "graph"}}
    service._ensure_transformation = lambda **_kwargs: {"transformation_name": "summary", "messages": [message]}
    service._validate_transformation_table_name = lambda **_kwargs: None
    service._save_messages_to_db = lambda **_kwargs: None
    assert asyncio.run(service.approveMessage(PROJECT_ID, "t1", "m1")) == {"data": [{"row": 1}]}
    (preview,) = cached.values()
    assert list(cached) == [service._preview_cache_key(PROJECT_ID, "t1", "m1", "summary")]
    assert markerIsValid(_footer(preview), PROJECT_ID, "summary")

    service._claim_table_name = lambda *_args: (True, None)
    service._write_claimed_table(PROJECT_ID, "t1", "summary", preview)
    uploaded = bucket.upload.call_args.kwargs["file"]
    assert uploaded == preview
    # Storage's eTag of the upload is the MD5 of these same bytes, so the table file record still matches it.
    assert TransformationService._holds_written_content({"eTag": f'"{hashlib.md5(uploaded).hexdigest()}"'}, preview)


def test_previewCacheKeyIsVersioned():
    from api.services.transformationService import TransformationService

    key = object.__new__(TransformationService)._preview_cache_key(PROJECT_ID, "t1", "m1", "summary")
    # A preview cached before normalising existed sat under the unversioned key; nothing may find it now.
    assert key == f"{PROJECT_ID}::transformation_preview::v2::t1::m1::summary"
    assert key != f"{PROJECT_ID}::transformation_preview::t1::m1::summary"
    # The user-erasure sweep deletes everything under this prefix.
    assert key.startswith(f"{PROJECT_ID}::")


def test_everyPreviewCacheAccessUsesThePreviewCacheKey():
    import ast

    source = (REPOSITORY / "api" / "services" / "transformationService.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert source.count("transformation_preview") == 1

    def isKeyCall(node):
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_preview_cache_key"

    accesses = 0
    for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
        if function.name in ("_get_metadata", "_preview_cache_key"):
            continue
        assigned = {}
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                assigned.setdefault(node.targets[0].id, []).append(node.value)
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("get", "set", "delete", "unlink", "setex")):
                continue
            receiver = ast.unparse(node.func.value)
            if "redis" not in receiver.lower():
                continue
            keyArgument = node.args[0] if node.args else next(keyword.value for keyword in node.keywords if keyword.arg == "name")
            if isKeyCall(keyArgument):
                accesses += 1
            else:
                assert isinstance(keyArgument, ast.Name), ast.unparse(node)
                assert assigned[keyArgument.id] and all(isKeyCall(value) for value in assigned[keyArgument.id]), ast.unparse(node)
                accesses += 1
    # approveMessage writes, applyTransformation reads and deletes, the rollback deletes twice.
    assert accesses == 5


@pytest.mark.parametrize(
    "error, status, code, message",
    [
        (ValueError(SAVE_FAILED), 400, None, SAVE_FAILED),
        (ValueError("The transformation's result is too large to save (over 2 GiB)."), 400, None, "The transformation's result is too large to save (over 2 GiB)."),
        (SandboxBusyError(), 503, BUSY_CODE, BUSY_MESSAGE),
    ],
    ids=["unreadable", "tooLarge", "busy"],
)
def test_previewAnswersTheNormalisersRefusals(monkeypatch, error, status, code, message):
    executorModule, executor, _ = _executorWithRun(monkeypatch, _runOutput(_frame()))
    monkeypatch.setattr(executorModule, "normaliseRunOutput", _raiser(lambda: error), raising=False)
    monkeypatch.setattr(storedTables, "normaliseRunOutput", _raiser(lambda: error))
    with pytest.raises(CustomException) as caught:
        executor.executeAndPreview(PROJECT_ID, "code", "summary")
    _assertAnswer(caught.value, status, code, message)


def test_validationRunIsNotNormalised(monkeypatch):
    executorModule, executor, _ = _executorWithRun(monkeypatch, _runOutput(_frame()))
    refuse = _raiser(lambda: AssertionError("normalised a validation run"))
    monkeypatch.setattr(executorModule, "normaliseRunOutput", refuse, raising=False)
    monkeypatch.setattr(storedTables, "normaliseRunOutput", refuse)
    executor._execute_code(PROJECT_ID, "code")


# ---- Routes that call a blocking service keep it off the event loop and still answer a busy reader with 503 ----

ROUTES = [("dashboard", "dashboardService", "getAllColumns"), ("manager", "managementService", "generateMetadata"), ("manager", "managementService", "generateReport")]


def _route(monkeypatch, moduleName, serviceName, name):
    import importlib

    module = importlib.import_module(f"api.routers.{moduleName}")
    monkeypatch.setattr(module, "requireProjectOwner", lambda *_args: None)
    user = MagicMock(userId="user_1")
    return getattr(module, serviceName), lambda: getattr(module, name)(PROJECT_ID, user=user)


@pytest.mark.parametrize("moduleName, serviceName, name", ROUTES, ids=[route[2] for route in ROUTES])
def test_blockingRoutesRunTheServiceOffTheEventLoop(monkeypatch, moduleName, serviceName, name):
    import threading

    service, call = _route(monkeypatch, moduleName, serviceName, name)
    ranOnLoopThread = []

    def run(**_kwargs):
        ranOnLoopThread.append(threading.current_thread() is loopThread[0])
        return {}

    loopThread = []

    async def serve():
        loopThread.append(threading.current_thread())
        return await call()

    monkeypatch.setattr(service, name, run)
    assert asyncio.run(serve()).status_code == 200
    assert ranOnLoopThread == [False]


@pytest.mark.parametrize("moduleName, serviceName, name", ROUTES, ids=[route[2] for route in ROUTES])
def test_blockingRoutesAnswerBusyWith503(monkeypatch, moduleName, serviceName, name):
    service, call = _route(monkeypatch, moduleName, serviceName, name)

    def busy(**_kwargs):
        raise CustomException(SandboxBusyError(), statusCode=503, uiMessage=BUSY_MESSAGE, errorCode=BUSY_CODE)

    monkeypatch.setattr(service, name, busy)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(call())
    assert caught.value.status_code == 503
    assert (caught.value.detail["errorCode"], caught.value.detail["message"]) == (BUSY_CODE, BUSY_MESSAGE)


# ---- An honest table the API did not mark still opens everywhere ----


def _uploadStyleTable() -> bytes:
    """Written the way the CSV loader writes a table, as every table stored before marking was."""
    from api.services.dataLoadService import DataLoadService

    df = pd.DataFrame(
        {
            "id": [1, 2, 3, 4, 5],
            "amount": [1.5, None, 3.25, 4.0, 5.5],
            "region": ["north", None, "south", "east", "₹"],
            "when": pd.date_range("2026-01-01", periods=5, freq="D"),
            "flag": [True, False, True, True, False],
        }
    )
    sink = io.BytesIO()
    DataLoadService._sanitizeDfForParquet(df).to_parquet(sink, compression="snappy")
    return sink.getvalue()


def _todaysAttributeInfo(name: str, data: bytes) -> str:
    """What managementService._attributeInfoFunc wrote before the choke point."""
    parquet = pq.ParquetFile(io.BytesIO(data))
    text = f"DATAFRAME NAME: {name}\n"
    for field in parquet.schema_arrow:
        text += f"- {field.name} ({field.type})\n"
    text += f"SHAPE: ({parquet.metadata.num_rows}, {parquet.metadata.num_columns})\n"
    sample = next(parquet.iter_batches(batch_size=1), None)
    return text + ("SAMPLE ROW: (no rows)\n" if sample is None else "SAMPLE ROW:\n" + str(sample.to_pandas().to_string()) + "\n")


def test_unmarkedHonestTableStillOpensEverywhere(monkeypatch, storage):
    """Through the real reader child: as a run uid under a root parent on Linux, as this user on Windows."""
    import polars as pl

    from api.routers import manager
    from api.services import dashboardService as dashboardModule
    from api.services import managementService as managementModule
    from utils import initMethods

    body = _uploadStyleTable()
    assert not markerIsValid(_footer(body), PROJECT_ID, "sales")
    storage.put(PROJECT_ID, "sales.parquet", body)
    readerRuns, runUids = [], []
    realRun, realClaim = sandboxRunner.runTrustedSandbox, sandboxRunner._claimRun

    def countingRun(launcherName, *args, **kwargs):
        readerRuns.append(launcherName)
        return realRun(launcherName, *args, **kwargs)

    def recordingClaim():
        claimed = realClaim()
        runUids.append(claimed[0])
        return claimed

    monkeypatch.setattr(sandboxRunner, "runTrustedSandbox", countingRun)
    monkeypatch.setattr(sandboxRunner, "_claimRun", recordingClaim)
    monkeypatch.setattr(manager, "requireProjectOwner", lambda *_args: None)

    def viewTable():
        response = asyncio.run(manager.viewTable(PROJECT_ID, "sales", page=2, pageSize=2, user=MagicMock(userId="user_1")))
        return json.loads(response.body)

    grid = viewTable()
    with monkeypatch.context() as today:
        todaysPage = lambda p, t, offset, limit: (pl.read_parquet(io.BytesIO(body)).slice(offset, limit), 5)  # noqa: E731
        today.setattr(manager, "readStoredTablePage", todaysPage, raising=False)
        today.setattr(manager, "readTablePage", todaysPage, raising=False)
        assert grid == viewTable()

    columnTypes = dashboardModule.dashboardService._getDataTypes(PROJECT_ID, "sales")
    with monkeypatch.context() as today:
        today.setattr(dashboardModule, "decodeStoredTable", lambda p, t, data: types.SimpleNamespace(to_pandas=lambda: pd.read_parquet(io.BytesIO(data))), raising=False)
        assert columnTypes == dashboardModule.dashboardService._getDataTypes(PROJECT_ID, "sales")

    info, shape = managementModule.managementService._attributeInfoFunc(PROJECT_ID, "sales")
    assert (info, shape) == (_todaysAttributeInfo("sales", body), [5, 5])

    monkeypatch.setattr(initMethods, "_fetch_data_from_redis", lambda *_args: None)
    monkeypatch.setattr(initMethods, "_store_to_redis", lambda *_args: None)
    initMethods.invalidate_data_cache()
    try:
        pd.testing.assert_frame_equal(initMethods.fetch_data(PROJECT_ID, "sales"), pq.read_table(io.BytesIO(body)).to_pandas())
    finally:
        initMethods.invalidate_data_cache()

    assert readerRuns == [READER] * 4
    if os.name == "posix" and os.geteuid() == 0:
        assert len(runUids) == 4 and None not in runUids


# ---- Nothing decodes a stored table around the choke point ----

# A Parquet decode in any spelling pandas, polars and pyarrow offer.
_DECODE = re.compile(r"read_parquet|scan_parquet|\bread_table\b|ParquetFile|ParquetDataset|iter_batches|read_row_groups?\b|read_metadata|read_schema|read_pandas|pyarrow\.dataset|\bds\.dataset\(|ParquetReader")
# Parent-side files where a decode is allowed, and the lines (None = the whole file).
_DECODERS = {
    # The choke point and the bounded decoders it shares with the reader child.
    "utils/storedTables.py": None,
    # sandboxRunner's child-side readTable: a run decodes the tables it was served, under its own limits.
    "utils/sandboxRunner.py": {"return pq.read_table(path)"},
}
# The launchers that decode run in a sandbox child under the run limits: transform_launcher reads back its own output for
# the preview, normalise_launcher and table_reader_launcher are the normaliser and the reader. inspect_launcher and
# sandbox_launcher decode nothing themselves (they read tables only through sandboxRunner's channel), so they are not
# listed: a decode added to either one must be reviewed and named here.
_CHILD_SIDE = {"utils/transform_launcher.py", "utils/normalise_launcher.py", "utils/table_reader_launcher.py"}


def _decodeLines():
    for folder in ("api", "nubrix", "utils"):
        for path in sorted((REPOSITORY / folder).rglob("*.py")):
            relative = path.relative_to(REPOSITORY).as_posix()
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if _DECODE.search(line):
                    yield relative, number, line.strip()


def test_noOtherStoredTableDecodeRemains():
    for spelling in ("pd.read_parquet(io.BytesIO(readTableBytes(p, t)))", "pq.read_table(io.BytesIO(x))", "pq.ParquetFile(file_like)",
                     "pl.scan_parquet(url)", "pl.read_parquet(buffer)", "pf.iter_batches(batch_size=1)", "pq.read_metadata(f)",
                     "import pyarrow.dataset as ds", "from pyarrow.dataset import dataset", "pyarrow.dataset.dataset(path)",
                     "table = ds.dataset(source, format='parquet').to_table()", "reader = ParquetReader()"):
        assert _DECODE.search(spelling), spelling
    assert not _DECODE.search("pq.write_table(table, sink)")
    assert not _DECODE.search("fields = ds.field('a')")
    offenders = []
    for relative, number, line in _decodeLines():
        if relative in _CHILD_SIDE:
            continue
        if relative in _DECODERS and (_DECODERS[relative] is None or line in _DECODERS[relative]):
            continue
        offenders.append(f"{relative}:{number}: {line}")
    assert offenders == []
