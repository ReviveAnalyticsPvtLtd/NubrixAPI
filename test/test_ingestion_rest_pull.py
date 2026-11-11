import importlib
import io
import json
import socket
import ssl
import time

import pytest


def test_rest_pull_is_available():
    try:
        importlib.import_module("api.services.ingestion.restPull")
    except ModuleNotFoundError:
        pytest.fail("REST ingestion has no extractor")


@pytest.fixture
def restPull():
    return importlib.import_module("api.services.ingestion.restPull")


@pytest.fixture
def config():
    return {
        "endpoint": "https://orders.example.test/v1/orders?region=west",
        "authHeader": "Authorization",
        "recordsPath": "data.orders",
        "nextCursorPath": "data.next",
        "cursorQueryParam": "cursor",
        "columns": [
            {"name": "order_id", "path": "id", "type": "string", "nullable": False},
            {"name": "total", "path": "amount", "type": "integer", "nullable": False},
        ],
    }


def page(rows, cursor=None):
    return 200, json.dumps({"data": {"orders": rows, "next": cursor}}).encode(), {}


@pytest.fixture
def provider(monkeypatch, restPull):
    monkeypatch.setenv("API_INGESTION_ALLOWED_HOSTS", "orders.example.test")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
    ])
    replies, requests = [], []

    class Response(io.BytesIO):
        def __init__(self, status, body, headers):
            super().__init__(body)
            self.status = status
            self.headers = {key.lower(): value for key, value in headers.items()}

        def getheader(self, key):
            return self.headers.get(key.lower())

    class Connection:
        def __init__(self, host, peer, deadline):
            self.host = host
            self.peer = peer

        def request(self, method, path, *, headers):
            requests.append((self.host, self.peer[1], method, path, headers))

        def getresponse(self):
            return Response(*replies.pop(0))

        def close(self):
            pass

        def expire(self):
            pass

    monkeypatch.setattr(restPull, "_PinnedHTTPSConnection", Connection)
    return replies, requests


def test_paginates_on_same_origin_and_preserves_text_ids(restPull, config, provider):
    replies, requests = provider
    replies.extend([page([{"id": "0007", "amount": 9}], "next+a&x=1"), page([{"id": "0008", "amount": 12}])])
    assert restPull.extract(config, "integration-secret") == [
        {"order_id": "0007", "total": 9}, {"order_id": "0008", "total": 12},
    ]
    assert [entry[3] for entry in requests] == [
        "/v1/orders?region=west", "/v1/orders?region=west&cursor=next%2Ba%26x%3D1",
    ]
    assert all(entry[0:3] == ("orders.example.test", ("93.184.216.34", 443), "GET") for entry in requests)
    assert all(entry[4]["Authorization"] == "Bearer integration-secret" for entry in requests)


@pytest.mark.parametrize(("lastPage", "code"), [
    (page([{"id": "0008", "amount": 12}], "two"), "REPEATED_CURSOR"),
    ((200, b'{"data":', {}), "INVALID_JSON"),
    (page([{"id": "0008", "amount": True}]), "INVALID_RECORD"),
    ((200, b'{"data":{"orders":[]}}', {}), "INVALID_CURSOR"),
    ((302, b"", {"Location": "https://somewhere-else.example.test"}), "UNEXPECTED_REDIRECT"),
])
def test_failed_last_page_does_not_return_partial_data(restPull, config, provider, lastPage, code):
    replies, requests = provider
    replies.extend([page([{"id": "0007", "amount": 9}], "two"), lastPage])
    with pytest.raises(restPull.IngestionError) as failure:
        restPull.extract(config, "integration-secret")
    assert failure.value.code == code
    assert len(requests) == 2


def test_refuses_internal_answer_even_when_dns_has_a_public_peer(restPull, config, provider, monkeypatch):
    _, requests = provider
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.0.0.8", 443)),
    ])
    with pytest.raises(restPull.IngestionError) as failure:
        restPull.extract(config, "integration-secret")
    assert failure.value.code == "DESTINATION_BLOCKED"
    assert requests == []


@pytest.mark.parametrize("address", ["64:ff9b::a00:8", "2002:a00:8::1"])
def test_rejects_ipv6_transition_addresses(restPull, config, provider, monkeypatch, address):
    _, requests = provider
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args: [
        (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443, 0, 0)),
    ])
    with pytest.raises(restPull.IngestionError) as failure:
        restPull.extract(config, "integration-secret")
    assert failure.value.code == "DESTINATION_BLOCKED"
    assert requests == []


