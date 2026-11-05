"""
supabaseClient.py

The backend's Supabase client, with database (PostgREST) traffic that survives a
connection the server has dropped.

The library sends PostgREST requests over HTTP/2. httpcore never checks whether the
server closed an idle HTTP/2 connection before reusing it, so once the gateway drops
one, every request multiplexed on it fails with "Server disconnected" and the API
answers 500. HTTP/1.1 connections are checked before reuse, and each carries one
request. Reads that still meet a dropped connection are retried once.
"""

__all__ = ["ReadRetryTransport", "createSupabaseClient"]

import httpx
from postgrest import SyncPostgrestClient
from postgrest.constants import DEFAULT_POSTGREST_CLIENT_TIMEOUT
from supabase import Client, ClientOptions

from utils.logger import logger

# Reads only. A write whose response was lost may already have been applied, and a
# conditional update retried after it succeeded would report that it matched nothing.
_RETRIED_METHODS = frozenset({"GET", "HEAD"})
_DROPPED_CONNECTION = (httpx.RemoteProtocolError, httpx.ReadError)


class ReadRetryTransport(httpx.BaseTransport):
    """Send a read again, once, on a new connection when the server dropped the old one."""

    def __init__(self, transport: httpx.BaseTransport):
        self._transport = transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return self._transport.handle_request(request)
        except _DROPPED_CONNECTION as exc:
            if request.method not in _RETRIED_METHODS:
                raise
            # The path names the table; the query string can hold tokens, so it is not logged.
            logger.warning("Supabase dropped the connection ({}) for {} {}; retrying once",
                           type(exc).__name__, request.method, request.url.path)
            return self._transport.handle_request(request)

    def close(self) -> None:
        self._transport.close()


def _restHttpClient(timeout, verify: bool) -> httpx.Client:
    # retries=1 covers a failed connect, where nothing reached the server.
    transport = httpx.HTTPTransport(http1=True, http2=False, verify=verify, retries=1)
    return httpx.Client(timeout=timeout, follow_redirects=True, transport=ReadRetryTransport(transport))


class _SupabaseClient(Client):
    # The library rebuilds PostgREST through this hook, including after auth state
    # changes, so every rebuild gets the same connection handling. Storage, Auth and
    # Functions keep the library's own HTTP clients.
    @staticmethod
    def _init_postgrest_client(
        rest_url: str,
        headers: dict[str, str],
        schema: str,
        timeout=DEFAULT_POSTGREST_CLIENT_TIMEOUT,
        verify: bool = True,
        proxy: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> SyncPostgrestClient:
        if proxy is not None:
            raise ValueError("A proxy is not supported for the Supabase REST client")
        # PostgREST sets base_url and headers on the client it is given, so it gets its own.
        return SyncPostgrestClient(rest_url, headers=headers, schema=schema,
                                   http_client=http_client or _restHttpClient(timeout, verify))


def createSupabaseClient(supabaseUrl: str, supabaseKey: str, options: ClientOptions) -> Client:
    """Create a Supabase client whose PostgREST requests survive a dropped connection."""
    return _SupabaseClient.create(supabaseUrl, supabaseKey, options)
