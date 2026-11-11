"""Read bounded JSON snapshots from an approved public HTTPS origin."""

import http.client
import ipaddress
import json
import math
import os
import queue
import re
import socket
import ssl
import threading
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit


MAX_PAGES = 100
MAX_ROWS = 100_000
MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_RUN_BYTES = 50 * 1024 * 1024
MAX_PREVIEW_BYTES = 1024 * 1024
MAX_PREVIEW_ROWS = 20
MAX_FIELD_BYTES = 64 * 1024
REQUEST_SECONDS = 30
RUN_SECONDS = 5 * 60

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_PATH_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,127}\Z")
_HEADER = re.compile(r"[A-Za-z][A-Za-z0-9-]{0,63}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z")
_TYPES = frozenset({"string", "integer", "number", "boolean", "date", "timestamp"})
_SECRET_PARAMS = frozenset({"token", "accesstoken", "refreshtoken", "apikey", "key", "password", "passwd",
                            "secret", "clientsecret", "signature", "authorization", "credential"})
_RESERVED_HEADERS = frozenset({"host", "connection", "content-length", "transfer-encoding", "te", "trailer",
                               "upgrade", "expect", "accept", "accept-encoding", "cookie", "forwarded"})
_NATIVE_IPV6 = ipaddress.IPv6Network("2000::/3")
_IPV6_TRANSITION = (ipaddress.IPv6Network("2001::/32"), ipaddress.IPv6Network("2002::/16"))
_DNS_SLOTS = threading.BoundedSemaphore(8)
_MISSING = object()


class IngestionError(Exception):
    """An error safe to put in a run result or return to a project owner."""

    def __init__(self, code: str, *, retryable: bool = False, retryAfter: int | None = None):
        self.code = code
        self.retryable = retryable
        self.retryAfter = retryAfter
        super().__init__(code)


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise IngestionError("UPSTREAM_TIMEOUT", retryable=True)
    return seconds


def _endpoint(config):
    endpoint = config.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2048:
        raise IngestionError("INVALID_ENDPOINT")
    if any(not 33 <= ord(char) <= 126 for char in endpoint) or "\\" in endpoint or "#" in endpoint:
        raise IngestionError("INVALID_ENDPOINT")
    try:
        parts = urlsplit(endpoint)
        query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=50)
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.port not in (None, 443)
                or parts.path.startswith("//")):
            raise IngestionError("INVALID_ENDPOINT")
    except ValueError:
        raise IngestionError("INVALID_ENDPOINT") from None
    if any(re.sub(r"[^a-z0-9]", "", key.lower()) in _SECRET_PARAMS for key, _ in query):
        raise IngestionError("INVALID_ENDPOINT")
    allowed = {host.strip().lower() for host in os.environ.get("API_INGESTION_ALLOWED_HOSTS", "").split(",")}
    if parts.hostname not in allowed:
        raise IngestionError("DESTINATION_BLOCKED")
    return parts, query


def _path(value, *, root=False):
    if not isinstance(value, str) or len(value) > 512:
        return False
    if value == "":
        return root
    pieces = value.split(".")
    return len(pieces) <= 8 and all(_PATH_PART.fullmatch(piece) for piece in pieces)


def validateConfig(config: dict) -> None:
    """Validate stored input without contacting the provider."""
    if not isinstance(config, dict):
        raise IngestionError("INVALID_CONFIG")
    _, query = _endpoint(config)
    header = config.get("authHeader", "Authorization")
    if (not isinstance(header, str) or not _HEADER.fullmatch(header)
            or header.lower() in _RESERVED_HEADERS or header.lower().startswith(("proxy-", "x-forwarded-"))):
        raise IngestionError("INVALID_CONFIG")
    if not _path(config.get("recordsPath", ""), root=True):
        raise IngestionError("INVALID_CONFIG")
    nextPath = config.get("nextCursorPath")
    param = config.get("cursorQueryParam")
    if nextPath or param:
        if (not _path(nextPath) or not isinstance(param, str) or not _PATH_PART.fullmatch(param)
                or any(key == param for key, _ in query)):
            raise IngestionError("INVALID_CONFIG")
    if not isinstance(config.get("allowEmpty", False), bool):
        raise IngestionError("INVALID_CONFIG")
    columns = config.get("columns", [])
    if not isinstance(columns, list) or len(columns) > 100:
        raise IngestionError("INVALID_CONFIG")
    names = set()
    for column in columns:
        if not isinstance(column, dict):
            raise IngestionError("INVALID_CONFIG")
        name, columnType = column.get("name"), column.get("type")
        if (not isinstance(name, str) or not _NAME.fullmatch(name) or name in names
                or not isinstance(columnType, str) or columnType not in _TYPES
                or not _path(column.get("path")) or not isinstance(column.get("nullable", False), bool)):
            raise IngestionError("INVALID_CONFIG")
        names.add(name)


