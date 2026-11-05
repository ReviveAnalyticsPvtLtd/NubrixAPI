"""Stored Parquet tables: the marker, the bounded footer check and the API's writer.

Why: the API only decodes a stored table in its own process when the table
carries a marker that only this API can write for that project and table, and
only after its footer has been read within a fixed size. Anything else is read
elsewhere. Every function that inspects a file returns None/False rather than
raising, so a bad file can never break the request path.
"""

import hmac
import io
import os
from hashlib import sha256

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from utils.logger import logger
from utils.sandboxLimits import _readBool, _readInt

MARKER_KEY = b"nubrix.stored-table"
MAX_INPROCESS_FOOTER_BYTES = 1 << 20
_MAX_FOOTER_CONTAINER = 100_000
_DEFAULT_MAX_DECODED_BYTES = 2147483648
_MARKER_DOMAIN = b"nubrix stored-table marker v1"
_PARQUET_MAGIC = b"PAR1"

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
    pq.write_table(
        table,
        sink,
        compression="zstd",
        data_page_size=1 << 20,
        row_group_size=1_048_576,
    )
    return sink.getvalue()
