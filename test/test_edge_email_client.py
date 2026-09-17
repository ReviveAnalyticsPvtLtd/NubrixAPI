import requests

from api.services.notifications.edgeEmailClient import EdgeEmailClient


class FakeResponse:
    def __init__(self, statusCode, payload=None, jsonError=None):
        self.status_code = statusCode
        self._payload = payload
        self._jsonError = jsonError

    def json(self):
        if self._jsonError is not None:
            raise self._jsonError
        return self._payload


class RecordingPost:
    def __init__(self, *, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


def _payload():
    return {
        "deliveryId": "11111111-1111-4111-8111-111111111111",
        "notificationType": "trial_expiry_warning",
        "templateVersion": "1",
        "email": "recipient@example.test",
        "name": "Recipient",
        "trialStartDate": "2026-09-07T00:00:00+00:00",
        "trialEndDate": "2026-09-19T00:00:00+00:00",
        "trackingTag": (
            "nubrix_delivery:11111111-1111-4111-8111-111111111111"
        ),
    }


def _client(post):
    return EdgeEmailClient(
        edgeUrl="https://project.supabase.co/functions/v1/warningEmail",
        apiKey="old-project-server-key",
        requestPost=post,
    )


def test_validateUsesServiceHeadersWithoutRecipientData():
    post = RecordingPost(
        response=FakeResponse(200, {
            "status": "ok",
            "notificationTypes": ["trial_expiry_warning"],
            "templateVersions": ["1"],
        })
    )

    _client(post).validate()

    url, kwargs = post.calls[0]
    assert url.endswith("/warningEmail")
    assert kwargs["headers"]["Authorization"] == "Bearer old-project-server-key"
    assert kwargs["headers"]["apikey"] == "old-project-server-key"
    assert kwargs["json"] == {
        "mode": "validate",
        "notificationType": "trial_expiry_warning",
        "templateVersion": "1",
    }
    assert kwargs["timeout"] == 10


def test_httpSuccessWithoutMessageIdIsRetryable():
    post = RecordingPost(
        response=FakeResponse(200, {"status": "accepted", "provider": "brevo"})
    )

    result = _client(post).sendTrialExpiry(_payload())

    assert result.outcome == "RETRYABLE"
    assert result.errorCode == "PROVIDER_MESSAGE_ID_MISSING"


def test_timeoutAfterTransmissionIsAmbiguousWithoutLeakingMessage():
    post = RecordingPost(error=requests.Timeout("recipient@example.test timed out"))

    result = _client(post).sendTrialExpiry(_payload())

    assert result.outcome == "AMBIGUOUS"
    assert result.errorCode == "AMBIGUOUS_SEND"
    assert "recipient" not in repr(result)


def test_retryableHttpStatusesAreNormalized():
    for status in (408, 429, 500, 502, 503):
        result = _client(RecordingPost(response=FakeResponse(status, {}))).sendTrialExpiry(
            _payload()
        )
        assert result.outcome == "RETRYABLE"
        assert result.errorCode == f"EDGE_HTTP_{status}"


def test_validationErrorsArePermanentAndSanitized():
    result = _client(
        RecordingPost(
            response=FakeResponse(
                400,
                {"errorCode": "INVALID_RECIPIENT", "email": "private@example.test"},
            )
        )
    ).sendTrialExpiry(_payload())

    assert result.outcome == "PERMANENT"
    assert result.errorCode == "INVALID_RECIPIENT"
    assert "private@example.test" not in repr(result)


def test_invalidJsonFromSuccessfulEdgeResponseIsRetryable():
    result = _client(
        RecordingPost(
            response=FakeResponse(200, jsonError=ValueError("not json"))
        )
    ).sendTrialExpiry(_payload())

    assert result.outcome == "RETRYABLE"
    assert result.errorCode == "EDGE_RESPONSE_INVALID"
