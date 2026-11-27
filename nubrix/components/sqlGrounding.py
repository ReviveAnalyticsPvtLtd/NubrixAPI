"""
sqlGrounding.py

The table text the SQL chart prompts are grounded on. For each active table it
lists the name as a double-quoted identifier, each column with the exact type
DuckDB will see (read from the stored Arrow schema, not from metadata.json's
upload dtypes), the metadata description and the metadata sample value, then
join hints: columns with the same name and type in two or more tables.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["SAMPLE_VALUE_CAP", "duckdbTypeName", "groundingText", "quoteIdentifier"]

import json
import re

import pyarrow as pa

# Longest sample value shown, in characters. A sample shows the model a value's
# format (casing, date layout, code style); 100 characters is enough for that,
# while long free text or nested values would cost tokens on every prompt.
SAMPLE_VALUE_CAP = 100

_SIMPLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WHITESPACE = re.compile(r"\s+")

_INTEGER_NAMES = {
    (True, 8): "TINYINT",
    (True, 16): "SMALLINT",
    (True, 32): "INTEGER",
    (True, 64): "BIGINT",
    (False, 8): "UTINYINT",
    (False, 16): "USMALLINT",
    (False, 32): "UINTEGER",
    (False, 64): "UBIGINT",
}
_TIMESTAMP_NAMES = {"s": "TIMESTAMP_S", "ms": "TIMESTAMP_MS", "us": "TIMESTAMP", "ns": "TIMESTAMP_NS"}
_TEXT_TYPES = ("VARCHAR",)


def quoteIdentifier(name: str) -> str:
    """Returns `name` as a DuckDB double-quoted identifier, embedded quotes doubled."""
    return '"' + str(name).replace('"', '""') + '"'


def _fieldName(name: str) -> str:
    return name if _SIMPLE_NAME.fullmatch(name) else quoteIdentifier(name)


def duckdbTypeName(dataType: pa.DataType) -> str:
    """
    Returns the type name DuckDB gives an Arrow column it scans.

    int64 is BIGINT, float64 DOUBLE, string VARCHAR, timestamp[us] TIMESTAMP
    (other units TIMESTAMP_S / _MS / _NS, with a time zone TIMESTAMP WITH TIME
    ZONE), date32 DATE, bool BOOLEAN, decimal DECIMAL(p,s); a dictionary column
    is its value type. Anything not listed is `str(type).upper()`.
    """
    if pa.types.is_dictionary(dataType):
        return duckdbTypeName(dataType.value_type)
    if pa.types.is_integer(dataType):
        return _INTEGER_NAMES[(pa.types.is_signed_integer(dataType), dataType.bit_width)]
    if pa.types.is_float16(dataType) or pa.types.is_float32(dataType):
        return "FLOAT"
    if pa.types.is_float64(dataType):
        return "DOUBLE"
    if pa.types.is_string(dataType) or pa.types.is_large_string(dataType) or pa.types.is_string_view(dataType):
        return "VARCHAR"
    if pa.types.is_boolean(dataType):
        return "BOOLEAN"
    if pa.types.is_date(dataType):
        return "DATE"
    if pa.types.is_timestamp(dataType):
        if dataType.tz:
            return "TIMESTAMP WITH TIME ZONE"
        return _TIMESTAMP_NAMES[dataType.unit]
    if pa.types.is_decimal(dataType):
        return f"DECIMAL({dataType.precision},{dataType.scale})"
    if pa.types.is_time(dataType):
        return "TIME"
    if pa.types.is_duration(dataType):
        return "INTERVAL"
    if pa.types.is_binary(dataType) or pa.types.is_large_binary(dataType) or pa.types.is_fixed_size_binary(dataType):
        return "BLOB"
    if pa.types.is_list(dataType) or pa.types.is_large_list(dataType):
        return f"{duckdbTypeName(dataType.value_type)}[]"
    if pa.types.is_fixed_size_list(dataType):
        return f"{duckdbTypeName(dataType.value_type)}[{dataType.list_size}]"
    if pa.types.is_struct(dataType):
        fields = ", ".join(f"{_fieldName(field.name)} {duckdbTypeName(field.type)}" for field in dataType)
        return f"STRUCT({fields})"
    if pa.types.is_map(dataType):
        return f"MAP({duckdbTypeName(dataType.key_type)}, {duckdbTypeName(dataType.item_type)})"
    return str(dataType).upper()


def _oneLine(text: object) -> str:
    return _WHITESPACE.sub(" ", str(text)).strip()


def _sampleText(value: object, typeName: str) -> str:
    """
    A sample value as one line. Text columns show it as a SQL string literal
    (quotes doubled), so the model sees how to write that value; other values
    are shown as given. Values over SAMPLE_VALUE_CAP characters are cut.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    text = _oneLine(text)
    shortened = len(text) > SAMPLE_VALUE_CAP
    if shortened:
        text = text[:SAMPLE_VALUE_CAP] + "..."
    if typeName in _TEXT_TYPES or typeName == "UNKNOWN" and isinstance(value, str):
        text = "'" + text.replace("'", "''") + "'"
    return text + " (shortened)" if shortened else text


