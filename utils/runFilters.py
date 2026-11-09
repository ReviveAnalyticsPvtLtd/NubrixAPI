"""One filter meaning for every widget's tables.

The dashboard's filter list becomes ONE Arrow expression per table. The SQL launcher narrows each
registered table with ``Dataset.filter(expr)``; saved Python widgets call ``applyFilters`` on every table
they load. Filter values are Arrow scalars and never reach SQL text.

Meaning (same as ``_apply_filters`` in ``utils/initMethods.py``, plus the documented changes):
- entries that are not dicts are ignored; a ``table.column`` key applies only to that table;
- a column the table does not have is skipped; several conditions combine with AND;
- a scalar means equals, a non-empty list means one of, ``min`` is ``>=``, ``max`` is ``<=`` (both apply);
- ``contains`` ignores case (literal substring), ``startswith`` / ``endswith`` match case, and the text
  operators apply only to string columns;
- a value is cast to the column's type with Arrow's safe cast; when the cast fails the condition matches
  no rows, so a filtered page never shows unfiltered numbers.
"""

from __future__ import annotations

import datetime
import decimal
import zoneinfo

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds


def _matchNothing() -> pc.Expression:
    return pc.scalar(False)


def _isString(dataType: pa.DataType) -> bool:
    return (
        pa.types.is_string(dataType)
        or pa.types.is_large_string(dataType)
        or (hasattr(pa.types, "is_string_view") and pa.types.is_string_view(dataType))
    )


def _isDate(dataType: pa.DataType) -> bool:
    return pa.types.is_date32(dataType) or pa.types.is_date64(dataType)


def _zone(tz: str) -> datetime.tzinfo:
    if tz.upper() in ("UTC", "Z"):
        return datetime.timezone.utc
    try:
        return zoneinfo.ZoneInfo(tz)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        # A fixed offset such as "+05:30".
        sign = -1 if tz.startswith("-") else 1
        hours, _, minutes = tz.lstrip("+-").partition(":")
        return datetime.timezone(sign * datetime.timedelta(hours=int(hours), minutes=int(minutes or 0)))


def _parseTimestamp(value, unit: str, tz: str | None) -> pa.Scalar:
    """Datetime text (``YYYY-MM-DD[ T]HH:MM:SS``, optional ``Z`` / offset) to a timestamp scalar.

    Text without an offset is read in the column's own timezone (wall-clock time for a naive column), as
    pandas did. Text with an offset keeps it.
    """
    if isinstance(value, str):
        moment = datetime.datetime.fromisoformat(value.strip())
    elif isinstance(value, datetime.datetime):
        moment = value
    elif isinstance(value, datetime.date):
        moment = datetime.datetime(value.year, value.month, value.day)
    else:
        return pa.scalar(value).cast(pa.timestamp(unit, tz=tz), safe=True)
    if tz is not None:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=_zone(tz))
    elif moment.tzinfo is not None:
        moment = moment.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return pa.scalar(moment, type=pa.timestamp(unit, tz=tz))


def _castScalar(value, dataType: pa.DataType) -> pa.Scalar | None:
    """The value as a scalar of ``dataType`` (safe cast), or None when it cannot be."""
    try:
        if value is None:
            return pa.scalar(None, type=dataType)
        if pa.types.is_timestamp(dataType):
            return _parseTimestamp(value, dataType.unit, dataType.tz)
        if pa.types.is_boolean(dataType):
            if isinstance(value, bool):
                return pa.scalar(value, type=dataType)
            if isinstance(value, str) and value.strip().lower() in ("true", "false"):
                return pa.scalar(value.strip().lower() == "true", type=dataType)
            return None
        if _isString(dataType):
            return pa.scalar(value if isinstance(value, str) else str(value)).cast(dataType, safe=True)
        if pa.types.is_decimal(dataType):
            return pa.scalar(str(value).strip()).cast(dataType, safe=True)
        if isinstance(value, str):
            value = value.strip()
        try:
            return pa.scalar(value).cast(dataType, safe=True)
        except (pa.ArrowException, OverflowError):
            if pa.types.is_integer(dataType) and not isinstance(value, bool):
                # 1.5 can never equal an integer, but ">= 1.5" must still mean 2 and up.
                return pa.scalar(value).cast(pa.float64(), safe=True)
            raise
    except (pa.ArrowException, ValueError, TypeError, OverflowError, decimal.InvalidOperation):
        return None


