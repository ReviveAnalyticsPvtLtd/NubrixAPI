"""Backend transactional email sender: renders locally and posts to the Brevo SMTP API."""

__all__ = ["BrevoEmailSender", "EmailSendResult"]


import datetime
import os
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import requests

from api.services.notifications.emailTemplates import (
    BILLING_TYPES,
    RenderedEmail,
    renderBillingMessage,
    renderFreeTrialWelcome,
    renderTrialExpiryWarning,
)
from utils.logger import logger


_TRIAL_TYPE = "trial_expiry_warning"
_TEMPLATE_VERSION = "1"
_UUID_V4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", re.IGNORECASE
)
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")


@dataclass(frozen=True)
class EmailSendResult:
    outcome: Literal["ACCEPTED", "RETRYABLE", "PERMANENT", "AMBIGUOUS"]
    messageId: str | None = None
    errorCode: str | None = None


class BrevoEmailSender:
    def __init__(
        self,
        apiKey: str | None = None,
        senderEmail: str | None = None,
        senderName: str | None = None,
        appUrl: str | None = None,
        apiBaseUrl: str | None = None,
        requestPost=None,
        timeoutSeconds: int = 10,
    ):
        self.apiKey = apiKey or os.environ.get("BREVO_API_KEY", "")
        self.senderEmail = senderEmail or os.environ.get("BREVO_SENDER_EMAIL", "")
        self.senderName = senderName or os.environ.get("BREVO_SENDER_NAME", "")
        self.appUrl = appUrl or os.environ.get("DASHBOARD_BASE_URL", "")
        self.apiBaseUrl = (
            apiBaseUrl or os.environ.get("BREVO_API_BASE_URL") or "https://api.brevo.com"
        ).rstrip("/")
        self.requestPost = requestPost or requests.post
        self.timeoutSeconds = timeoutSeconds

    def validate(self) -> None:
        """Local configuration check only; makes no HTTP call."""
        self._requireConfiguration()

    def sendTrialExpiry(self, payload: dict) -> EmailSendResult:
        self._requireConfiguration()
        try:
            request = self._parseTrial(payload)
            rendered = renderTrialExpiryWarning(
                name=request["name"],
                trialEndDate=request["trialEndDate"],
                appUrl=self.appUrl,
                senderName=self.senderName,
            )
        except ValueError as error:
            return self._permanent(error)
        return self._send(request, rendered, billing=False)

    def sendBilling(self, payload: dict) -> EmailSendResult:
        self._requireConfiguration()
        try:
            request = self._parseBilling(payload)
            rendered = renderBillingMessage(
                notificationType=request["notificationType"],
                name=request["name"],
                metadata=request["metadata"],
                appUrl=self.appUrl,
            )
        except ValueError as error:
            return self._permanent(error)
        return self._send(request, rendered, billing=True)

    def sendFreeTrialWelcome(self, email: str, name: str, trialEndsAt, experts) -> EmailSendResult:
        self._requireConfiguration()
        try:
            email = str(email or "").strip()
            if not _EMAIL.fullmatch(email):
                raise ValueError("INVALID_RECIPIENT")
            rendered = renderFreeTrialWelcome(
                name=name, trialEndsAt=trialEndsAt, experts=experts, appUrl=self.appUrl
            )
        except ValueError as error:
            return self._permanent(error)
        return self._send(
            {"email": email, "name": str(name).strip()}, rendered, billing=False
        )

    def _send(self, request: dict, rendered: RenderedEmail, *, billing: bool) -> EmailSendResult:
        """Tracking (``trackingTag``/``deliveryId``) is optional: untracked mail omits tags and headers."""
        deliveryId = request.get("deliveryId")
        body = {
            "sender": {"email": self.senderEmail, "name": self.senderName},
            "to": [{"email": request["email"], "name": request["name"]}],
            "subject": rendered.subject,
            "htmlContent": rendered.htmlContent,
        }
        if deliveryId:
            body["tags"] = [request["trackingTag"]]
            body["headers"] = {"X-Mailin-custom": f"delivery_id:{deliveryId}"}
        if rendered.textContent:
            body["textContent"] = rendered.textContent
        try:
            response = self.requestPost(
                f"{self.apiBaseUrl}/v3/smtp/email",
                json=body,
                headers={
                    "api-key": self.apiKey,
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                timeout=self.timeoutSeconds,
            )
        except (requests.Timeout, requests.ConnectionError):
            logger.warning(f"Brevo send outcome uncertain for delivery {deliveryId or 'untracked'}")
            return EmailSendResult(outcome="AMBIGUOUS", errorCode="AMBIGUOUS_SEND")
        except requests.RequestException:
            logger.warning(f"Brevo send request failed for delivery {deliveryId or 'untracked'}")
            return EmailSendResult(outcome="RETRYABLE", errorCode="EMAIL_REQUEST_FAILED")

        status = int(response.status_code)
        if 200 <= status < 300:
            messageId = self._messageId(response)
            if messageId:
                return EmailSendResult(outcome="ACCEPTED", messageId=messageId)
            logger.warning(f"Brevo accepted delivery {deliveryId or 'untracked'} without a message id")
            if billing:
                return EmailSendResult(outcome="AMBIGUOUS", errorCode="AMBIGUOUS_SEND")
            return EmailSendResult(outcome="RETRYABLE", errorCode="PROVIDER_MESSAGE_ID_MISSING")

        logger.warning(f"Brevo rejected delivery {deliveryId or 'untracked'} with status {status}")
        errorCode = self._safeErrorCode(f"BREVO_HTTP_{status}")
        if status in {408, 429} or status >= 500:
            return EmailSendResult(outcome="RETRYABLE", errorCode=errorCode)
        return EmailSendResult(outcome="PERMANENT", errorCode=errorCode)

    def _requireConfiguration(self) -> None:
        if not (self.apiKey and self.senderEmail and self.senderName and self.appUrl):
            raise RuntimeError("EMAIL_CONFIGURATION_MISSING")
        try:
            parts = urlsplit(self.appUrl)
            valid = (
                parts.scheme in ("http", "https")
                and bool(parts.hostname)
                and not parts.username
                and not parts.password
            )
        except ValueError:
            valid = False
        if not valid:
            raise RuntimeError("EMAIL_CONFIGURATION_MISSING")

    @staticmethod
    def _messageId(response) -> str:
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("messageId") or "").strip()

    @staticmethod
    def _permanent(error: ValueError) -> EmailSendResult:
        return EmailSendResult(
            outcome="PERMANENT", errorCode=BrevoEmailSender._safeErrorCode(str(error))
        )

    @staticmethod
    def _safeErrorCode(candidate: str) -> str:
        candidate = str(candidate or "").strip().upper()
        if re.fullmatch(r"[A-Z0-9_]{1,64}", candidate):
            return candidate
        return "EMAIL_PAYLOAD_INVALID"

    @staticmethod
    def _text(body: dict, field: str, errorCode: str) -> str:
        value = body.get(field)
        value = "" if value is None else str(value).strip()
        if not value:
            raise ValueError(errorCode)
        return value

    @staticmethod
    def _date(value: str, errorCode: str) -> datetime.datetime:
        try:
            parsed = datetime.datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(errorCode) from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed

    @classmethod
    def _parseCommon(cls, body, *, billing: bool) -> dict:
        """Mirrors the shared checks of the edge contracts, in their order."""
        if not isinstance(body, dict):
            raise ValueError("INVALID_REQUEST_BODY")
        notificationType = body.get("notificationType")
        allowed = BILLING_TYPES if billing else (_TRIAL_TYPE,)
        if notificationType not in allowed:
            raise ValueError("UNSUPPORTED_NOTIFICATION_TYPE")
        if str(body.get("templateVersion") or "") != _TEMPLATE_VERSION:
            raise ValueError("UNSUPPORTED_TEMPLATE_VERSION")
        deliveryId = str(body.get("deliveryId") or "").strip()
        if not _UUID_V4.fullmatch(deliveryId):
            raise ValueError("INVALID_DELIVERY_ID")
        email = str(body.get("email") or "").strip()
        if not _EMAIL.fullmatch(email):
            raise ValueError("INVALID_RECIPIENT")
        name = cls._text(body, "name", "INVALID_RECIPIENT_NAME")
        return {
            "deliveryId": deliveryId,
            "notificationType": notificationType,
            "email": email,
            "name": name,
        }

    @classmethod
    def _checkTag(cls, body: dict, request: dict) -> None:
        trackingTag = str(body.get("trackingTag") or "").strip()
        if trackingTag != f"nubrix_delivery:{request['deliveryId']}":
            raise ValueError("INVALID_TRACKING_TAG")
        request["trackingTag"] = trackingTag

    @classmethod
    def _parseTrial(cls, body) -> dict:
        request = cls._parseCommon(body, billing=False)
        start = cls._text(body, "trialStartDate", "INVALID_TRIAL_START")
        startDate = cls._date(start, "INVALID_TRIAL_START")
        end = cls._text(body, "trialEndDate", "INVALID_TRIAL_END")
        endDate = cls._date(end, "INVALID_TRIAL_END")
        if endDate <= startDate:
            raise ValueError("INVALID_TRIAL_END")
        cls._checkTag(body, request)
        request["trialEndDate"] = end
        return request

    @classmethod
    def _parseBilling(cls, body) -> dict:
        request = cls._parseCommon(body, billing=True)
        cls._date(str(body.get("periodEnd") or ""), "INVALID_PERIOD_END")
        cls._checkTag(body, request)
        metadata = body.get("metadata")
        request["metadata"] = metadata if isinstance(metadata, dict) else {}
        return request