def _resolvePeer(host, deadline):
    # A timed-out libc DNS lookup can finish in the background. Bound the number
    # of outstanding resolutions so repeated timeouts cannot grow worker threads.
    if not _DNS_SLOTS.acquire(blocking=False):
        raise IngestionError("UPSTREAM_UNAVAILABLE", retryable=True)
    results = queue.SimpleQueue()

    def resolve():
        try:
            results.put(socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM, socket.IPPROTO_TCP))
        except OSError:
            results.put([])
        finally:
            _DNS_SLOTS.release()

    try:
        threading.Thread(target=resolve, daemon=True).start()
    except RuntimeError:
        _DNS_SLOTS.release()
        raise IngestionError("UPSTREAM_UNAVAILABLE", retryable=True) from None
    try:
        addresses = results.get(timeout=_remaining(deadline))
    except queue.Empty:
        raise IngestionError("UPSTREAM_TIMEOUT", retryable=True) from None
    if not addresses:
        raise IngestionError("UPSTREAM_UNAVAILABLE", retryable=True)
    for family, _, _, _, address in addresses:
        try:
            ip = ipaddress.ip_address(address[0])
        except ValueError:
            raise IngestionError("DESTINATION_BLOCKED") from None
        if family not in (socket.AF_INET, socket.AF_INET6) or not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise IngestionError("DESTINATION_BLOCKED")
        if ip.version == 6 and (ip not in _NATIVE_IPV6 or any(ip in block for block in _IPV6_TRANSITION)):
            raise IngestionError("DESTINATION_BLOCKED")
    family, _, _, _, address = addresses[0]
    return family, address


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, peer, deadline):
        super().__init__(host, 443, timeout=_remaining(deadline), context=ssl.create_default_context())
        self._peer = peer
        self._deadline = deadline
        self._transport = None
        self._transportLock = threading.Lock()

    def connect(self):
        family, address = self._peer
        with self._transportLock:
            seconds = _remaining(self._deadline)
            raw = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
            self.sock = self._transport = raw
            raw.settimeout(seconds)
        raw.connect(address)
        with self._transportLock:
            _remaining(self._deadline)
            tls = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
            self.sock = self._transport = tls
            tls.settimeout(_remaining(self._deadline))
        tls.do_handshake()

    def expire(self):
        with self._transportLock:
            if self._transport is not None:
                try:
                    self._transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def _retryAfter(value):
    if not value:
        return None
    if re.fullmatch(r"[0-9]{1,10}", value):
        return int(value)
    try:
        timestamp = parsedate_to_datetime(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return max(0, math.ceil((timestamp - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return None


def _readPage(host, target, headers, deadline, limit):
    peer = _resolvePeer(host, deadline)
    connection = _PinnedHTTPSConnection(host, peer, deadline)
    timer = threading.Timer(_remaining(deadline), connection.expire)
    timer.daemon = True
    response = None
    try:
        timer.start()
        connection.request("GET", target, headers=headers)
        response = connection.getresponse()
        _remaining(deadline)
        status = response.status
        if 300 <= status < 400:
            raise IngestionError("UNEXPECTED_REDIRECT")
        if status in (401, 403):
            raise IngestionError("AUTH_FAILED")
        if status == 429:
            raise IngestionError("RATE_LIMITED", retryable=True, retryAfter=_retryAfter(response.getheader("Retry-After")))
        if status in (408, 425) or 500 <= status < 600:
            raise IngestionError("UPSTREAM_UNAVAILABLE", retryable=True)
        if status != 200:
            raise IngestionError("UPSTREAM_REJECTED")
        if (response.getheader("Content-Encoding") or "identity").strip().lower() != "identity":
            raise IngestionError("UNSUPPORTED_ENCODING")
        lengthHeader = response.getheader("Content-Length")
        length = None
        if lengthHeader is not None:
            if not re.fullmatch(r"[0-9]{1,20}", lengthHeader.strip()):
                raise IngestionError("INVALID_RESPONSE")
            length = int(lengthHeader)
            if length > limit:
                raise IngestionError("RESPONSE_TOO_LARGE")
        body = bytearray()
        while True:
            _remaining(deadline)
            block = response.read(min(64 * 1024, limit + 1 - len(body)))
            if not block:
                break
            body.extend(block)
            if len(body) > limit:
                raise IngestionError("RESPONSE_TOO_LARGE")
        _remaining(deadline)
        if length is not None and len(body) != length:
            raise IngestionError("INCOMPLETE_RESPONSE", retryable=True)
        return body
    except TimeoutError:
        raise IngestionError("UPSTREAM_TIMEOUT", retryable=True) from None
    except ssl.SSLCertVerificationError:
        raise IngestionError("TLS_FAILED") from None
    except http.client.IncompleteRead:
        _remaining(deadline)
        raise IngestionError("INCOMPLETE_RESPONSE", retryable=True) from None
    except (OSError, http.client.HTTPException):
        _remaining(deadline)
        raise IngestionError("UPSTREAM_UNAVAILABLE", retryable=True) from None
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        connection.close()


def _getPath(root, path):
    value = root
    if not path:
        return value
    for field in path.split("."):
        if not isinstance(value, dict) or field not in value:
            return _MISSING
        value = value[field]
    return value


def _mapValue(value, column):
    if value is _MISSING or value is None:
        if column.get("nullable", False):
            return None
        raise IngestionError("INVALID_RECORD")
    columnType = column["type"]
    if columnType == "string" and isinstance(value, str):
        try:
            if len(value.encode("utf-8")) <= MAX_FIELD_BYTES:
                return value
        except UnicodeEncodeError:
            pass
    if columnType == "boolean" and isinstance(value, bool):
        return value
    if columnType == "integer" and type(value) is int and -(2**63) <= value < 2**63:
        return value
    if columnType == "number" and type(value) in (int, float):
        try:
            if math.isfinite(value):
                return float(value)
        except OverflowError:
            pass
    if isinstance(value, str):
        try:
            if columnType == "date" and _DATE.fullmatch(value):
                return date.fromisoformat(value).isoformat()
            if columnType == "timestamp" and _TIMESTAMP.fullmatch(value):
                return datetime.fromisoformat(value).astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            pass
    raise IngestionError("INVALID_RECORD")


def _rejectConstant(_value):
    raise ValueError("Non-finite JSON number")


def _finiteFloat(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Non-finite JSON number")
    return parsed


def _nextCursor(payload, path, seen):
    value = _getPath(payload, path)
    if value is None or value == "":
        return None
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or len(value) > 512:
        raise IngestionError("INVALID_CURSOR")
    if value in seen:
        raise IngestionError("REPEATED_CURSOR")
    seen.add(value)
    return value


def extract(config: dict, secret: str, *, preview: bool = False) -> list[dict]:
    """Return a complete snapshot or a bounded sample from the first page."""
    validateConfig(config)
    endpoint, query = _endpoint(config)
    columns = config.get("columns", [])
    if not columns and not preview:
        raise IngestionError("INVALID_CONFIG")
    if (not isinstance(secret, str) or not secret or len(secret) > 4096
            or any(not 33 <= ord(char) <= 126 for char in secret)):
        raise IngestionError("INVALID_CREDENTIAL")
    header = config.get("authHeader", "Authorization")
    headers = {"Accept": "application/json", "Accept-Encoding": "identity", "Connection": "close",
               header: f"Bearer {secret}" if header.lower() == "authorization" else secret}
    deadline = time.monotonic() + RUN_SECONDS
    nextPath = config.get("nextCursorPath")
    rows, seen = [], set()
    cursor = None
    bytesRead = mappedBytes = 0
    for _ in range(1 if preview else MAX_PAGES):
        params = query if cursor is None else [*query, (config["cursorQueryParam"], cursor)]
        target = endpoint.path or "/"
        if params:
            try:
                target += "?" + urlencode(params)
            except UnicodeEncodeError:
                raise IngestionError("INVALID_CURSOR") from None
        limit = min(MAX_PAGE_BYTES, MAX_RUN_BYTES - bytesRead, MAX_PREVIEW_BYTES if preview else MAX_PAGE_BYTES)
        body = _readPage(endpoint.hostname, target, headers, min(deadline, time.monotonic() + REQUEST_SECONDS), limit)
        bytesRead += len(body)
        try:
            payload = json.loads(body.decode("utf-8"), parse_constant=_rejectConstant, parse_float=_finiteFloat)
        except (ValueError, RecursionError):
            raise IngestionError("INVALID_JSON") from None
        records = _getPath(payload, config.get("recordsPath", ""))
        if not isinstance(records, list):
            raise IngestionError("INVALID_RECORDS_PATH")
        if preview:
            records = records[:MAX_PREVIEW_ROWS]
        elif len(rows) + len(records) > MAX_ROWS:
            raise IngestionError("ROW_LIMIT")
        for record in records:
            _remaining(deadline)
            if not isinstance(record, dict):
                raise IngestionError("INVALID_RECORD")
            if columns:
                mapped = {column["name"]: _mapValue(_getPath(record, column["path"]), column) for column in columns}
                mappedBytes += len(json.dumps(mapped, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                if mappedBytes > MAX_RUN_BYTES:
                    raise IngestionError("MAPPED_SIZE_LIMIT")
                rows.append(mapped)
            else:
                try:
                    json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")
                except (ValueError, RecursionError):
                    raise IngestionError("INVALID_JSON") from None
                rows.append(record)
        if preview or not nextPath:
            break
        cursor = _nextCursor(payload, nextPath, seen)
        if cursor is None:
            break
    else:
        raise IngestionError("PAGE_LIMIT")
    if not preview and not rows and not config.get("allowEmpty", False):
        raise IngestionError("EMPTY_SNAPSHOT")
    return rows