def _comparable(field: pc.Expression, dataType: pa.DataType) -> tuple[pc.Expression, pa.DataType]:
    """The column expression and the type values are cast to (dates compare as midnight timestamps)."""
    if pa.types.is_dictionary(dataType):
        return _comparable(field.cast(dataType.value_type), dataType.value_type)
    if hasattr(pa.types, "is_string_view") and pa.types.is_string_view(dataType):
        # pyarrow 20 has no compute kernels for string_view.
        return field.cast(pa.string()), pa.string()
    if _isDate(dataType):
        return field.cast(pa.timestamp("us")), pa.timestamp("us")
    return field, dataType


def _conditionExpressions(column: str, dataType: pa.DataType, condition) -> list[pc.Expression]:
    field, valueType = _comparable(pc.field(column), dataType)
    predicates: list[pc.Expression] = []

    if isinstance(condition, dict):
        if _isString(valueType):
            for key, build in (
                ("contains", lambda f, s: pc.match_substring(f, s, ignore_case=True)),
                ("startswith", pc.starts_with),
                ("endswith", pc.ends_with),
            ):
                if condition.get(key) is not None:
                    text = condition[key] if isinstance(condition[key], str) else str(condition[key])
                    predicates.append(build(field, text))
        for key, compare in (("min", pc.greater_equal), ("max", pc.less_equal)):
            if key in condition:
                scalar = _castScalar(condition[key], valueType)
                predicates.append(_matchNothing() if scalar is None else compare(field, scalar))
        return predicates

    if isinstance(condition, (list, tuple, set)):
        if not condition:
            return predicates
        members = [_castScalar(item, valueType) for item in condition if item is not None]
        members = [item for item in members if item is not None]
        if not members:
            return [_matchNothing()]
        # One is_in per scalar type, OR-ed: the result never depends on member order or on mixed types
        # (e.g. 1.5 beside 2 on an integer column).
        byType: dict[pa.DataType, list] = {}
        for item in members:
            byType.setdefault(item.type, []).append(item.as_py())
        expression = None
        for memberType, values in byType.items():
            one = pc.is_in(field, value_set=pa.array(values, type=memberType))
            expression = one if expression is None else expression | one
        return [expression]

    scalar = _castScalar(condition, valueType)
    return [_matchNothing() if scalar is None else pc.equal(field, scalar)]


def filterExpression(tableName: str, schema: pa.Schema, filters: list | None) -> pc.Expression | None:
    """One Arrow expression for this table's filters, or None when nothing applies to it."""
    predicates: list[pc.Expression] = []
    for entry in filters or []:
        if not isinstance(entry, dict):
            continue
        for columnPath, condition in entry.items():
            column = columnPath
            if isinstance(columnPath, str) and "." in columnPath:
                scope, column = columnPath.split(".", 1)
                if scope != tableName:
                    continue
            index = schema.get_field_index(column) if isinstance(column, str) else -1
            if index < 0:
                continue
            predicates.extend(_conditionExpressions(column, schema.field(index).type, condition))
    if not predicates:
        return None
    expression = predicates[0]
    for predicate in predicates[1:]:
        expression = expression & predicate
    return expression


def applyFilters(table: pa.Table, tableName: str, filters: list | None) -> pa.Table:
    expression = filterExpression(tableName, table.schema, filters)
    if expression is None:
        return table
    viewColumns = [
        i for i, field in enumerate(table.schema) if hasattr(pa.types, "is_string_view") and pa.types.is_string_view(field.type)
    ]
    # pyarrow 20 can neither filter nor take string_view columns, so they come back as plain string.
    for i in viewColumns:
        table = table.set_column(i, table.schema.field(i).name, table.column(i).cast(pa.string()))
    return table.filter(expression)


def filterDataset(dataset: ds.Dataset, tableName: str, filters: list | None) -> ds.Dataset:
    expression = filterExpression(tableName, dataset.schema, filters)
    return dataset if expression is None else dataset.filter(expression)
