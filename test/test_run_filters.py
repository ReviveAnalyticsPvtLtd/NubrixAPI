import datetime
import decimal

import duckdb
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from utils.runFilters import applyFilters, filterDataset, filterExpression


def _people():
    return pa.table(
        {
            "name": ["Alice", "bob", "Carol", "alan", None],
            "qty": pa.array([1, 2, 3, 4, None], type=pa.int64()),
            "price": [1.5, 2.5, 3.5, 4.5, 5.5],
        }
    )


def _names(table):
    return table.column("name").to_pylist()


def test_equalsListMinMax():
    t = _people()
    assert _names(applyFilters(t, "t", [{"qty": 2}])) == ["bob"]
    assert _names(applyFilters(t, "t", [{"qty": [1, 3]}])) == ["Alice", "Carol"]
    assert _names(applyFilters(t, "t", [{"qty": {"min": 3}}])) == ["Carol", "alan"]
    assert _names(applyFilters(t, "t", [{"qty": {"max": 2}}])) == ["Alice", "bob"]
    assert applyFilters(t, "t", [{"qty": []}]).num_rows == 5


def test_minAndMaxTogetherApplyBoth():
    assert _names(applyFilters(_people(), "t", [{"qty": {"min": 2, "max": 3}}])) == ["bob", "Carol"]


def test_containsIgnoresCaseStartsAndEndsWithMatchCase():
    t = _people()
    assert _names(applyFilters(t, "t", [{"name": {"contains": "AL"}}])) == ["Alice", "alan"]
    assert _names(applyFilters(t, "t", [{"name": {"startswith": "al"}}])) == ["alan"]
    assert _names(applyFilters(t, "t", [{"name": {"startswith": "AL"}}])) == []
    assert _names(applyFilters(t, "t", [{"name": {"endswith": "ol"}}])) == ["Carol"]
    assert _names(applyFilters(t, "t", [{"name": {"endswith": "OL"}}])) == []


def test_textOperatorsIgnoredOnNumberColumns():
    t = _people()
    assert applyFilters(t, "t", [{"qty": {"contains": "1"}}]).num_rows == 5
    assert applyFilters(t, "t", [{"qty": {"startswith": "1", "endswith": "1"}}]).num_rows == 5


def test_tableScopedKeyOnlyAppliesToItsTable():
    t = _people()
    assert _names(applyFilters(t, "people", [{"people.qty": 1}])) == ["Alice"]
    assert applyFilters(t, "orders", [{"people.qty": 1}]).num_rows == 5
    assert filterExpression("orders", t.schema, [{"people.qty": 1}]) is None


def test_missingColumnIsSkipped():
    t = _people()
    assert applyFilters(t, "t", [{"nope": 1}, "junk", 7, {"qty": 1}]).num_rows == 1
    assert applyFilters(t, "t", [{"nope": 1}]) is t
    assert applyFilters(t, "t", None) is t


def test_nullValuesNeverMatchEquals():
    t = _people()
    assert _names(applyFilters(t, "t", [{"qty": 1}])) == ["Alice"]
    assert applyFilters(t, "t", [{"qty": None}]).num_rows == 0
    assert applyFilters(t, "t", [{"qty": [None]}]).num_rows == 0
    assert applyFilters(t, "t", [{"qty": {"min": 0}}]).num_rows == 4


def test_textDateBoundsFilterTimestampColumns():
    stamps = [
        datetime.datetime(2016, 12, 31, 23, 59, 59),
        datetime.datetime(2017, 1, 1),
        datetime.datetime(2017, 3, 5, 12),
        datetime.datetime(2017, 6, 30, 23, 59, 59),
        datetime.datetime(2017, 7, 1),
    ]
    flt = [{"at": {"min": "2017-01-01 00:00:00", "max": "2017-06-30 23:59:59"}}]
    ids = [0, 1, 2, 3, 4]
    for unit, tz in (("us", None), ("us", "UTC"), ("ns", None), ("ms", "Asia/Kolkata")):
        if tz == "Asia/Kolkata":
            # Text without an offset is read as UTC for a timezone-aware column.
            values = [s.replace(tzinfo=datetime.timezone.utc) for s in stamps]
        else:
            values = stamps if tz is None else [s.replace(tzinfo=datetime.timezone.utc) for s in stamps]
        t = pa.table({"at": pa.array(values, type=pa.timestamp(unit, tz=tz)), "i": ids})
        assert applyFilters(t, "t", flt).column("i").to_pylist() == [1, 2, 3], (unit, tz)
    t = pa.table({"at": pa.array(stamps, type=pa.timestamp("us")), "i": ids})
    # Date columns compare as midnight timestamps.
    days = pa.table({"at": pa.array([s.date() for s in stamps], type=pa.date32()), "i": ids})
    assert applyFilters(days, "t", flt).column("i").to_pylist() == [1, 2, 3]
    # An offset in the text is honoured.
    tz = pa.table({"at": pa.array(stamps, type=pa.timestamp("us", tz="UTC")), "i": ids})
    assert applyFilters(tz, "t", [{"at": {"min": "2017-01-01T05:30:00+05:30"}}]).column("i").to_pylist() == [1, 2, 3, 4]
    # Equals and one-of on a timestamp column.
    assert applyFilters(t, "t", [{"at": "2017-01-01 00:00:00"}]).column("i").to_pylist() == [1]
    assert applyFilters(t, "t", [{"at": ["2017-01-01", "2017-07-01 00:00:00"]}]).column("i").to_pylist() == [1, 4]


