import datetime
import decimal
import zoneinfo

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


def test_textOperatorsMatchNumberColumnsByTheirText():
    t = _people()
    assert _names(applyFilters(t, "t", [{"qty": {"contains": "1"}}])) == ["Alice"]
    assert _names(applyFilters(t, "t", [{"qty": {"startswith": "1", "endswith": "1"}}])) == ["Alice"]


def _typed():
    return pa.table(
        {
            "id": pa.array([1, 2, 3, 4], type=pa.int64()),
            "d": pa.array(
                [datetime.date(2024, 1, 5), datetime.date(2024, 2, 5), datetime.date(2023, 1, 20), None], type=pa.date32()
            ),
            "ts": pa.array(
                [
                    datetime.datetime(2024, 1, 15, 8, 30),
                    datetime.datetime(2024, 1, 16, 8, 30),
                    datetime.datetime(2023, 1, 15, 0, 0),
                    datetime.datetime(2024, 1, 15, 23, 59),
                ],
                type=pa.timestamp("us"),
            ),
            "dec": pa.array(
                [decimal.Decimal("12.50"), decimal.Decimal("112.00"), decimal.Decimal("120.75"), decimal.Decimal("3.00")],
                type=pa.decimal128(10, 2),
            ),
            "num": pa.array([15, 25, 30, 105], type=pa.int64()),
            "lst": pa.array([[1], [2], [3], [4]], type=pa.list_(pa.int64())),
            "st": pa.array([{"a": 1}, {"a": 2}, {"a": 3}, {"a": 4}], type=pa.struct([("a", pa.int64())])),
        }
    )


def _ids(table):
    return table.column("id").to_pylist()


def test_textOperatorsMatchDateColumnsByTheirIsoText():
    t = _typed()
    assert _ids(applyFilters(t, "t", [{"d": {"contains": "2024-01"}}])) == [1]
    assert _ids(applyFilters(t, "t", [{"d": {"startswith": "2024"}}])) == [1, 2]
    assert _ids(applyFilters(t, "t", [{"d": {"endswith": "-05"}}])) == [1, 2]


def test_textOperatorsMatchTimestampColumnsByTheirText():
    t = _typed()
    assert _ids(applyFilters(t, "t", [{"ts": {"startswith": "2024-01-15"}}])) == [1, 4]


def test_textOperatorsMatchDecimalColumnsByTheirText():
    t = _typed()
    assert _ids(applyFilters(t, "t", [{"dec": {"startswith": "12"}}])) == [1, 3]


def test_textOperatorsMatchIntegerColumnsByTheirText():
    t = _typed()
    assert _ids(applyFilters(t, "t", [{"num": {"endswith": "5"}}])) == [1, 2, 4]


def test_textOperatorsOnUncastableColumnsMatchNothing():
    t = _typed()
    assert applyFilters(t, "t", [{"lst": {"contains": "1"}}]).num_rows == 0
    assert applyFilters(t, "t", [{"st": {"startswith": "1"}}]).num_rows == 0


def test_textOperatorsOnNonStringColumnsWorkInTheDatasetPath(tmp_path):
    t = _typed().drop_columns(["lst", "st"])
    path = tmp_path / "t.parquet"
    pq.write_table(t, path)
    dataset = ds.dataset(path)
    for flt in (
        [{"d": {"contains": "2024-01"}}],
        [{"ts": {"startswith": "2024-01-15"}}],
        [{"dec": {"startswith": "12"}}],
        [{"num": {"endswith": "5"}}],
    ):
        assert filterDataset(dataset, "t", flt).to_table().equals(applyFilters(t, "t", flt))
    assert _ids(filterDataset(dataset, "t", [{"d": {"contains": "2024-01"}}]).to_table()) == [1]
    assert _ids(filterDataset(dataset, "t", [{"dec": {"startswith": "12"}}]).to_table()) == [1, 3]


def test_textOperatorsOnUncastableColumnsMatchNothingInTheDatasetPath(tmp_path):
    path = tmp_path / "t.parquet"
    pq.write_table(_typed(), path)
    dataset = ds.dataset(path)
    assert filterDataset(dataset, "t", [{"lst": {"contains": "1"}}]).to_table().num_rows == 0


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
            # Text without an offset is read in the column's own zone, so these are Kolkata wall times.
            values = [s.replace(tzinfo=zoneinfo.ZoneInfo("Asia/Kolkata")) for s in stamps]
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