def _isActive(entry: object) -> bool:
    return not (isinstance(entry, dict) and entry.get("isActive") is False)


def _tableColumns(entry: dict, schema: pa.Schema | None) -> list[tuple[str, str]]:
    """(column, DuckDB type) in schema order; from metadata with an unknown type when no schema was read."""
    if schema is not None:
        return [(field.name, duckdbTypeName(field.type)) for field in schema]
    columns = entry.get("columns") if isinstance(entry.get("columns"), list) else []
    return [(str(column["name"]), "UNKNOWN") for column in columns if isinstance(column, dict) and column.get("name")]


def groundingText(metadata: dict, schemas: dict[str, pa.Schema]) -> str:
    """
    Describes the project's active tables for the SQL chart prompts.

    Args:
        metadata (dict): The project's metadata.json content. Only tables listed
            here are described: one whose `isActive` is false is left out, and a
            missing key means active.
        schemas (dict[str, pa.Schema]): The stored Arrow schema of each table.
            Used for the types only: a schema with no metadata entry is ignored,
            and a listed table with no schema gets type UNKNOWN.

    Returns:
        str: One block per table, then a join hints block.
    """
    metadata = metadata if isinstance(metadata, dict) else {}
    names = [name for name, entry in metadata.items() if _isActive(entry)]

    blocks: list[str] = []
    shared: dict[tuple[str, str], list[str]] = {}
    for name in names:
        entry = metadata.get(name) if isinstance(metadata.get(name), dict) else {}
        descriptions = {
            str(column.get("name")): column.get("description")
            for column in entry.get("columns") or []
            if isinstance(column, dict)
        }
        samples = entry.get("sample_row") if isinstance(entry.get("sample_row"), dict) else {}
        description = _oneLine(entry.get("description") or "")
        lines = [f"Table {quoteIdentifier(name)}" + (f": {description}" if description else "")]
        lines.append("Columns (name, DuckDB type, description, sample value):")
        for column, typeName in _tableColumns(entry, schemas.get(name)):
            line = f"- {quoteIdentifier(column)} {typeName}"
            columnDescription = _oneLine(descriptions.get(column) or "")
            if columnDescription:
                line += f": {columnDescription}"
            if column in samples:
                line += f"{'' if columnDescription.endswith('.') else '.'} Sample: {_sampleText(samples[column], typeName)}"
            lines.append(line)
            if typeName != "UNKNOWN":
                shared.setdefault((column, typeName), []).append(name)
        blocks.append("\n".join(lines))

    if not blocks:
        return "No active tables."

    hints = [
        f"- {quoteIdentifier(column)} {typeName} in " + ", ".join(quoteIdentifier(table) for table in tables)
        for (column, typeName), tables in shared.items()
        if len(tables) > 1
    ]
    if hints:
        blocks.append(
            "Join hints (the same column name and type in more than one table; "
            "use them only when the question needs columns from both tables, and check they mean the same thing):\n"
            + "\n".join(hints)
        )
    else:
        blocks.append("Join hints: none (no column name and type appears in more than one table).")
    return "\n\n".join(blocks)