def test_tls_connects_to_resolved_peer_with_original_hostname(restPull, monkeypatch):
    connected, verified = [], []

    class Transport:
        def settimeout(self, _seconds):
            pass

        def connect(self, address):
            connected.append(address)

        def do_handshake(self):
            pass

        def close(self):
            pass

    transport = Transport()

    class Context:
        verify_mode = ssl.CERT_REQUIRED
        check_hostname = True
        post_handshake_auth = False

        def wrap_socket(self, raw, *, server_hostname, do_handshake_on_connect):
            assert raw is transport
            verified.append(server_hostname)
            return transport

    def unexpectedLookup(*_args):
        pytest.fail("The TLS connection resolved the hostname a second time")

    monkeypatch.setattr(socket, "socket", lambda *_args: transport)
    monkeypatch.setattr(socket, "getaddrinfo", unexpectedLookup)
    monkeypatch.setattr(ssl, "create_default_context", Context)
    connection = restPull._PinnedHTTPSConnection(
        "orders.example.test", (socket.AF_INET, ("93.184.216.34", 443)), time.monotonic() + 5,
    )
    try:
        connection.connect()
    finally:
        connection.close()
    assert connected == [("93.184.216.34", 443)]
    assert verified == ["orders.example.test"]


@pytest.mark.parametrize("endpoint", [
    "http://orders.example.test/v1/orders",
    "https://user:private@orders.example.test/v1/orders",
    "https://orders.example.test:444/v1/orders",
    "https://orders.example.test/v1/orders?token=private",
    "https://orders.example.test.other.test/v1/orders",
])
def test_rejects_unsafe_endpoint_before_request(restPull, config, provider, endpoint):
    _, requests = provider
    config["endpoint"] = endpoint
    with pytest.raises(restPull.IngestionError):
        restPull.extract(config, "integration-secret")
    assert requests == []


def test_rate_limit_reports_retry_after_without_provider_content(restPull, config, provider):
    replies, _ = provider
    replies.append((429, b'{"private":"provider-body"}', {"Retry-After": "90"}))
    with pytest.raises(restPull.IngestionError) as failure:
        restPull.extract(config, "integration-secret")
    assert failure.value.code == "RATE_LIMITED"
    assert failure.value.retryable
    assert failure.value.retryAfter == 90
    assert "provider-body" not in str(failure.value)


def test_preview_caps_sample_without_publishing_or_following_cursor(restPull, config, provider):
    replies, requests = provider
    config["columns"] = []
    replies.append(page([{"id": str(index)} for index in range(30)], "later"))
    assert restPull.extract(config, "integration-secret", preview=True) == [
        {"id": str(index)} for index in range(20)
    ]
    assert len(requests) == 1


@pytest.mark.parametrize("invalid", [b'1e999', b'"\\ud800"'])
def test_preview_rejects_values_the_client_cannot_encode(restPull, config, provider, invalid):
    replies, _ = provider
    config["columns"] = []
    replies.append((200, b'{"data":{"orders":[{"amount":' + invalid + b'}],"next":null}}', {}))
    with pytest.raises(restPull.IngestionError) as failure:
        restPull.extract(config, "integration-secret", preview=True)
    assert failure.value.code == "INVALID_JSON"


def test_full_pull_requires_opt_in_to_empty_result(restPull, config, provider):
    replies, _ = provider
    replies.extend([page([]), page([])])
    with pytest.raises(restPull.IngestionError) as failure:
        restPull.extract(config, "integration-secret")
    assert failure.value.code == "EMPTY_SNAPSHOT"
    config["allowEmpty"] = True
    assert restPull.extract(config, "integration-secret") == []


def test_capped_body_refuses_oversized_response_without_content_length(restPull, config, provider, monkeypatch):
    replies, _ = provider
    monkeypatch.setattr(restPull, "MAX_PAGE_BYTES", 8)
    replies.append(page([{"id": "0007", "amount": 9}]))
    with pytest.raises(restPull.IngestionError) as failure:
        restPull.extract(config, "integration-secret")
    assert failure.value.code == "RESPONSE_TOO_LARGE"
