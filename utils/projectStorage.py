"""
projectStorage.py

Authenticated reads of project files in the private AnalyticsHub bucket.

Runs in the API and the Celery worker. Sandbox subprocesses import it too but
hold no credentials, so it reads SUPABASE_URL and SUPABASE_KEY at call time and
must not import api.commons.
"""

__all__ = ["StorageReadError", "objectUrl", "authHeaders", "readBytes", "readJson", "readTableBytes", "downloadTable", "readTablePage"]


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
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_PAGE_BATCH_ROWS = 8192
_UNSAFE_PATH_SEGMENT = re.compile(r"[/\\%\x00-\x1f]")
_SSL_CONTEXT: ssl.SSLContext | None = None


class StorageReadError(Exception):
    """A storage read failed. Message only, so it survives the trip back from a process pool."""


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


def _requireSuccess(response: httpx.Response) -> None:
    if not response.is_success:
        raise StorageReadError(f"Storage answered {response.status_code} for {response.url.path}.")


def readBytes(projectId: str, fileName: str) -> bytes:
    """Read a project file. Redirects stay off so the service key never reaches another host."""
    response = httpx.get(
        objectUrl(projectId, fileName),
        headers=authHeaders(),
        timeout=_READ_TIMEOUT_SECONDS,
        verify=_sslContext(),
    )
    _requireSuccess(response)
    return response.content


def readJson(projectId: str, fileName: str) -> dict | list:
    """Read and parse a project JSON file."""
    return json.loads(readBytes(projectId, fileName))


def readTableBytes(projectId: str, tableName: str) -> bytes:
    """Read a table's whole parquet file."""
    return readBytes(projectId, f"{tableName}.parquet")


def downloadTable(projectId: str, tableName: str, path: str, cancelled: threading.Event | None = None) -> None:
    """Stream a table's parquet file to a new file, so a big table is never held in memory.

    Setting `cancelled` stops it at the next chunk.
    """
    with httpx.stream(
        "GET",
        objectUrl(projectId, f"{tableName}.parquet"),
        headers=authHeaders(),
        timeout=_READ_TIMEOUT_SECONDS,
        verify=_sslContext(),
    ) as response:
        response.raise_for_status()
        with open(path, "xb") as table:
            for chunk in response.iter_bytes(_DOWNLOAD_CHUNK_BYTES):
                if cancelled is not None and cancelled.is_set():
                    raise InterruptedError("Table download cancelled.")
                table.write(chunk)


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
        self._size = self._readSize()

    def _readRange(self, start: int, end: int) -> httpx.Response:
        response = self._client.get(self._url, headers={"Range": f"bytes={start}-{end - 1}"})
        _requireSuccess(response)
        if response.status_code != httpx.codes.PARTIAL_CONTENT or len(response.content) != end - start:
            raise StorageReadError(f"Storage did not answer bytes {start}-{end - 1} of {response.url.path}.")
        return response

    def _readSize(self) -> int:
        """Read the size from HEAD. A GET costs far more on a large object, so a range is only the fallback."""
        response = self._client.head(self._url)
        _requireSuccess(response)
        length = response.headers.get("Content-Length")
        if length is not None:
            return int(length)
        total = self._readRange(0, 1).headers.get("Content-Range", "").rpartition("/")[2]
        if not total.isdigit():
            raise StorageReadError(f"Storage did not report the size of {self._url.rpartition('/')[2]}.")
        return int(total)

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
        content = self._readRange(start, end).content
        self._thread.position = end
        return content


def _pageBatches(parquet, rowGroups: list, firstRow: int, offset: int, limit: int) -> list:
    """Decode only up to the end of the page. pandas writes a million rows as one row group."""
    batches, batchStart = [], firstRow
    for batch in parquet.iter_batches(batch_size=_PAGE_BATCH_ROWS, row_groups=rowGroups):
        batchEnd = batchStart + batch.num_rows
        if batchEnd > offset:
            start = max(offset, batchStart)
            batches.append(batch.slice(start - batchStart, offset + limit - start))
        batchStart = batchEnd
        if batchStart >= offset + limit:
            break
    return batches


def readTablePage(projectId: str, tableName: str, offset: int, limit: int) -> tuple:
    """Return `(rows, totalRows)` for one page, reading only the footer and the row groups it touches.

    `pl.scan_parquet` can't do this on a private bucket. Polars drops
    `storage_options` on HTTP URLs, so it can't send the service key, and a
    signed URL makes storage read the whole object on Polars' HEAD request.
    """
    import polars as pl
    import pyarrow as pa
    import pyarrow.parquet as pq

    headers = {**authHeaders(), "Accept-Encoding": "identity"}
    with httpx.Client(headers=headers, timeout=_READ_TIMEOUT_SECONDS, verify=_sslContext()) as client:
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
            batches = _pageBatches(parquet, rowGroups, firstRow, offset, limit)
            return pl.from_arrow(pa.Table.from_batches(batches, schema=parquet.schema_arrow)), metadata.num_rows
