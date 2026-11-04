import datetime
import os

import httpx
import pytest
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt
from supabase import ClientOptions

from test.test_user_access_enforcement import FakeClient
from utils.supabaseClient import ReadRetryTransport, createSupabaseClient


class FlakyTransport(httpx.BaseTransport):
    """Fails the first `failures` requests the way a dropped connection does."""

    def __init__(self, failures, error=httpx.RemoteProtocolError):
        self.failures = failures
        self.error = error
        self.methods = []

    def handle_request(self, request):
        self.methods.append(request.method)
        if len(self.methods) <= self.failures:
            raise self.error("Server disconnected", request=request)
        return httpx.Response(200, json=[{"ok": True}], request=request)


def send(transport, method):
    with httpx.Client(transport=transport, base_url="http://supabase.test") as client:
        return client.request(method, "/rest/v1/Sessions")


@pytest.mark.parametrize("error", [httpx.RemoteProtocolError, httpx.ReadError])
def test_read_is_retried_once_on_a_dropped_connection(error):
    inner = FlakyTransport(failures=1, error=error)

    response = send(ReadRetryTransport(inner), "GET")

    assert response.status_code == 200
    assert inner.methods == ["GET", "GET"]


def test_read_retries_only_once():
    inner = FlakyTransport(failures=2)

    with pytest.raises(httpx.RemoteProtocolError):
        send(ReadRetryTransport(inner), "GET")

    assert inner.methods == ["GET", "GET"]


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE"])
def test_write_is_never_retried(method):
    # The server may already have applied it; a conditional update retried after
    # it succeeded would report that it matched nothing.
    inner = FlakyTransport(failures=1)

    with pytest.raises(httpx.RemoteProtocolError):
        send(ReadRetryTransport(inner), method)

    assert inner.methods == [method]


def resilientClient():
    return createSupabaseClient(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_KEY"],
        ClientOptions(auto_refresh_token=False, persist_session=False),
    )


def assertResilientRestSession(postgrest):
    transport = postgrest.session._transport
    assert isinstance(transport, ReadRetryTransport)
    pool = transport._transport._pool
    # httpcore checks an idle HTTP/1.1 connection for a server-side close before reuse; it has no such check for HTTP/2.
    assert pool._http1 is True
    assert pool._http2 is False


def test_client_sends_rest_traffic_over_http1_with_read_retry():
    client = resilientClient()

    assertResilientRestSession(client.postgrest)
    assert client.options.httpx_client is None  # storage and auth keep their own clients


def test_client_stays_resilient_after_postgrest_is_rebuilt():
    client = resilientClient()
    client.postgrest

    client._postgrest = None  # what the library does on an auth state change

    assertResilientRestSession(client.postgrest)


def test_query_builder_read_survives_a_dropped_connection():
    client = resilientClient()
    flaky = FlakyTransport(failures=1)
    client.postgrest.session._transport._transport = flaky

    rows = client.table("Sessions").select("*").eq("accessToken", "token").execute().data

    assert rows == [{"ok": True}]
    assert flaky.methods == ["GET", "GET"]


def test_query_builder_update_is_not_retried():
    client = resilientClient()
    flaky = FlakyTransport(failures=1)
    client.postgrest.session._transport._transport = flaky

    with pytest.raises(httpx.RemoteProtocolError):
        client.table("Sessions").update({"lastActivity": "now"}).eq("accessToken", "token").execute()

    assert flaky.methods == ["PATCH"]


class DroppingUpdateClient(FakeClient):
    def table(self, tableName):
        query = super().table(tableName)
        execute = query.execute

        def dropOnUpdate():
            if query.operation == "update":
                raise httpx.RemoteProtocolError("Server disconnected")
            return execute()

        query.execute = dropOnUpdate
        return query


def test_failed_last_activity_write_does_not_fail_session_check(monkeypatch):
    from api import commons

    email = "active@example.com"
    token = jwt.encode(
        {
            "userId": "user-1",
            "email": email,
            "sessionStartTime": str(datetime.datetime.now(datetime.timezone.utc)),
        },
        os.environ["SECRET_KEY"],
        "HS256",
    )
    monkeypatch.setattr(commons, "client", DroppingUpdateClient(
        [{"userId": "user-1", "email": email, "isBanned": False}],
        sessions=[{
            "userId": "user-1",
            "email": email,
            "accessToken": token,
            "expiresAt": "2099-01-01T00:00:00+00:00",
        }],
    ))

    verified = commons._verifyTokenInternal(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=token),
        checkExpiry=False,
    )

    assert verified == token
