"""The table text the SQL chart prompts are grounded on: exact DuckDB types, descriptions, samples, join hints."""

import datetime
import decimal

import duckdb
import pyarrow as pa
import pytest

from nubrix.components.sqlGrounding import SAMPLE_VALUE_CAP, duckdbTypeName, groundingText, quoteIdentifier

ORDERS = pa.schema(
    [
        ("order_id", pa.int64()),
        ("customer_id", pa.int64()),
        ("placed_at", pa.timestamp("us")),
        ("amount", pa.float64()),
        ("status", pa.string()),
    ]
)
CUSTOMERS = pa.schema(
    [
        ("customer_id", pa.int64()),
        ("full_name", pa.string()),
        ("joined_on", pa.date32()),
        ("status", pa.int32()),
    ]
)
METADATA = {
    "orders": {
        "description": "One row per order.",
        "columns": [
            {"name": "order_id", "type": "int64", "description": "Order number."},
            {"name": "customer_id", "type": "int64", "description": "Who placed the order."},
            {"name": "placed_at", "type": "datetime64[ns]", "description": "When the order was placed."},
            {"name": "amount", "type": "float64", "description": "Order value."},
            {"name": "status", "type": "object", "description": "Fulfilment status."},
        ],
        "sample_row": {"order_id": 7, "customer_id": 3, "placed_at": "2024-02-01 10:00:00", "amount": 12.5, "status": "Shipped"},
    },
    "customers": {
        "description": "One row per customer.",
        "columns": [
            {"name": "customer_id", "type": "int64", "description": "Customer number."},
            {"name": "full_name", "type": "object", "description": "Customer name."},
        ],
        "sample_row": {"customer_id": 3, "full_name": "Ann O'Neil", "joined_on": "2023-05-04", "status": 1},
    },
}


def test_groundingListsTypesDescriptionsAndJoinHints():
    text = groundingText(METADATA, {"orders": ORDERS, "customers": CUSTOMERS})

    assert 'Table "orders": One row per order.' in text
    assert 'Table "customers": One row per customer.' in text
    assert '- "order_id" BIGINT: Order number. Sample: 7' in text
    assert '- "placed_at" TIMESTAMP: When the order was placed. Sample: 2024-02-01 10:00:00' in text
    assert '- "amount" DOUBLE: Order value. Sample: 12.5' in text
    assert "- \"status\" VARCHAR: Fulfilment status. Sample: 'Shipped'" in text
    # Text samples are shown as SQL literals, quotes doubled.
    assert "- \"full_name\" VARCHAR: Customer name. Sample: 'Ann O''Neil'" in text
    # Types come from the stored schema, not the metadata's dtypes; a column with no description still shows.
    assert '- "joined_on" DATE. Sample: 2023-05-04' in text
    assert "datetime64" not in text and "object" not in text

    hints = text.split("Join hints")[1]
    assert '- "customer_id" BIGINT in "orders", "customers"' in hints
    # Same name, different type: no hint.
    assert '"status"' not in hints
    assert '"order_id"' not in hints


def test_groundingQuotesIdentifiers():
    schema = pa.schema([("Order Date", pa.date32()), ('say "hi"', pa.string())])
    metadata = {'Sales "2024" Export': {"description": "d", "columns": [], "sample_row": {}}}

    text = groundingText(metadata, {'Sales "2024" Export': schema})

    assert 'Table "Sales ""2024"" Export"' in text
    assert '- "Order Date" DATE' in text
    assert '- "say ""hi""" VARCHAR' in text
    assert quoteIdentifier('a"b') == '"a""b"'
    assert quoteIdentifier("plain") == '"plain"'


def test_groundingSkipsInactiveTablesAndKeepsSchemaOrder():
    metadata = {
        "orders": {**METADATA["orders"], "isActive": False},
        "customers": METADATA["customers"],
    }

    text = groundingText(metadata, {"orders": ORDERS, "customers": CUSTOMERS})

    assert '"orders"' not in text
    assert text.index('"customer_id"') < text.index('"full_name"') < text.index('"joined_on"') < text.index('"status"')
    assert "Join hints: none" in text


def test_groundingListsOnlyTablesTheMetadataMarksActive():
    # A stored schema without a metadata entry is a table the project does not list, so it stays out.
    text = groundingText({"customers": METADATA["customers"]}, {"orders": ORDERS, "customers": CUSTOMERS})

    assert 'Table "customers"' in text
    assert '"orders"' not in text and '"placed_at"' not in text
    assert "Join hints: none" in text
    assert groundingText({}, {"orders": ORDERS}) == "No active tables."


def test_groundingCapsLongSampleValues():
    schema = pa.schema([("notes", pa.string()), ("tags", pa.string())])
    long = "x" * (SAMPLE_VALUE_CAP + 50)
    metadata = {"t": {"description": "d\nmore", "columns": [], "sample_row": {"notes": long, "tags": "a\nb"}}}

    text = groundingText(metadata, {"t": schema})

    assert "'" + "x" * SAMPLE_VALUE_CAP + "...' (shortened)" in text
    assert "x" * (SAMPLE_VALUE_CAP + 1) not in text
    assert "Sample: 'a b'" in text
    assert 'Table "t": d more' in text


@pytest.mark.parametrize(
    "dataType",
    [
        pa.int8(),
        pa.int16(),
        pa.int32(),
        pa.int64(),
        pa.uint8(),
        pa.uint64(),
        pa.float32(),
        pa.float64(),
        pa.string(),
        pa.large_string(),
        pa.bool_(),
        pa.date32(),
        pa.date64(),
        pa.timestamp("s"),
        pa.timestamp("ms"),
        pa.timestamp("us"),
        pa.timestamp("ns"),
        pa.timestamp("us", tz="UTC"),
        pa.decimal128(12, 2),
        pa.binary(),
        pa.time64("us"),
        pa.list_(pa.int64()),
        pa.struct([("a", pa.int64())]),
        pa.dictionary(pa.int32(), pa.string()),
    ],
    ids=str,
)
def test_duckdbTypeNameMatchesDuckDb(dataType):
    values = {
        pa.types.is_integer: [1],
        pa.types.is_floating: [1.0],
        pa.types.is_string: ["a"],
        pa.types.is_large_string: ["a"],
        pa.types.is_boolean: [True],
        pa.types.is_date: [datetime.date(2024, 1, 1)],
        pa.types.is_timestamp: [datetime.datetime(2024, 1, 1)],
        pa.types.is_decimal: [decimal.Decimal("1.25")],
        pa.types.is_binary: [b"x"],
        pa.types.is_time: [datetime.time(1, 2)],
        pa.types.is_list: [[1]],
        pa.types.is_struct: [{"a": 1}],
    }
    if pa.types.is_dictionary(dataType):
        array = pa.array(["a"]).dictionary_encode()
    else:
        array = pa.array(next(v for check, v in values.items() if check(dataType)), dataType)
    table = pa.table({"c": array})
    connection = duckdb.connect()
    connection.register("t", table)
    expected = connection.execute("DESCRIBE SELECT * FROM t").fetchall()[0][1]

    assert duckdbTypeName(dataType) == expected
    assert duckdbTypeName(pa.int64()) == "BIGINT"
    assert duckdbTypeName(pa.decimal128(12, 2)) == "DECIMAL(12,2)"
