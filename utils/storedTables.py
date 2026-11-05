"""Stored Parquet tables: the marker, the bounded footer check, the API's writer, and reads in a limited child.

Why: the API only decodes a stored table in its own process when the table
carries a marker that only this API can write for that project and table, and
only after its footer has been read within a fixed size. Anything else is read
elsewhere. The footer and marker checks return None/False rather than raising,
so a bad file can never break the request path.

A run's output is rewritten by `normaliseRunOutput` in a limited child before it
can be stored, and an unmarked table is decoded by the `readUntrusted*`
functions in a limited child too: a Parquet file can decode to far more memory
than its size, so neither is decoded in the API's own process.
"""

import hmac
import io
import json
import os
from hashlib import sha256

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from utils.logger import logger
from utils.sandboxLimits import _formatBytes, _readBool, _readInt, currentRunLimits
from utils.sandboxSlots import SandboxBusyError

MARKER_KEY = b"nubrix.stored-table"
MAX_INPROCESS_FOOTER_BYTES = 1 << 20
_MAX_FOOTER_CONTAINER = 100_000
_DEFAULT_MAX_DECODED_BYTES = 2147483648
_DEFAULT_READER_MEMORY_BYTES = 4294967296
_MARKER_DOMAIN = b"nubrix stored-table marker v1"
_PARQUET_MAGIC = b"PAR1"
# How every stored table is written: by the API's writers and by the normaliser.
WRITE_SETTINGS = {"compression": "zstd", "data_page_size": 1 << 20}
ROW_GROUP_ROWS = 1_048_576
# A child's own refusal of a table over the decoded-size limit. No runtime here exits with it: Python exits 1 for an
# uncaught error, 2 for a usage error and 120 when flushing fails, MSVC's abort() exits 3, shells use 126, 127 and
# 128 + n, and a limit that ends the child gives a signal. So only the launcher's own size check exits with this.
EXIT_TOO_LARGE = 42
NORMALISE_BATCH_ROWS = 65536
_MAX_PANDAS_METADATA_BYTES = 64 * 1024
# An info answer is a schema, a shape and one row, so it has its own cap whatever the decoded-size limit is.
MAX_INFO_ANSWER_BYTES = 4 * 1024 * 1024
MAX_SAMPLE_CHARS = 64 * 1024
# Carries a page's row count from the reader child; the parent removes it.
PAGE_ROWS_KEY = b"nubrix.reader.total-rows"
_NORMALISE_LAUNCHER = "normalise_launcher.py"
_READER_LAUNCHER = "table_reader_launcher.py"
_NORMALISE_TIMEOUT_SECONDS = 120
_READER_TIMEOUT_SECONDS = 60
_STDERR_TAIL_CHARS = 2000
_INFO_KEYS = {"columns", "rows", "cols", "sample", "sampleFailed"}
_SAVE_FAILED = "The transformation's result could not be read safely."
_READ_FAILED = "This table could not be read safely."

_missingKeyWarned = False


class TableTooLargeError(ValueError):
    """The table would decode to more than STORED_TABLE_MAX_DECODED_BYTES."""


class TableUnreadableError(ValueError):
    """The table could not be read safely."""


def maxDecodedBytes() -> int:
    """Largest decoded table the API stores or opens. 0 means no limit."""
    return _readInt("STORED_TABLE_MAX_DECODED_BYTES", _DEFAULT_MAX_DECODED_BYTES)


def normaliseEnabled() -> bool:
    """Whether stored tables are rewritten and trusted by marker (1) or not (0)."""
    return _readBool("STORED_TABLE_NORMALISE", True)


def tableMarker(projectId: str, tableName: str) -> bytes:
    """The marker for one table, or b"" when the service key is not configured.

    Without the key nothing can be marked, so no table is ever treated as
    marked; writers then write plain tables and reads take the bounded path.
    """
    global _missingKeyWarned
    key = os.environ.get("SUPABASE_KEY", "")
    if not key:
        if not _missingKeyWarned:
            _missingKeyWarned = True
            logger.warning("SUPABASE_KEY is not set: stored tables are written without a marker.")
        return b""
    derived = hmac.new(key.encode(), _MARKER_DOMAIN, sha256).digest()
    message = f"v1|{projectId}|{tableName}".encode()
    return hmac.new(derived, message, sha256).hexdigest().encode()


