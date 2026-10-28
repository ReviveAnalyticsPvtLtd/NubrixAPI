import logging

import pytest
import requests

from api.services.notifications.brevoEmailSender import BrevoEmailSender, EmailSendResult

API_KEY = "test-key-123"
DELIVERY_ID = "11111111-1111-4111-8111-111111111111"
TAG = f"nubrix_delivery:{DELIVERY_ID}"


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


def _trialPayload(**overrides):
    payload = {
        "deliveryId": DELIVERY_ID,
        "notificationType": "trial_expiry_warning",
        "templateVersion": "1",
        "email": "a@b.co",
        "name": "Asha",
        "trialStartDate": "2026-09-07T00:00:00+00:00",
        "trialEndDate": "2026-09-19T00:00:00+00:00",
        "trackingTag": TAG,
    }
    payload.update(overrides)
    return payload


def _billingPayload(**overrides):
    payload = {
        "deliveryId": DELIVERY_ID,
        "notificationType": "payment_receipt",
        "templateVersion": "1",
        "email": "a@b.co",
        "name": "Asha",
        "periodEnd": "2026-11-19T00:00:00+00:00",
        "metadata": {"amount": 4720, "currency": "INR", "invoiceId": "inv_1"},
        "trackingTag": TAG,
    }
    payload.update(overrides)
    return payload


def _sender(post, **overrides):
    settings = dict(
        apiKey=API_KEY,
        senderEmail="noreply@nubrix.test",
        senderName="Nubrix AI",
        appUrl="https://app.nubrix.test",
        requestPost=post,
    )
    settings.update(overrides)
    return BrevoEmailSender(**settings)


def _ok():
    return RecordingPost(response=FakeResponse(201, {"messageId": "<msg-1@brevo>"}))


SEND = {
    "trial": lambda sender: sender.sendTrialExpiry(_trialPayload()),
    "billing": lambda sender: sender.sendBilling(_billingPayload()),
}


@pytest.mark.parametrize("kind", ["trial", "billing"])
def test_acceptedWithMessageId(kind):
    result = SEND[kind](_sender(_ok()))
    assert result == EmailSendResult(outcome="ACCEPTED", messageId="<msg-1@brevo>")


def test_requestShapeMatchesEdgeFunction():
    post = _ok()
    _sender(post).sendTrialExpiry(_trialPayload())
    url, kwargs = post.calls[0]
    assert url == "https://api.brevo.com/v3/smtp/email"
    assert kwargs["headers"]["api-key"] == API_KEY
    assert kwargs["headers"]["accept"] == "application/json"
    assert kwargs["headers"]["content-type"] == "application/json"
    body = kwargs["json"]
    assert body["tags"] == [TAG]
    assert body["headers"] == {"X-Mailin-custom": f"delivery_id:{DELIVERY_ID}"}
    assert body["to"] == [{"email": "a@b.co", "name": "Asha"}]
    assert body["sender"] == {"email": "noreply@nubrix.test", "name": "Nubrix AI"}
    assert body["subject"] and body["htmlContent"]
    assert kwargs["timeout"] == 10


def test_billingRequestShapeCarriesTrackingAndText():
    post = _ok()
    _sender(post, apiBaseUrl="https://brevo.example.test").sendBilling(_billingPayload())
    url, kwargs = post.calls[0]
    assert url == "https://brevo.example.test/v3/smtp/email"
    assert kwargs["json"]["tags"] == [TAG]
    assert kwargs["json"]["headers"] == {"X-Mailin-custom": f"delivery_id:{DELIVERY_ID}"}
    assert kwargs["json"]["textContent"]


@pytest.mark.parametrize("kind", ["trial", "billing"])
def test_timeoutIsAmbiguousForEveryType(kind):
    for error in (requests.Timeout("slow"), requests.ConnectionError("reset")):
        result = SEND[kind](_sender(RecordingPost(error=error)))
        assert (result.outcome, result.errorCode) == ("AMBIGUOUS", "AMBIGUOUS_SEND")


@pytest.mark.parametrize("kind", ["trial", "billing"])
def test_otherRequestExceptionIsRetryable(kind):
    result = SEND[kind](_sender(RecordingPost(error=requests.RequestException("boom"))))
    assert (result.outcome, result.errorCode) == ("RETRYABLE", "EMAIL_REQUEST_FAILED")


@pytest.mark.parametrize("kind,outcome,code", [
    ("trial", "RETRYABLE", "PROVIDER_MESSAGE_ID_MISSING"),
    ("billing", "AMBIGUOUS", "AMBIGUOUS_SEND"),
])
@pytest.mark.parametrize("response", [
    FakeResponse(201, {}),
    FakeResponse(201, {"messageId": "  "}),
    FakeResponse(201, ["not", "a", "dict"]),
    FakeResponse(200, jsonError=ValueError("not json")),
])
def test_successWithoutMessageId(kind, outcome, code, response):
    result = SEND[kind](_sender(RecordingPost(response=response)))
    assert (result.outcome, result.errorCode) == (outcome, code)


@pytest.mark.parametrize("kind", ["trial", "billing"])
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_retryableStatuses(kind, status):
    result = SEND[kind](_sender(RecordingPost(response=FakeResponse(status, {}))))
    assert (result.outcome, result.errorCode) == ("RETRYABLE", f"BREVO_HTTP_{status}")


@pytest.mark.parametrize("kind", ["trial", "billing"])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_otherClientErrorsArePermanent(kind, status):
    result = SEND[kind](_sender(RecordingPost(response=FakeResponse(status, {}))))
    assert (result.outcome, result.errorCode) == ("PERMANENT", f"BREVO_HTTP_{status}")


