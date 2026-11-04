"""A local HTTP server that behaves like the private AnalyticsHub bucket.

The status codes and bodies match storage-api v1.82.1 behind Kong: a private
object answers 400 with a JSON 404 body and the public route is dead.
"""

import gzip
import http.server
import json
import threading
from urllib.parse import unquote, urlsplit

SERVICE_KEY = "test-service-key"
_OBJECT_PREFIX = "/storage/v1/object/"
_NOT_FOUND = {"statusCode": "404", "error": "not_found", "message": "Object not found"}
_NO_BUCKET = {"statusCode": "404", "error": "Bucket not found", "message": "Bucket not found"}


class FakePrivateStorage:
    """Serves `objects`, keyed `AnalyticsHub/<projectId>/<fileName>`."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.requests: list[dict] = []
        self.redirectTo: str | None = None
        self.ignoreRange = False
        self.publicBucket = False
        self.publicRouteStatus: int | None = None
        self.publicKeys: set[str] = set()
        self.anonKey: str | None = None
        self.omitContentLength = False
        self.truncateRanges = False
        self.hideRangeTotal = False
        self.compressUnlessIdentity = False
        self.responseHeaders: dict[str, str] = {}
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                fake._handle(self, sendBody=True)

            def do_HEAD(self):
                fake._handle(self, sendBody=False)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def put(self, projectId: str, fileName: str, body: bytes) -> None:
        self.objects[f"AnalyticsHub/{projectId}/{fileName}"] = body

    def putJson(self, projectId: str, fileName: str, value) -> None:
        self.put(projectId, fileName, json.dumps(value).encode("utf-8"))

    def pathsRead(self) -> list[str]:
        return [entry["path"] for entry in self.requests]

    def _authorized(self, handler) -> bool:
        return handler.headers.get("Authorization") == f"Bearer {SERVICE_KEY}"

    def _handle(self, handler, sendBody: bool) -> None:
        parts = urlsplit(handler.path)
        self.requests.append({
            "method": handler.command,
            "path": parts.path,
            "query": parts.query,
            "authorization": handler.headers.get("Authorization"),
            "apikey": handler.headers.get("apikey"),
            "range": handler.headers.get("Range"),
            "acceptEncoding": handler.headers.get("Accept-Encoding"),
        })
        if self.redirectTo:
            handler.send_response(302)
            handler.send_header("Location", self.redirectTo)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        route = unquote(parts.path)[len(_OBJECT_PREFIX):]
        if route.startswith("public/"):
            key = route[len("public/"):]
            if self.publicRouteStatus:
                self._sendJson(handler, _NOT_FOUND, sendBody, status=self.publicRouteStatus)
            elif self.publicBucket or key in self.publicKeys:
                self._sendObject(handler, key, sendBody)
            else:
                self._sendJson(handler, _NO_BUCKET, sendBody)
        elif self.anonKey and handler.headers.get("Authorization") == f"Bearer {self.anonKey}":
            self._sendObject(handler, route, sendBody)
        elif not self._authorized(handler):
            self._sendJson(handler, _NO_BUCKET, sendBody)
        else:
            self._sendObject(handler, route, sendBody)

    def _sendJson(self, handler, body: dict, sendBody: bool, status: int = 400) -> None:
        payload = json.dumps(body).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        if sendBody:
            handler.wfile.write(payload)

    def _sendObject(self, handler, key: str, sendBody: bool) -> None:
        if key not in self.objects:
            self._sendJson(handler, _NOT_FOUND, sendBody)
            return
        body = self.objects[key]
        if self.compressUnlessIdentity and handler.headers.get("Accept-Encoding") != "identity":
            self._sendCompressed(handler, body, sendBody)
            return
        status = 200
        requestedRange = handler.headers.get("Range")
        if requestedRange and handler.command == "GET" and not self.ignoreRange:
            start, end = requestedRange.split("=")[1].split("-")
            start, end = int(start), min(int(end) if end else len(body) - 1, len(body) - 1)
            contentRange = f"bytes {start}-{end}/{'*' if self.hideRangeTotal else len(body)}"
            body = body[start:end + 1]
            status = 206
            if self.truncateRanges and len(body) > 1:
                body = body[:-1]
        handler.send_response(status)
        if status == 206:
            handler.send_header("Content-Range", contentRange)
        handler.send_header("Accept-Ranges", "bytes")
        for name, value in self.responseHeaders.items():
            handler.send_header(name, value)
        if not self.omitContentLength:
            handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        if sendBody:
            handler.wfile.write(body)

    def _sendCompressed(self, handler, body: bytes, sendBody: bool) -> None:
        """Answer like an edge that compresses text/plain: gzip, no length and no Range."""
        handler.send_response(200)
        handler.send_header("Content-Encoding", "gzip")
        handler.end_headers()
        if sendBody:
            handler.wfile.write(gzip.compress(body))