def readFooter(data: bytes) -> "pq.FileMetaData | None":
    """Parse a Parquet footer of at most 1 MiB. None for anything else."""
    try:
        size = len(data)
        if size < 12 or data[:4] != _PARQUET_MAGIC or data[-4:] != _PARQUET_MAGIC:
            return None
        footerLength = int.from_bytes(data[-8:-4], "little")
        if footerLength == 0 or footerLength > MAX_INPROCESS_FOOTER_BYTES or footerLength > size - 12:
            return None
        parquetFile = pq.ParquetFile(
            pa.BufferReader(data),
            thrift_string_size_limit=MAX_INPROCESS_FOOTER_BYTES,
            thrift_container_size_limit=_MAX_FOOTER_CONTAINER,
        )
        return parquetFile.metadata
    except Exception:
        return None


def markerIsValid(metadata: "pq.FileMetaData", projectId: str, tableName: str) -> bool:
    """True when the file carries this table's marker. Never raises."""
    try:
        if not normaliseEnabled():
            return False
        expected = tableMarker(projectId, tableName)
        if not expected:
            return False
        found = (metadata.metadata or {}).get(MARKER_KEY)
        if not isinstance(found, bytes):
            return False
        return hmac.compare_digest(found, expected)
    except Exception:
        return False


def encodedUncompressedBytes(metadata: "pq.FileMetaData") -> int:
    """Sum of the uncompressed size of every column chunk in every row group."""
    return sum(
        metadata.row_group(group).column(column).total_uncompressed_size
        for group in range(metadata.num_row_groups)
        for column in range(metadata.num_columns)
    )


def writeStoredTable(table: "pa.Table | pd.DataFrame", projectId: str, tableName: str) -> bytes:
    """Serialise a table with this table's marker, in small pages and row groups.

    The schema metadata is replaced, so a marker or other metadata carried by
    the input is dropped. For a DataFrame, the pandas entry is the one built
    here from the frame itself, which keeps nullable dtypes round-tripping.
    """
    pandasMetadata = None
    if isinstance(table, pd.DataFrame):
        table = pa.Table.from_pandas(table, preserve_index=False)
        pandasMetadata = (table.schema.metadata or {}).get(b"pandas")
    limit = maxDecodedBytes()
    if limit and table.nbytes > limit:
        raise TableTooLargeError(f"Table is {table.nbytes} bytes decoded, over the limit of {limit}.")
    metadata: dict[bytes, bytes] = {}
    if pandasMetadata is not None:
        metadata[b"pandas"] = pandasMetadata
    marker = tableMarker(projectId, tableName)
    if marker:
        metadata[MARKER_KEY] = marker
    table = table.replace_schema_metadata(metadata)
    sink = io.BytesIO()
    pq.write_table(table, sink, **WRITE_SETTINGS, row_group_size=ROW_GROUP_ROWS)
    return sink.getvalue()


def readerMemoryBytes() -> int:
    """Memory (RLIMIT_DATA) of the child that reads an unmarked table. 0 decodes in this process instead."""
    return _readInt("TABLE_READER_MEMORY_BYTES", _DEFAULT_READER_MEMORY_BYTES)


def trustedPandasMetadata(schema: "pa.Schema") -> bytes | None:
    """The schema's pandas entry, re-serialised, or None when it is not one an honest table would carry.

    It is kept only as a JSON object of at most 64 KiB whose columns all name fields of this schema. It lets
    nullable dtypes such as Int64 read back as they were written.
    """
    try:
        raw = (schema.metadata or {}).get(b"pandas")
        if raw is None or len(raw) > _MAX_PANDAS_METADATA_BYTES:
            return None
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("columns"), list):
            return None
        names = set(schema.names)
        for column in parsed["columns"]:
            name = column.get("field_name", column.get("name")) if isinstance(column, dict) else None
            if not isinstance(name, str) or name not in names:
                return None
        return json.dumps(parsed).encode()
    except Exception:
        return None


def _tooLargeToOpen(limit: int) -> TableTooLargeError:
    return TableTooLargeError(f"This table is too large to open (over {_formatBytes(limit)}).")


def _source(data: "bytes | str"):
    """A path is opened by pyarrow; bytes are read from memory."""
    return pa.BufferReader(data) if isinstance(data, (bytes, bytearray, memoryview)) else data


def _overDecodedLimit(metadata: "pq.FileMetaData", limit: int) -> bool:
    """Whether the whole table is over `limit` bytes encoded and uncompressed (0 means no limit)."""
    return bool(limit) and encodedUncompressedBytes(metadata) > limit


