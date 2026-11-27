"""A local stand-in for the private AnalyticsHub bucket, for the chart evaluation harness.

It serves `<folder>/<projectId>/<fileName>` at the object route `utils/projectStorage.py` reads, with the same
`Authorization: Bearer <key>` header, and answers HEAD and `Range` requests like storage does. It listens on 127.0.0.1
only, asks for the key on every request, and serves nothing outside `<folder>`.

    url, stop = serveFolder(folder)   # the key is SUPABASE_KEY, or pass key=
"""

__all__ = ["serveFolder"]

import hmac
import http.server
import json
import os
import re
import threading
from collections.abc import Callable
from urllib.parse import unquote, urlsplit

_OBJECT_PREFIX = "/storage/v1/object/AnalyticsHub/"
_UNSAFE_SEGMENT = re.compile(r"[/\\:\x00-\x1f]")
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
_CHUNK_BYTES = 1024 * 1024


def _segments(path: str) -> tuple[str, str] | None:
    """`(projectId, fileName)` for an object route, or None for anything else, including any `.`, `..` or separator."""
    if not path.startswith(_OBJECT_PREFIX):
        return None
    parts = [unquote(part) for part in path[len(_OBJECT_PREFIX):].split("/")]
    if len(parts) != 2:
        return None
    if any(not part or part in (".", "..") or _UNSAFE_SEGMENT.search(part) for part in parts):
        return None
    return parts[0], parts[1]


_IGNORE = "ignore"
_UNSATISFIABLE = "unsatisfiable"


def _byteRange(header: str, size: int) -> "tuple[int, int] | str":
    """What a `Range` header asks for, as RFC 7233 has it for a single range of bytes.

    Returns the inclusive `(start, end)` to send (206), `_UNSATISFIABLE` (416), or `_IGNORE` to send the whole file
    (200): a header that is not a single valid `bytes` range is ignored, as is one with several ranges. A suffix
    (`-n`) is the last n bytes, and an open range (`a-`) runs to the end.
    """
    match = _RANGE.match(header.strip())
    if match is None:
        return _IGNORE
    first, last = match.groups()
    if not first and not last:
        return _IGNORE
    if not first:
        length = int(last)
        return (max(size - length, 0), size - 1) if length and size else _UNSATISFIABLE
    start = int(first)
    if last and int(last) < start:
        return _IGNORE
    end = min(int(last), size - 1) if last else size - 1
    return (start, end) if start < size else _UNSATISFIABLE


def serveFolder(folder: str, key: str | None = None, requestLog: list | None = None) -> tuple[str, Callable[[], None]]:
    """Serve `folder` on 127.0.0.1 and return `(url, stop)`.

    Requests must carry `Authorization: Bearer <key>`; `key` defaults to the SUPABASE_KEY of the environment.
    When `requestLog` is a list, every response adds `{"method", "file", "range", "status"}` to it (`file` is the file
    name of an object route, else None), so a caller can count what was read and how. `stop()` ends the server and its
    thread, and may be called again.
    """
    key = os.environ.get("SUPABASE_KEY") if key is None else key
    if not key:
        raise ValueError("The local storage server needs a key: pass key= or set SUPABASE_KEY.")
    root = os.path.realpath(folder)
    if not os.path.isdir(root):
        raise ValueError("The folder to serve does not exist.")
    expected = f"Bearer {key}".encode()

    def locate(projectId: str, fileName: str) -> str | None:
        """The real path of a regular file inside `root`, or None."""
        path = os.path.realpath(os.path.join(root, projectId, fileName))
        inside = os.path.commonpath([root, path]) == root if os.path.splitdrive(path)[0] == os.path.splitdrive(root)[0] else False
        return path if inside and path != root and os.path.isfile(path) else None

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def send_response(self, code, message=None):
            if requestLog is not None:
                segments = _segments(urlsplit(self.path).path)
                requestLog.append({"method": self.command, "file": segments[1] if segments else None, "range": self.headers.get("Range"), "status": code})
            super().send_response(code, message)

        def do_GET(self):
            self._serve(sendBody=True)

        def do_HEAD(self):
            self._serve(sendBody=False)

        def _json(self, status: int, message: str, sendBody: bool) -> None:
            payload = json.dumps({"statusCode": str(status), "error": message, "message": message}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if sendBody:
                self.wfile.write(payload)

        def _serve(self, sendBody: bool) -> None:
            try:
                self._respond(sendBody)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True

        def _respond(self, sendBody: bool) -> None:
            if not hmac.compare_digest((self.headers.get("Authorization") or "").encode(), expected):
                self._json(401, "Unauthorized", sendBody)
                return
            segments = _segments(urlsplit(self.path).path)
            if segments is None:
                self._json(404, "not_found", sendBody)
                return
            path = locate(*segments)
            if path is None:
                self._json(404, "not_found", sendBody)
                return
            size = os.path.getsize(path)
            start, end, status = 0, size - 1, 200
            requested = self.headers.get("Range")
            span = _byteRange(requested, size) if requested else _IGNORE
            if span == _UNSATISFIABLE:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if span != _IGNORE:
                (start, end), status = span, 206
            self.send_response(status)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "application/octet-stream")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1 if size else 0))
            self.end_headers()
            if not sendBody or not size:
                return
            with open(path, "rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = handle.read(min(_CHUNK_BYTES, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="local-storage-server", daemon=True)
    thread.start()
    stopped = threading.Event()

    def stop() -> None:
        if stopped.is_set():
            return
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)

    return f"http://127.0.0.1:{server.server_address[1]}", stop