def test_providerRejectionIsPermanentAndKeyNeverLogged(caplog):
    caplog.set_level(logging.DEBUG)
    for status in (400, 401):
        body = {"code": "unauthorized", "message": f"bad key {API_KEY} for a@b.co"}
        post = RecordingPost(response=FakeResponse(status, body))
        for kind in ("trial", "billing"):
            result = SEND[kind](_sender(post))
            assert result.outcome == "PERMANENT"
            assert API_KEY not in repr(result)
            assert "a@b.co" not in repr(result)
    assert API_KEY not in caplog.text
    assert "a@b.co" not in caplog.text


@pytest.mark.parametrize("kind,field,value,code", [
    ("trial", "deliveryId", "not-a-uuid", "INVALID_DELIVERY_ID"),
    ("trial", "deliveryId", "11111111-1111-1111-8111-111111111111", "INVALID_DELIVERY_ID"),
    ("trial", "email", "nope", "INVALID_RECIPIENT"),
    ("trial", "email", "", "INVALID_RECIPIENT"),
    ("trial", "name", "  ", "INVALID_RECIPIENT_NAME"),
    ("trial", "trackingTag", "nubrix_delivery:other", "INVALID_TRACKING_TAG"),
    ("trial", "trackingTag", "", "INVALID_TRACKING_TAG"),
    ("trial", "templateVersion", "2", "UNSUPPORTED_TEMPLATE_VERSION"),
    ("trial", "notificationType", "payment_receipt", "UNSUPPORTED_NOTIFICATION_TYPE"),
    ("trial", "trialStartDate", "garbage", "INVALID_TRIAL_START"),
    ("trial", "trialStartDate", "", "INVALID_TRIAL_START"),
    ("trial", "trialEndDate", "garbage", "INVALID_TRIAL_END"),
    ("trial", "trialEndDate", "2026-09-07T00:00:00+00:00", "INVALID_TRIAL_END"),
    ("billing", "deliveryId", "not-a-uuid", "INVALID_DELIVERY_ID"),
    ("billing", "email", "nope", "INVALID_RECIPIENT"),
    ("billing", "name", "", "INVALID_RECIPIENT_NAME"),
    ("billing", "trackingTag", "nubrix_delivery:other", "INVALID_TRACKING_TAG"),
    ("billing", "templateVersion", "2", "UNSUPPORTED_TEMPLATE_VERSION"),
    ("billing", "notificationType", "trial_expiry_warning", "UNSUPPORTED_NOTIFICATION_TYPE"),
    ("billing", "notificationType", "mystery", "UNSUPPORTED_NOTIFICATION_TYPE"),
    ("billing", "periodEnd", "garbage", "INVALID_PERIOD_END"),
    ("billing", "metadata", {"amount": -1, "currency": "INR"}, "INVALID_AMOUNT"),
])
def test_invalidPayloadIsPermanentWithoutHttpCall(kind, field, value, code):
    post = _ok()
    sender = _sender(post)
    if kind == "trial":
        result = sender.sendTrialExpiry(_trialPayload(**{field: value}))
    else:
        result = sender.sendBilling(_billingPayload(**{field: value}))
    assert (result.outcome, result.errorCode) == ("PERMANENT", code)
    assert post.calls == []


@pytest.mark.parametrize("kind", ["trial", "billing"])
def test_nonDictPayloadIsInvalidRequestBody(kind):
    sender = _sender(_ok())
    send = sender.sendTrialExpiry if kind == "trial" else sender.sendBilling
    for bad in (None, [], "text"):
        result = send(bad)
        assert (result.outcome, result.errorCode) == ("PERMANENT", "INVALID_REQUEST_BODY")


def test_validateRejectsMissingOrUnsafeConfiguration():
    def never(*args, **kwargs):
        raise AssertionError("validate must not call HTTP")

    for override in (
        {"apiKey": ""}, {"senderEmail": ""}, {"senderName": ""}, {"appUrl": ""},
        {"appUrl": "https://u:p@x.test"}, {"appUrl": "ftp://x.test"},
    ):
        sender = _sender(never)
        # Blank constructor values would fall back to the environment; pin them empty.
        for key, value in override.items():
            setattr(sender, key, value)
        with pytest.raises(RuntimeError, match="^EMAIL_CONFIGURATION_MISSING$"):
            sender.validate()
    _sender(never).validate()


def test_defaultsComeFromEnvironment(monkeypatch):
    monkeypatch.setenv("BREVO_API_KEY", API_KEY)
    monkeypatch.setenv("BREVO_SENDER_EMAIL", "env@nubrix.test")
    monkeypatch.setenv("BREVO_SENDER_NAME", "Env Sender")
    monkeypatch.setenv("DASHBOARD_BASE_URL", "https://env.nubrix.test")
    monkeypatch.delenv("BREVO_API_BASE_URL", raising=False)
    sender = BrevoEmailSender(requestPost=_ok())
    sender.validate()
    assert sender.apiBaseUrl == "https://api.brevo.com"
    assert sender.senderEmail == "env@nubrix.test"


@pytest.mark.parametrize("kind", ["trial", "billing"])
def test_sendBeforeValidateStillRequiresConfiguration(kind):
    post = _ok()
    sender = _sender(post)
    sender.apiKey = ""
    with pytest.raises(RuntimeError, match="^EMAIL_CONFIGURATION_MISSING$"):
        SEND[kind](sender)
    assert post.calls == []