def plainType(dataType: "pa.DataType") -> "pa.DataType":
    """`dataType` with every dictionary, at any depth, replaced by its value type."""
    if pa.types.is_dictionary(dataType):
        return plainType(dataType.value_type)
    if pa.types.is_large_list(dataType):
        return pa.large_list(dataType.value_field.with_type(plainType(dataType.value_type)))
    if pa.types.is_fixed_size_list(dataType):
        return pa.list_(dataType.value_field.with_type(plainType(dataType.value_type)), dataType.list_size)
    if pa.types.is_list(dataType):
        return pa.list_(dataType.value_field.with_type(plainType(dataType.value_type)))
    if pa.types.is_map(dataType):
        return pa.map_(
            dataType.key_field.with_type(plainType(dataType.key_type)),
            dataType.item_field.with_type(plainType(dataType.item_type)),
            dataType.keys_sorted,
        )
    if pa.types.is_struct(dataType):
        return pa.struct([dataType.field(i).with_type(plainType(dataType.field(i).type)) for i in range(dataType.num_fields)])
    return dataType


def hasDictionary(dataType: "pa.DataType") -> bool:
    """Whether a dictionary appears anywhere in `dataType`, including inside any nested or extension type."""
    if pa.types.is_dictionary(dataType):
        return True
    if isinstance(dataType, pa.BaseExtensionType):
        return hasDictionary(dataType.storage_type)
    return any(hasDictionary(dataType.field(i).type) for i in range(dataType.num_fields))


def decodeTable(data: "bytes | str", limit: int) -> "pa.Table":
    """Decode a whole table after the decoded-size check on its footer."""
    with pq.ParquetFile(_source(data)) as parquet:
        metadata = parquet.metadata
    if _overDecodedLimit(metadata, limit):
        raise _tooLargeToOpen(limit)
    return pq.read_table(_source(data))


def decodePage(data: "bytes | str", offset: int, limit: int, maxDecoded: int) -> tuple["pa.Table", int]:
    """Decode the rows `offset` to `offset + limit` and return them with the table's row count.

    The whole table is checked against `maxDecoded` first, as for `decodeTable`; then only the row groups holding the
    page are decoded, as the data grid reads them.
    """
    from utils.projectStorage import _pageBatches

    with pq.ParquetFile(_source(data)) as parquet:
        metadata = parquet.metadata
        if _overDecodedLimit(metadata, maxDecoded):
            raise _tooLargeToOpen(maxDecoded)
        rowGroups, firstRow, nextRow = [], offset, 0
        for index in range(metadata.num_row_groups):
            groupRows = metadata.row_group(index).num_rows
            if nextRow < offset + limit and nextRow + groupRows > offset:
                if not rowGroups:
                    firstRow = nextRow
                rowGroups.append(index)
            nextRow += groupRows
        batches = _pageBatches(parquet, rowGroups, firstRow, offset, limit)
        return pa.Table.from_batches(batches, schema=parquet.schema_arrow), metadata.num_rows


def tableInfo(data: "bytes | str", maxDecoded: int) -> dict:
    """Columns, shape and a one-row sample, as metadata generation reads them.

    The shape comes from the footer. A table over `maxDecoded` gets no sample (sampleFailed, as when the sample can't
    be read), and nothing is decoded for it.
    """
    with pq.ParquetFile(_source(data)) as parquet:
        metadata = parquet.metadata
        info = {
            "columns": [[field.name, str(field.type)] for field in parquet.schema_arrow],
            "rows": int(metadata.num_rows),
            "cols": int(metadata.num_columns),
            "sample": None,
            "sampleFailed": False,
        }
        if _overDecodedLimit(metadata, maxDecoded):
            info["sampleFailed"] = True
            return info
        try:
            sample = next(parquet.iter_batches(batch_size=1), None)
            if sample is not None:
                info["sample"] = sample.to_pandas().to_string()
        except Exception:
            info["sampleFailed"] = True
        return info


def _logChildFailure(what: str, projectId: str, tableName: str, result=None, error: BaseException | None = None) -> None:
    """Keep what the child said for us. It never reaches the user's copy."""
    if result is not None:
        detail = f"exit {result.returncode}, stopped {result.stopped!r}, stderr: {result.stderr[-_STDERR_TAIL_CHARS:]}"
    else:
        detail = f"{type(error).__name__}: {str(error)[-_STDERR_TAIL_CHARS:]}"
    logger.warning(f"{what} failed for table {tableName!r} in project {projectId}: {detail}")


