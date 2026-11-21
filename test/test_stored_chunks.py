"""writeStoredChunksToPath: many DataFrames become one marked stored table."""

import os

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from utils.storedTables import TableTooLargeError, markerIsValid, writeStoredChunksToPath


@pytest.fixture(autouse=True)
def _cleanEnv(monkeypatch):
    monkeypatch.setenv("SUPABASE_KEY", "unit-test-key")
    monkeypatch.delenv("STORED_TABLE_MAX_DECODED_BYTES", raising=False)


def _write(tmp_path, chunks):
    path = str(tmp_path / "out.parquet")
    result = writeStoredChunksToPath(iter(chunks), "p1", "t1", path)
    return path, result


def test_chunksBecomeOneMarkedTable(tmp_path):
    frames = [pd.DataFrame({"a": [i, i + 1], "b": ["x", "y"]}) for i in (0, 2, 4)]
    path, result = _write(tmp_path, frames)
    assert result == (6, 2)
    assert pq.read_table(path).num_rows == 6
    assert markerIsValid(pq.read_metadata(path), "p1", "t1")
    assert not markerIsValid(pq.read_metadata(path), "p1", "other")


def test_nullOnlyFirstChunkTakesTheLaterType(tmp_path):
    path, _ = _write(tmp_path, [pd.DataFrame({"a": [None, None]}), pd.DataFrame({"a": ["u", "v"]})])
    table = pq.read_table(path)
    assert table.schema.field("a").type == pa.string()
    assert table.column("a").to_pylist() == [None, None, "u", "v"]


def test_intColumnThatGainsNullsLater(tmp_path):
    path, _ = _write(tmp_path, [pd.DataFrame({"a": [1, 2]}), pd.DataFrame({"a": [3.0, None]})])
    assert pq.read_table(path).column("a").to_pylist() == [1, 2, 3, None]


def test_conflictingTypesFallBackToString(tmp_path):
    path, _ = _write(tmp_path, [pd.DataFrame({"a": [1, 2]}), pd.DataFrame({"a": ["x", "y"]})])
    table = pq.read_table(path)
    assert table.schema.field("a").type == pa.string()
    assert table.column("a").to_pylist() == ["1", "2", "x", "y"]


def test_missingColumnsInALaterChunkAreNull(tmp_path):
    path, result = _write(tmp_path, [pd.DataFrame({"a": [1], "b": ["x"]}), pd.DataFrame({"a": [2], "c": [9]})])
    table = pq.read_table(path)
    assert result == (2, 3)
    assert table.column("a").to_pylist() == [1, 2]
    assert table.column("b").to_pylist() == ["x", None]
    assert table.column("c").to_pylist() == [None, 9]


def test_overTheLimitRaisesAndLeavesNoFile(tmp_path, monkeypatch):
    monkeypatch.setenv("STORED_TABLE_MAX_DECODED_BYTES", "1000")
    path = str(tmp_path / "out.parquet")
    with pytest.raises(TableTooLargeError):
        writeStoredChunksToPath(iter([pd.DataFrame({"a": range(500)})] * 3), "p1", "t1", path)
    assert not os.path.exists(path)
    assert os.listdir(tmp_path) == []


def test_noChunksWritesAnEmptyTable(tmp_path):
    path, result = _write(tmp_path, [])
    assert result == (0, 0)
    assert pq.read_table(path).num_rows == 0