def test_textValuesFilterBoolAndNumberColumns():
    t = pa.table(
        {
            "flag": [True, False, True, None],
            "qty": pa.array([1, 2, 3, 4], type=pa.int64()),
            "ratio": [0.5, 1.5, 2.5, 3.5],
            "cost": pa.array(
                [decimal.Decimal("1.50"), decimal.Decimal("2.50"), None, decimal.Decimal("9.99")],
                type=pa.decimal128(10, 2),
            ),
        }
    )
    assert applyFilters(t, "t", [{"flag": "True"}]).column("qty").to_pylist() == [1, 3]
    assert applyFilters(t, "t", [{"flag": "false"}]).column("qty").to_pylist() == [2]
    assert applyFilters(t, "t", [{"qty": ["1", "2"]}]).column("qty").to_pylist() == [1, 2]
    assert applyFilters(t, "t", [{"qty": {"min": "2", "max": "3"}}]).column("qty").to_pylist() == [2, 3]
    assert applyFilters(t, "t", [{"qty": {"min": "1.5"}}]).column("qty").to_pylist() == [2, 3, 4]
    assert applyFilters(t, "t", [{"ratio": "1.5"}]).column("qty").to_pylist() == [2]
    assert applyFilters(t, "t", [{"cost": {"min": "2"}}]).column("qty").to_pylist() == [2, 4]
    assert applyFilters(t, "t", [{"cost": 1.5}]).column("qty").to_pylist() == [1]


def test_uncastableValueMatchesNoRows():
    t = pa.table(
        {
            "qty": pa.array([1, 2], type=pa.int64()),
            "flag": [True, False],
            "at": pa.array([datetime.datetime(2017, 1, 1)] * 2),
        }
    )
    for column, bad in (("qty", "abc"), ("flag", "maybe"), ("at", "not a date")):
        assert applyFilters(t, "t", [{column: bad}]).num_rows == 0
        assert applyFilters(t, "t", [{column: [bad]}]).num_rows == 0
        assert applyFilters(t, "t", [{column: {"min": bad}}]).num_rows == 0
        assert applyFilters(t, "t", [{column: {"max": bad}}]).num_rows == 0
    # One bad member of a list does not hide the good ones.
    assert applyFilters(t, "t", [{"qty": ["abc", "2"]}]).column("qty").to_pylist() == [2]
    # The cast failure is found when the expression is built, so the expression still exists.
    assert filterExpression("t", t.schema, [{"qty": "abc"}]) is not None


def test_stringColumnsCompareAsText():
    for stringType in (pa.string(), pa.large_string()):
        t = pa.table({"d": pa.array(["2017-01-01", "2017-02-01", "2017-03-01"], type=stringType)})
        assert applyFilters(t, "t", [{"d": {"min": "2017-02-01", "max": "2017-02-28"}}]).num_rows == 1
        assert applyFilters(t, "t", [{"d": {"contains": "-02-"}}]).num_rows == 1
        assert applyFilters(t, "t", [{"d": 2017}]).num_rows == 0
    numbers = pa.table({"d": pa.array(["1", "2"], type=pa.string())})
    assert applyFilters(numbers, "t", [{"d": 2}]).num_rows == 1


def test_dictionaryEncodedStringsAreComparedByValue():
    t = pa.table({"c": pa.array(["a", "b", "a", "B"]).dictionary_encode()})
    assert applyFilters(t, "t", [{"c": "a"}]).num_rows == 2
    assert applyFilters(t, "t", [{"c": ["a", "b"]}]).num_rows == 3
    assert applyFilters(t, "t", [{"c": {"contains": "B"}}]).num_rows == 2


def test_conditionsCombineWithAnd():
    flt = [{"qty": {"min": 2}}, {"name": {"startswith": "a"}}]
    assert _names(applyFilters(_people(), "t", flt)) == ["alan"]


def test_filterDatasetMatchesApplyFilters(tmp_path):
    t = pa.table(
        {
            "name": ["Alice", "bob", "Carol", "alan"],
            "qty": pa.array([1, 2, 3, 4], type=pa.int64()),
            "at": pa.array([datetime.datetime(2017, m, 1) for m in (1, 3, 5, 8)], type=pa.timestamp("us")),
            "flag": [True, False, True, False],
        }
    )
    path = tmp_path / "t.parquet"
    pq.write_table(t, path)
    filters = [
        {"qty": {"min": "2"}},
        {"at": {"max": "2017-06-30 23:59:59"}},
        {"name": {"contains": "A"}},
        {"t.flag": "True"},
        {"other.qty": 99},
    ]
    dataset = ds.dataset(path)
    filtered = filterDataset(dataset, "t", filters)
    assert isinstance(filtered, ds.Dataset)
    expected = applyFilters(t, "t", filters)
    assert expected.num_rows == 1
    assert filtered.to_table().equals(expected)
    assert filterDataset(dataset, "t", [{"zzz": 1}]) is dataset
    assert filterDataset(dataset, "t", [{"qty": "abc"}]).to_table().num_rows == 0


def test_duckdbSeesOnlyFilteredRows(tmp_path):
    t = pa.table({"k": pa.array(list(range(10)), type=pa.int64()), "v": [float(i) for i in range(10)]})
    path = tmp_path / "t.parquet"
    pq.write_table(t, path)
    filtered = filterDataset(ds.dataset(path), "t", [{"k": {"min": "3", "max": "6"}}])
    con = duckdb.connect()
    con.register("t", filtered)
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")
    assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 4
    assert con.execute("SELECT count(*) FROM t WHERE k >= 5").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM t a JOIN t b ON a.k = b.k").fetchone()[0] == 4
    assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 4
    nothing = filterDataset(ds.dataset(path), "t", [{"k": "abc"}])
    con.register("n", nothing)
    assert con.execute("SELECT count(*) FROM n").fetchone()[0] == 0
    con.close()