def _logTooLarge(what: str, projectId: str, tableName: str, limit: int) -> None:
    logger.warning(f"{what} refused table {tableName!r} in project {projectId}: over {limit} bytes.")


def normaliseRunOutput(projectId: str, tableName: str, parquetBytes: bytes) -> bytes:
    """Rewrite a run's Parquet output in a limited child, with this table's marker and no metadata from the input.

    The child streams the input and refuses it once its decoded batches pass STORED_TABLE_MAX_DECODED_BYTES, so a
    file built to expand never reaches this process. Only the marker value is passed, never the key behind it.
    A failure of the child or its answer is a ValueError carrying the user's copy. SandboxBusyError and errors of the
    sandbox itself (no free uid, a uid that could not be cleared, a TypeError) propagate unchanged, as they do for
    code runs. With STORED_TABLE_NORMALISE=0 the bytes are returned as they are.
    """
    if not normaliseEnabled():
        return parquetBytes
    from utils import sandboxRunner

    limit = maxDecodedBytes()
    # The output is stored as it comes back, so it gets the cap the run's own output had.
    cap = sandboxRunner._MAX_RESULT_BYTES
    request = {"marker": tableMarker(projectId, tableName).decode("ascii"), "maxDecodedBytes": limit}
    what = "Normalising a run's output"
    try:
        result = sandboxRunner.runTrustedSandbox(_NORMALISE_LAUNCHER, projectId, parquetBytes, request, _NORMALISE_TIMEOUT_SECONDS, cap)
    except SandboxBusyError:
        raise
    except sandboxRunner.ResultTooLargeError as error:
        _logTooLarge(what, projectId, tableName, cap)
        raise ValueError(f"The transformation's result is too large to save (over {_formatBytes(cap)}).") from error
    except (ValueError, OSError) as error:
        _logChildFailure(what, projectId, tableName, error=error)
        raise ValueError(_SAVE_FAILED) from error
    if result.returncode == EXIT_TOO_LARGE and not result.stopped and limit:
        _logTooLarge(what, projectId, tableName, limit)
        raise ValueError(f"The transformation's result is too large to save (over {_formatBytes(limit)}).")
    if result.returncode != 0 or result.stopped or result.resultBytes is None:
        _logChildFailure(what, projectId, tableName, result=result)
        raise ValueError(_SAVE_FAILED)
    return result.resultBytes


def _readInChild(projectId: str, tableName: str, data: bytes, request: dict) -> bytes:
    """Run the reader child on `data` and return its answer, mapping every failure to the user's copy."""
    from utils import sandboxRunner

    limit = maxDecodedBytes()
    isInfo = request["op"] == "info"
    cap = MAX_INFO_ANSWER_BYTES if isInfo else limit or sandboxRunner._MAX_RESULT_BYTES
    limits = currentRunLimits(maxProcessMemory=readerMemoryBytes())
    what = f"Reading a table ({request['op']})"
    try:
        result = sandboxRunner.runTrustedSandbox(
            _READER_LAUNCHER, projectId, data, {**request, "maxDecodedBytes": limit}, _READER_TIMEOUT_SECONDS, cap, limits=limits
        )
    except SandboxBusyError:
        raise
    except sandboxRunner.ResultTooLargeError as error:
        if isInfo:  # An info answer that big is not one tableInfo builds.
            _logChildFailure(what, projectId, tableName, error=error)
            raise TableUnreadableError(_READ_FAILED) from error
        _logTooLarge(what, projectId, tableName, cap)
        raise _tooLargeToOpen(cap) from error
    except (ValueError, OSError) as error:
        _logChildFailure(what, projectId, tableName, error=error)
        raise TableUnreadableError(_READ_FAILED) from error
    if result.returncode == EXIT_TOO_LARGE and not result.stopped and limit:
        _logTooLarge(what, projectId, tableName, limit)
        raise _tooLargeToOpen(limit)
    if result.returncode != 0 or result.stopped or result.resultBytes is None:
        _logChildFailure(what, projectId, tableName, result=result)
        raise TableUnreadableError(_READ_FAILED)
    return result.resultBytes


def _ipcTable(projectId: str, tableName: str, answer: bytes) -> "pa.Table":
    """Read the child's IPC answer. A dictionary at any depth would expand here, past the result cap, so it is refused."""
    try:
        with pa.ipc.open_stream(pa.BufferReader(answer)) as reader:
            if any(hasDictionary(field.type) for field in reader.schema):
                raise ValueError("The answer holds a dictionary column.")
            return reader.read_all()
    except Exception as error:
        _logChildFailure("Reading a table's answer", projectId, tableName, error=error)
        raise TableUnreadableError(_READ_FAILED) from error


