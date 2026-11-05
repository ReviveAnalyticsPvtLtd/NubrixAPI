"""Stored-table marker, bounded footer read and the API's writer."""

import io

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from utils import storedTables
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
def warnings(monkeypatch):
    recorder = _Warnings()
    monkeypatch.setattr(storedTables, "logger", recorder)
    return recorder


def test_missingKeyMarksNothingAndNeverRaises(monkeypatch, warnings):
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


def test_missingKeyWarnsOnce(monkeypatch, warnings):
    monkeypatch.delenv("SUPABASE_KEY")
    storedTables._missingKeyWarned = False
    for _ in range(5):
        tableMarker("p", "t")
    assert len([m for m in warnings.messages if "SUPABASE_KEY" in m]) == 1
