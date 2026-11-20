"""
sqlRunner.py — the API side of a SQL chart run.

`runChartQuery` hands one checked query to the sandbox (`utils/sql_launcher.py`) through the project's slot
and returns the rows it wrote. Every way a run can fail becomes a `ChartRunError` with a stage and a message
written for a repair prompt. A busy sandbox is not a failure of the query, so `SandboxBusyError` passes through.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import pyarrow as pa

from nubrix.components.chartRequest import ChartRequest, requiredColumns, rowLimit
from nubrix.components.sqlCheck import CheckedSql
from utils.sandboxRunner import ResultTooLargeError, SandboxResult

if TYPE_CHECKING:
    from utils.codeExecutor import REPLManager

_REPLY_STAGES = ("bind", "run")
# Rows times columns plus every nested value. Far above what any chart draws; the builder turns each one into a Python object.
MAX_RESULT_VALUES = 2_000_000
_TOO_LARGE = "The query result is too large to return. Select fewer columns or rows, or aggregate."
_SHARED_BYTES = "The query returns a column of a type a chart cannot read. Cast ENUM and UNION columns to VARCHAR."
_NESTED_TYPES = (pa.types.is_struct, pa.types.is_list, pa.types.is_large_list, pa.types.is_fixed_size_list, pa.types.is_map)
_LEAF_TYPES = (
    pa.types.is_null, pa.types.is_boolean, pa.types.is_integer, pa.types.is_floating, pa.types.is_decimal,
    pa.types.is_date, pa.types.is_time, pa.types.is_timestamp, pa.types.is_duration, pa.types.is_interval,
    pa.types.is_string, pa.types.is_large_string, pa.types.is_binary, pa.types.is_large_binary, pa.types.is_fixed_size_binary,
)


@dataclass(frozen=True)
class QueryResult:
    table: pa.Table
    truncated: bool


class ChartRunError(RuntimeError):
    """A SQL run that did not give rows. `stage` says where it stopped; `message` is what to tell the model."""

    def __init__(self, stage: Literal["bind", "run", "timeout"], message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message


def _firstLine(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _unreadable(result: SandboxResult) -> str:
    """What to tell the model when stdout had no usable reply; keep the first stderr line when present."""
    message = "The query run returned no readable answer."
    detail = _firstLine(result.stderr)
    return f"{message} {detail}" if detail else message


def _reply(result: SandboxResult) -> dict:
    """The one JSON line the launcher printed, or a ChartRunError when it printed none."""
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    try:
        reply = json.loads(lines[-1]) if lines else None
    except ValueError:
        reply = None
    if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
        raise ChartRunError("run", _unreadable(result))
    if not reply["ok"] and (reply.get("stage") not in _REPLY_STAGES or not isinstance(reply.get("message"), str)):
        raise ChartRunError("run", _unreadable(result))
    return reply


def _fieldAt(buffer: bytes, table: int, index: int) -> int | None:
    """Where field `index` of the flatbuffer table at `table` starts, or None when the table leaves it out."""
    vtable = table - struct.unpack_from("<i", buffer, table)[0]
    if vtable < 0:
        raise ValueError("The table's field list is outside the header.")
    slot = 4 + 2 * index
    if slot + 2 > struct.unpack_from("<H", buffer, vtable)[0]:
        return None
    offset = struct.unpack_from("<H", buffer, vtable + slot)[0]
    return table + offset if offset else None


def _isCompressed(message: pa.ipc.Message) -> bool:
    """Whether a record batch's header names a codec for its buffers.

    pyarrow has no accessor for it and inflates such buffers as it reads them, so the answer comes from the header
    itself: field 2 of the Message is the RecordBatch, whose field 3 is the compression (Arrow's Message.fbs).
    Arrow checked the header's layout when it read the message.
    """
    header = message.metadata.to_pybytes()
    batchAt = _fieldAt(header, struct.unpack_from("<I", header, 0)[0], 2)
    if batchAt is None:
        raise ValueError("The message has no header.")
    return _fieldAt(header, batchAt + struct.unpack_from("<I", header, batchAt)[0], 3) is not None


def _isPlain(kind: pa.DataType) -> bool:
    """Whether every value of `kind` has bytes of its own, at any depth.

    A dictionary, a view, run ends and a union let many rows point at the same bytes, so a small file grows as the
    builder reads it. Only the types listed here pass, so a type this code does not know is refused too.
    """
    if isinstance(kind, pa.BaseExtensionType):
        return _isPlain(kind.storage_type)
    if any(isNested(kind) for isNested in _NESTED_TYPES):
        return all(_isPlain(kind.field(index).type) for index in range(kind.num_fields))
    return any(isLeaf(kind) for isLeaf in _LEAF_TYPES)


def _valueCount(array: pa.Array) -> int:
    """The values in `array` at every depth. A null or a boolean takes next to no bytes, so only a count bounds them."""
    kind = array.type
    if isinstance(kind, pa.BaseExtensionType):
        return _valueCount(array.storage)
    if pa.types.is_struct(kind):
        return len(array) + sum(_valueCount(array.field(index)) for index in range(kind.num_fields))
    if any(isNested(kind) for isNested in _NESTED_TYPES):
        return len(array) + _valueCount(array.values)
    return len(array)


def _readTable(resultBytes: bytes | None, maxRows: int) -> pa.Table:
    """The result as a table that takes no more room here than its file, which `SQL_RESULT_CAP_BYTES` bounds.

    The run's child wrote the file, so nothing in it is trusted. Compressed buffers, types whose rows share bytes
    and more rows than were asked for are refused before a batch is decoded; what is left is read as slices of
    the file, counted, and validated in full before the builder walks it.
    """
    if resultBytes is None:
        raise ChartRunError("run", "The query run wrote no result.")
    try:
        messages = pa.ipc.MessageReader.open_stream(pa.BufferReader(resultBytes))
        first = next(messages, None)
        if first is None or first.type != "schema":
            raise ValueError("The result does not start with a schema.")
        schema = pa.ipc.read_schema(first)
        if not all(_isPlain(field.type) for field in schema):
            raise ChartRunError("run", _SHARED_BYTES)
        batches, rows = [], 0
        for message in messages:
            # Version 4 could also name a codec in the message's own metadata, which `_isCompressed` does not read.
            if message.type != "record batch" or message.metadata_version != pa.ipc.MetadataVersion.V5 or _isCompressed(message):
                raise ValueError("The result holds a message the launcher does not write.")
            batch = pa.ipc.read_record_batch(message, schema)
            rows += batch.num_rows
            if rows > maxRows:
                raise ChartRunError("run", _TOO_LARGE)
            batches.append(batch)
        table = pa.Table.from_batches(batches, schema)
        if sum(_valueCount(chunk) for column in table.columns for chunk in column.chunks) > MAX_RESULT_VALUES:
            raise ChartRunError("run", _TOO_LARGE)
        table.validate(full=True)
    except (pa.ArrowException, struct.error, ValueError) as error:
        raise ChartRunError("run", "The query run wrote a result that could not be read.") from error
    return table


def runChartQuery(
    manager: REPLManager, projectId: str, checked: CheckedSql, request: ChartRequest, filters: list | None, waitForSlot: bool = True, shadow: bool = False
) -> QueryResult:
    """Run a checked query in the sandbox. Raises ChartRunError, or SandboxBusyError when no slot is free.

    `waitForSlot` and `shadow` are `REPLManager.runSql`'s; each is passed on only when it is not the default.
    """
    childRequest = {
        "sql": checked.sql,
        "tables": list(checked.tables),
        "filters": filters,
        "requiredColumns": requiredColumns(request),
        "rowLimit": rowLimit(request.chartType),
    }
    try:
        result = manager.runSql(childRequest, projectId, waitForSlot=waitForSlot, **({"shadow": True} if shadow else {}))
    except ResultTooLargeError as error:
        raise ChartRunError("run", _TOO_LARGE) from error
    if result.stopped:
        raise ChartRunError("timeout" if "timed out" in result.stopped else "run", result.stopped)
    if result.returncode != 0:
        raise ChartRunError("run", _firstLine(result.stderr) or f"The query run exited with code {result.returncode}.")
    reply = _reply(result)
    if not reply["ok"]:
        raise ChartRunError(reply["stage"], reply["message"])
    return QueryResult(_readTable(result.resultBytes, childRequest["rowLimit"]), bool(reply.get("truncated")))
