import pytest
import requests
from loguru import logger

from api.services.notifications.brevoEmailSender import BrevoEmailSender
from api.services.subscriptions.subscriptionService import SubscriptionService

EMAIL = "secret.person@example.test"
ARGS = {
    "email": EMAIL,
    "name": "Asha",
    "trialEndsAt": "2026-11-09T10:00:00Z",
    "experts": ["banking", "supplychain"],
}


class FakeResponse:
    def __init__(self, statusCode, payload=None):
        self.status_code = statusCode
        self._payload = payload

    def json(self):
        return self._payload


def _post(response=None, error=None):
    def post(url, **kwargs):
        if error is not None:
            raise error
        return response

    return post


def _sender(post):
    return BrevoEmailSender(
        apiKey="test-key-123",
        senderEmail="noreply@nubrix.test",
        senderName="Nubrix AI",
        appUrl="https://app.nubrix.test",
        requestPost=post,
    )


def _run(monkeypatch, post):
    monkeypatch.setattr(
        "api.services.notifications.brevoEmailSender.BrevoEmailSender",
        lambda: _sender(post),
    )
    captured = []
    sinkId = logger.add(captured.append, level="DEBUG")
    try:
        result = SubscriptionService._sendFreeTrialEmail(**ARGS)
    finally:
        logger.remove(sinkId)
    return result, "\n".join(str(m) for m in captured)


def test_acceptedLogsMessageId(monkeypatch):
    result, text = _run(monkeypatch, _post(FakeResponse(201, {"messageId": "m-1"})))
    assert result is None
    assert "Free trial email accepted: m-1" in text
    assert EMAIL not in text


def test_retryableLogsErrorCode(monkeypatch):
    result, text = _run(monkeypatch, _post(FakeResponse(503)))
    assert result is None
    assert "BREVO_HTTP_503" in text
    assert EMAIL not in text


def test_connectionErrorIsAmbiguousAndNeverRaises(monkeypatch):
    result, text = _run(monkeypatch, _post(error=requests.ConnectionError(f"boom {EMAIL}")))
    assert result is None
    assert "AMBIGUOUS_SEND" in text
    assert EMAIL not in text


def test_unexpectedExceptionLogsOnlyTypeName(monkeypatch):
    class Exploding:
        def sendFreeTrialWelcome(self, *args):
            raise requests.ConnectionError(f"boom {EMAIL}")

    monkeypatch.setattr(
        "api.services.notifications.brevoEmailSender.BrevoEmailSender", lambda: Exploding()
    )
    captured = []
    sinkId = logger.add(captured.append, level="DEBUG")
    try:
        result = SubscriptionService._sendFreeTrialEmail(**ARGS)
    finally:
        logger.remove(sinkId)
    text = "\n".join(str(m) for m in captured)
    assert result is None
    assert "ConnectionError" in text
    assert "boom" not in text
    assert EMAIL not in text


def test_missingConfigurationLogsCode(monkeypatch):
    for key in ("BREVO_API_KEY", "BREVO_SENDER_EMAIL", "BREVO_SENDER_NAME", "DASHBOARD_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    captured = []
    sinkId = logger.add(captured.append, level="DEBUG")
    try:
        result = SubscriptionService._sendFreeTrialEmail(**ARGS)
    finally:
        logger.remove(sinkId)
    text = "\n".join(str(m) for m in captured)
    assert result is None
    assert "Free trial email not sent: EMAIL_CONFIGURATION_MISSING" in text
    assert EMAIL not in text
