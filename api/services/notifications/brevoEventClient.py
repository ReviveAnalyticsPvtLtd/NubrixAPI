"""Read-only Brevo transactional event reconciliation client."""

__all__ = ["BrevoEventClient", "ProviderEvent"]


import datetime
import json
import os
import re
from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class ProviderEvent:
    messageId: str
    providerStatus: str
    occurredAt: str
    terminalStatus: str | None
    errorCode: str | None


_EVENT_OUTCOMES = {
    "delivered": ("DELIVERED", None),
    "hard_bounce": ("BOUNCED", "HARD_BOUNCE"),
    "hard_bounces": ("BOUNCED", "HARD_BOUNCE"),
    "hardbounces": ("BOUNCED", "HARD_BOUNCE"),
    "bounces": ("BOUNCED", "HARD_BOUNCE"),
    "blocked": ("BLOCKED", "PROVIDER_BLOCKED"),
    "invalid_email": ("BLOCKED", "INVALID_RECIPIENT"),
    "invalid": ("BLOCKED", "INVALID_RECIPIENT"),
    "spam": ("BLOCKED", "COMPLAINT"),
    "complaint": ("BLOCKED", "COMPLAINT"),
    "unsubscribed": ("BLOCKED", "UNSUBSCRIBED"),
    "error": ("FAILED", "PROVIDER_ERROR"),
    "soft_bounce": (None, "SOFT_BOUNCE"),
    "soft_bounces": (None, "SOFT_BOUNCE"),
    "softbounces": (None, "SOFT_BOUNCE"),
    "deferred": (None, "PROVIDER_DEFERRED"),
}


class BrevoEventClient:
    endpoint = "https://api.brevo.com/v3/smtp/statistics/events"

    def __init__(
        self,
        apiKey: str | None = None,
        requestGet=None,
        timeoutSeconds: int = 10,
    ):
        self.apiKey = apiKey or os.environ.get("BREVO_API_KEY", "")
        self.requestGet = requestGet or requests.get
        self.timeoutSeconds = timeoutSeconds

    def validate(self) -> None:
        self._request({"limit": 1, "sort": "desc", "days": 1})

    def findByMessageId(self, messageId: str) -> ProviderEvent | None:
        return self._newestEvent({
            "messageId": messageId,
            "limit": 50,
            "sort": "desc",
            "days": 3,
        })

    def findByTag(self, trackingTag: str) -> ProviderEvent | None:
        return self._newestEvent({
            "tags": json.dumps([trackingTag], separators=(",", ":")),
            "limit": 50,
            "sort": "desc",
            "days": 3,
        })

    def _newestEvent(self, parameters: dict) -> ProviderEvent | None:
        events = self._request(parameters)
        normalized = [self._normalizeEvent(event) for event in events]
        normalized = [event for event in normalized if event is not None]
        if not normalized:
            return None
        return max(normalized, key=lambda event: self._timestamp(event.occurredAt))

    def _request(self, parameters: dict) -> list[dict]:
        if not self.apiKey:
            raise RuntimeError("BREVO_API_KEY is not configured")
        try:
            response = self.requestGet(
                self.endpoint,
                headers={"api-key": self.apiKey},
                params=parameters,
                timeout=self.timeoutSeconds,
            )
            payload = response.json()
        except (requests.RequestException, ValueError, TypeError):
            raise RuntimeError("BREVO_EVENT_API_FAILED") from None
        if response.status_code >= 300 or not isinstance(payload, dict):
            raise RuntimeError("BREVO_EVENT_API_FAILED")
        events = payload.get("events") or []
        if not isinstance(events, list):
            raise RuntimeError("BREVO_EVENT_API_FAILED")
        return [event for event in events if isinstance(event, dict)]

    @staticmethod
    def _normalizeEvent(event: dict) -> ProviderEvent | None:
        messageId = str(event.get("messageId") or event.get("message-id") or "").strip()
        rawStatus = str(event.get("event") or "").strip()
        providerStatus = re.sub(
            r"(?<!^)(?=[A-Z])",
            "_",
            rawStatus,
        ).lower().replace("-", "_").replace(" ", "_")
        occurredAt = str(event.get("date") or "").strip()
        if not messageId or not providerStatus or not occurredAt:
            return None
        if providerStatus not in _EVENT_OUTCOMES:
            return None
        terminalStatus, errorCode = _EVENT_OUTCOMES[providerStatus]
        return ProviderEvent(
            messageId=messageId,
            providerStatus=providerStatus,
            occurredAt=occurredAt,
            terminalStatus=terminalStatus,
            errorCode=errorCode,
        )

    @staticmethod
    def _timestamp(value: str) -> datetime.datetime:
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc)
