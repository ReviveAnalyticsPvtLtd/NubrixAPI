"""
projectStorage.py

Authenticated reads of project files in the private AnalyticsHub bucket.

Runs in the API, the Celery worker and the sandbox subprocesses, so it reads
SUPABASE_URL and SUPABASE_KEY at call time and must not import api.commons.
"""

__all__ = ["objectUrl", "authHeaders", "readBytes", "readJson", "readTableBytes", "readTablePage"]


import httpx
import json
import io
import os
import re
import ssl
import threading
from urllib.parse import quote

_BUCKET = "AnalyticsHub"
_READ_TIMEOUT_SECONDS = 60
_UNSAFE_PATH_SEGMENT = re.compile(r"[/\\%\x00-\x1f]")
_SSL_CONTEXT: ssl.SSLContext | None = None


def _sslContext() -> ssl.SSLContext:
    """Share one context: httpx rebuilds it per call otherwise, which costs far more than the read."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = httpx.create_ssl_context()
    return _SSL_CONTEXT


def _pathSegment(value: str) -> str:
    """Encode one path segment. The service key can read every bucket, so a `..` must never reach storage."""
    if not value or value in (".", "..") or _UNSAFE_PATH_SEGMENT.search(value):
        raise ValueError(f"Invalid storage path segment: {value!r}")
    return quote(value, safe="")


def objectUrl(projectId: str, fileName: str) -> str:
    """Build the authenticated URL of a project file. Send it with `authHeaders()`."""
    baseUrl = os.environ["SUPABASE_URL"].rstrip("/")
    return f"{baseUrl}/storage/v1/object/{_BUCKET}/{_pathSegment(projectId)}/{_pathSegment(fileName)}"


def authHeaders() -> dict:
    """Return the headers the storage client itself sends."""
    key = os.environ["SUPABASE_KEY"]
    return {"apikey": key, "Authorization": f"Bearer {key}"}


def readBytes(projectId: str, fileName: str) -> bytes:
    """Read a project file. Redirects stay off so the service key never reaches another host."""
    response = httpx.get(
        objectUrl(projectId, fileName),
        headers=authHeaders(),
        timeout=_READ_TIMEOUT_SECONDS,
        verify=_sslContext(),
    )
    response.raise_for_status()
    return response.content


def readJson(projectId: str, fileName: str) -> dict | list:
    """Read and parse a project JSON file."""
    return json.loads(readBytes(projectId, fileName))


def readTableBytes(projectId: str, tableName: str) -> bytes:
    """Read a table's whole parquet file."""
    return readBytes(projectId, f"{tableName}.parquet")


class _TableFile(io.RawIOBase):
    """Seekable view of a stored file: one authenticated Range GET per read.

    Arrow reads from several threads, each pairing a seek with a read, so the
    position is kept per thread.
    """

    def __init__(self, client: httpx.Client, url: str):
        super().__init__()
        self._client = client
        self._url = url
        self._thread = threading.local()
        response = client.head(url)
        response.raise_for_status()
        self._size = int(response.headers["Content-Length"])

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return getattr(self._thread, "position", 0)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        origin = {io.SEEK_SET: 0, io.SEEK_CUR: self.tell(), io.SEEK_END: self._size}[whence]
        self._thread.position = origin + offset
        return self._thread.position

    def read(self, size: int = -1) -> bytes:
        start = self.tell()
        end = self._size if size is None or size < 0 else min(start + size, self._size)
        if end <= start:
            return b""
        response = self._client.get(self._url, headers={"Range": f"bytes={start}-{end - 1}"})
        response.raise_for_status()
        if response.status_code != httpx.codes.PARTIAL_CONTENT:
            raise httpx.HTTPError(f"Storage ignored the Range header with status {response.status_code}.")
        self._thread.position = end
        return response.content


def readTablePage(projectId: str, tableName: str, offset: int, limit: int) -> tuple:
    """Return `(rows, totalRows)` for one page, reading only the footer and the row groups it touches.

    `pl.scan_parquet` can't do this on a private bucket. Polars drops
    `storage_options` on HTTP URLs, so it can't send the service key, and a
    signed URL makes storage read the whole object on Polars' HEAD request.
    """
    import polars as pl
    import pyarrow.parquet as pq

    with httpx.Client(headers=authHeaders(), timeout=_READ_TIMEOUT_SECONDS, verify=_sslContext()) as client:
        tableFile = _TableFile(client, objectUrl(projectId, f"{tableName}.parquet"))
        with pq.ParquetFile(tableFile, pre_buffer=True) as parquet:
            metadata = parquet.metadata
            rowGroups, firstRow, nextRow = [], offset, 0
            for index in range(metadata.num_row_groups):
                groupRows = metadata.row_group(index).num_rows
                if nextRow < offset + limit and nextRow + groupRows > offset:
                    if not rowGroups:
                        firstRow = nextRow
                    rowGroups.append(index)
                nextRow += groupRows
            table = parquet.read_row_groups(rowGroups) if rowGroups else parquet.schema_arrow.empty_table()
            return pl.from_arrow(table).slice(offset - firstRow, limit), metadata.num_rows