def test_naiveTextOnTimezoneColumnIsReadInTheColumnsOwnZone():
    kolkata = zoneinfo.ZoneInfo("Asia/Kolkata")
    moments = [
        datetime.datetime(2017, 1, 1, 4, 59, tzinfo=kolkata),
        datetime.datetime(2017, 1, 1, 5, 0, tzinfo=kolkata),
        datetime.datetime(2017, 1, 1, 5, 1, tzinfo=kolkata),
    ]
    t = pa.table({"at": pa.array(moments, type=pa.timestamp("us", tz="Asia/Kolkata")), "i": [0, 1, 2]})
    assert applyFilters(t, "t", [{"at": "2017-01-01 05:00:00"}]).column("i").to_pylist() == [1]
    assert applyFilters(t, "t", [{"at": {"min": "2017-01-01 05:00:00"}}]).column("i").to_pylist() == [1, 2]
    # Text with an explicit offset or Z keeps it: 05:00 Kolkata is 23:30 UTC the day before.
    assert applyFilters(t, "t", [{"at": "2016-12-31T23:30:00Z"}]).column("i").to_pylist() == [1]
    assert applyFilters(t, "t", [{"at": "2017-01-01 05:00:00+05:30"}]).column("i").to_pylist() == [1]


def test_oneOfListIsOrderIndependentWithMixedMemberTypes():
    t = pa.table({"q": pa.array([1, 2, 3], type=pa.int64())})
    for values in ([2, 1.5], [1.5, 2], ["2", "1.5"], ["1.5", "2"], [2, "1.5", "x"], ["x", "1.5", 2]):
        assert applyFilters(t, "t", [{"q": values}]).column("q").to_pylist() == [2], values
    assert applyFilters(t, "t", [{"q": [3.0, 1]}]).column("q").to_pylist() == [1, 3]
    assert applyFilters(t, "t", [{"q": [1.5]}]).num_rows == 0
    dec = pa.table({"c": pa.array([decimal.Decimal("1.50"), decimal.Decimal("2.00")], type=pa.decimal128(10, 2))})
    for values in (["2", "9.999"], ["9.999", "2"], [2, "x"]):
        assert applyFilters(dec, "t", [{"c": values}]).column("c").to_pylist() == [decimal.Decimal("2.00")], values
    stamps = pa.table({"at": pa.array([datetime.datetime(2017, 1, 1), datetime.datetime(2017, 1, 2)], type=pa.timestamp("us"))})
    for values in (["2017-01-02", "junk"], ["junk", "2017-01-02 00:00:00"], [datetime.datetime(2017, 1, 2), "2017-01-02"]):
        assert applyFilters(stamps, "t", [{"at": values}]).num_rows == 1, values


def test_stringViewColumnsAreFilteredAsText():
    t = pa.table({"s": pa.array(["Alpha", "beta", "alpine"], type=pa.string_view())})
    assert applyFilters(t, "t", [{"s": "beta"}]).num_rows == 1
    assert applyFilters(t, "t", [{"s": ["beta", "Alpha"]}]).num_rows == 2
    assert applyFilters(t, "t", [{"s": {"contains": "ALP"}}]).num_rows == 2
    assert applyFilters(t, "t", [{"s": {"startswith": "al"}}]).num_rows == 1
    assert applyFilters(t, "t", [{"s": {"min": "b"}}]).num_rows == 1


def test_textFilterMatchesNoRowsWhenTheColumnsTextFormCannotBeBuilt(monkeypatch, tmp_path):
    """A tz-aware timestamp needs a time zone database to become text; without one the condition matches nothing."""
    table = pa.table(
        {
            "id": pa.array([1, 2], type=pa.int64()),
            "at": pa.array([datetime.datetime(2024, 1, 15, 6), datetime.datetime(2023, 1, 15, 6)], type=pa.timestamp("us", tz="UTC")),
        }
    )
    path = tmp_path / "t.parquet"
    pq.write_table(table, path)
    realArray = pa.array

    def noTimeZoneDatabase(values, type=None, *args, **kwargs):
        if type is not None and pa.types.is_timestamp(type) and type.tz is not None and len(values):
            raise pa.ArrowInvalid("Cannot locate timezone 'UTC': Timezone database not found")
        return realArray(values, type, *args, **kwargs)

    monkeypatch.setattr(pa, "array", noTimeZoneDatabase)

    flt = [{"at": {"startswith": "2024"}}]
    assert applyFilters(table, "t", flt).num_rows == 0
    assert filterDataset(ds.dataset(path), "t", flt).to_table().num_rows == 0