def _inProcess(what: str, projectId: str, tableName: str, decode):
    """Decode in this process (TABLE_READER_MEMORY_BYTES=0), logging a refusal over the decoded-size limit as the child path does."""
    limit = maxDecodedBytes()
    try:
        return decode(limit)
    except TableTooLargeError:
        _logTooLarge(what, projectId, tableName, limit)
        raise


def readUntrustedTable(projectId: str, tableName: str, data: bytes) -> "pa.Table":
    """Decode a whole table that may not be the API's own, in a child limited to TABLE_READER_MEMORY_BYTES.

    Dictionary columns, at any depth, come back decoded and the schema keeps only a valid pandas entry. Raises
    SandboxBusyError unchanged, TableTooLargeError over STORED_TABLE_MAX_DECODED_BYTES and TableUnreadableError for
    anything the child or its answer does wrong. Errors of the sandbox itself (no free uid, a uid that could not be
    cleared, a TypeError) propagate as they do for code runs. With TABLE_READER_MEMORY_BYTES=0 the table is decoded in
    this process as before, after the same size check.
    """
    if not readerMemoryBytes():
        return _inProcess("Reading a table (table)", projectId, tableName, lambda limit: decodeTable(data, limit))
    return _ipcTable(projectId, tableName, _readInChild(projectId, tableName, data, {"op": "table"}))


def readUntrustedPage(projectId: str, tableName: str, data: bytes, offset: int, limit: int) -> tuple["pl.DataFrame", int]:
    """One page of a table that may not be the API's own, and the table's row count, read as `readUntrustedTable` is.

    The whole table is held to STORED_TABLE_MAX_DECODED_BYTES, not just the page. Errors, including the sandbox's own,
    and the revert are as for `readUntrustedTable`.
    """
    import polars as pl

    if not readerMemoryBytes():
        table, totalRows = _inProcess("Reading a table (page)", projectId, tableName, lambda maxDecoded: decodePage(data, offset, limit, maxDecoded))
        return pl.from_arrow(table), totalRows
    answer = _readInChild(projectId, tableName, data, {"op": "page", "offset": int(offset), "limit": int(limit)})
    table = _ipcTable(projectId, tableName, answer)
    metadata = dict(table.schema.metadata or {})
    rows = metadata.pop(PAGE_ROWS_KEY, b"")
    if not rows.isdigit():
        _logChildFailure("Reading a table's page", projectId, tableName, error=ValueError("no row count"))
        raise TableUnreadableError(_READ_FAILED)
    return pl.from_arrow(table.replace_schema_metadata(metadata or None)), int(rows)


def _isInfo(info) -> bool:
    """Whether the child's answer has exactly the shape `tableInfo` builds."""
    return (
        isinstance(info, dict)
        and set(info) == _INFO_KEYS
        and isinstance(info["columns"], list)
        and all(isinstance(pair, list) and len(pair) == 2 and all(isinstance(part, str) for part in pair) for pair in info["columns"])
        and all(type(info[key]) is int and info[key] >= 0 for key in ("rows", "cols"))
        and (info["sample"] is None or isinstance(info["sample"], str))
        and isinstance(info["sampleFailed"], bool)
    )


def readUntrustedInfo(projectId: str, tableName: str, data: bytes) -> dict:
    """Columns, shape and a one-row sample of a table that may not be the API's own, read as `readUntrustedTable` is.

    Returns {"columns": [[name, type], ...], "rows", "cols", "sample": str | None, "sampleFailed": bool}: a sample of
    None is a table with no rows, unless sampleFailed says it could not be read. A table over
    STORED_TABLE_MAX_DECODED_BYTES gets its shape and no sample. The child's sample is cut to MAX_SAMPLE_CHARS and its
    answer capped at MAX_INFO_ANSWER_BYTES; a larger answer is TableUnreadableError. Errors, including the sandbox's
    own, and the revert are otherwise as for `readUntrustedTable`.
    """
    if not readerMemoryBytes():
        return tableInfo(data, maxDecodedBytes())
    answer = _readInChild(projectId, tableName, data, {"op": "info"})
    try:
        info = json.loads(answer)
    except Exception:  # Malformed or too deeply nested.
        info = None
    if not _isInfo(info):
        _logChildFailure("Reading a table's info", projectId, tableName, error=ValueError("malformed answer"))
        raise TableUnreadableError(_READ_FAILED)
    return info
