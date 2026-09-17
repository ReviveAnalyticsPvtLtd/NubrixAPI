from api.services.notifications.brevoEventClient import BrevoEventClient


class FakeResponse:
    def __init__(self, statusCode=200, payload=None):
        self.status_code = statusCode
        self._payload = payload or {"events": []}

    def json(self):
        return self._payload


class RecordingGet:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _client(events):
    requestGet = RecordingGet(FakeResponse(payload={"events": events}))
    client = BrevoEventClient(apiKey="brevo-server-key", requestGet=requestGet)
    return client, requestGet


def test_messageLookupUsesReadOnlyEventEndpointWithoutRecipientFilter():
    client, requestGet = _client([{
        "event": "delivered",
        "messageId": "message-1",
        "date": "2026-09-17T01:02:00Z",
        "email": "private@example.test",
    }])

    event = client.findByMessageId("message-1")

    assert event.terminalStatus == "DELIVERED"
    assert event.messageId == "message-1"
    assert not hasattr(event, "email")
    url, kwargs = requestGet.calls[0]
    assert url == "https://api.brevo.com/v3/smtp/statistics/events"
    assert kwargs["headers"] == {"api-key": "brevo-server-key"}
    assert kwargs["params"] == {
        "messageId": "message-1",
        "limit": 50,
        "sort": "desc",
        "days": 3,
    }
    assert "email" not in kwargs["params"]


def test_tagLookupUsesSerializedTagArray():
    client, requestGet = _client([])

    assert client.findByTag("nubrix_delivery:delivery-1") is None

    assert requestGet.calls[0][1]["params"]["tags"] == (
        '["nubrix_delivery:delivery-1"]'
    )


def test_terminalAndDeferredEventsAreNormalizedWithoutRawReasons():
    cases = [
        ("hard_bounce", "BOUNCED", "HARD_BOUNCE"),
        ("blocked", "BLOCKED", "PROVIDER_BLOCKED"),
        ("invalid_email", "BLOCKED", "INVALID_RECIPIENT"),
        ("spam", "BLOCKED", "COMPLAINT"),
        ("unsubscribed", "BLOCKED", "UNSUBSCRIBED"),
        ("error", "FAILED", "PROVIDER_ERROR"),
        ("soft_bounce", None, "SOFT_BOUNCE"),
        ("deferred", None, "PROVIDER_DEFERRED"),
    ]

    for providerStatus, terminalStatus, errorCode in cases:
        client, _requestGet = _client([{
            "event": providerStatus,
            "messageId": "message-1",
            "date": "2026-09-17T01:02:00Z",
            "reason": "private mailbox details",
        }])
        event = client.findByMessageId("message-1")
        assert event.providerStatus == providerStatus
        assert event.terminalStatus == terminalStatus
        assert event.errorCode == errorCode
        assert "private mailbox details" not in repr(event)


def test_newestEventWinsEvenWhenProviderOrderChanges():
    client, _requestGet = _client([
        {
            "event": "delivered",
            "messageId": "message-1",
            "date": "2026-09-17T01:02:00Z",
        },
        {
            "event": "deferred",
            "messageId": "message-1",
            "date": "2026-09-17T01:03:00Z",
        },
    ])

    event = client.findByMessageId("message-1")

    assert event.providerStatus == "deferred"
    assert event.terminalStatus is None


def test_terminal_delivery_wins_over_later_engagement_event():
    client, _requestGet = _client([
        {
            "event": "delivered",
            "messageId": "message-1",
            "date": "2026-09-17T01:02:00Z",
        },
        {
            "event": "opened",
            "messageId": "message-1",
            "date": "2026-09-17T01:03:00Z",
        },
    ])

    event = client.findByMessageId("message-1")

    assert event.providerStatus == "delivered"
    assert event.terminalStatus == "DELIVERED"


def test_camel_case_brevo_bounce_and_invalid_events_are_normalized():
    cases = [
        ("hardBounces", "BOUNCED", "HARD_BOUNCE"),
        ("softBounces", None, "SOFT_BOUNCE"),
        ("invalid", "BLOCKED", "INVALID_RECIPIENT"),
    ]

    for providerStatus, terminalStatus, errorCode in cases:
        client, _requestGet = _client([{
            "event": providerStatus,
            "messageId": "message-1",
            "date": "2026-09-17T01:02:00Z",
        }])
        event = client.findByMessageId("message-1")
        assert event.terminalStatus == terminalStatus
        assert event.errorCode == errorCode
