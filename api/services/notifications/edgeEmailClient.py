"""HTTP client for the versioned expiry-warning Edge Function."""

__all__ = ["EdgeEmailClient", "EdgeSendResult"]


import os
import re
from dataclasses import dataclass
from typing import Literal

import requests


@dataclass(frozen=True)
class EdgeSendResult:
    outcome: Literal["ACCEPTED", "RETRYABLE", "PERMANENT", "AMBIGUOUS"]
    messageId: str | None = None
    errorCode: str | None = None


class EdgeEmailClient:
    def __init__(
        self,
        edgeUrl: str | None = None,
        apiKey: str | None = None,
        requestPost=None,
        timeoutSeconds: int = 10,
    ):
        self.edgeUrl = edgeUrl or os.environ.get(
            "FREE_TRIAL_EXPIRY_WARNING_EMAIL_URL", ""
        )
        self.apiKey = apiKey or os.environ.get("SUPABASE_KEY_OLD", "")
        self.requestPost = requestPost or requests.post
        self.timeoutSeconds = timeoutSeconds

    def validate(self) -> None:
        self._requireConfiguration()
        try:
            response = self.requestPost(
                self.edgeUrl,
                json={
                    "mode": "validate",
                    "notificationType": "trial_expiry_warning",
                    "templateVersion": "1",
                },
                headers=self._headers(),
                timeout=self.timeoutSeconds,
            )
            payload = response.json()
        except (requests.RequestException, ValueError, TypeError):
            raise RuntimeError("EDGE_VALIDATION_FAILED") from None

        if response.status_code >= 300 or payload != {
            "status": "ok",
            "notificationTypes": ["trial_expiry_warning"],
            "templateVersions": ["1"],
        }:
            raise RuntimeError("EDGE_VALIDATION_FAILED")

    def sendTrialExpiry(self, payload: dict, *, billing=False) -> EdgeSendResult:
        self._requireConfiguration()
        try:
            response = self.requestPost(
                self.edgeUrl,
                json=payload,
                headers=self._headers(),
                timeout=self.timeoutSeconds,
            )
        except (requests.Timeout, requests.ConnectionError):
            return EdgeSendResult(
                outcome="AMBIGUOUS",
                errorCode="AMBIGUOUS_SEND",
            )
        except requests.RequestException:
            return EdgeSendResult(
                outcome="RETRYABLE",
                errorCode="EDGE_REQUEST_FAILED",
            )

        responsePayload = self._responsePayload(response)
        status = int(response.status_code)
        if 200 <= status < 300:
            if responsePayload is None:
                return EdgeSendResult(
                    outcome="AMBIGUOUS" if billing else "RETRYABLE",
                    errorCode="AMBIGUOUS_SEND" if billing else "EDGE_RESPONSE_INVALID",
                )
            messageId = str(responsePayload.get("messageId") or "").strip()
            if (
                responsePayload.get("status") != "accepted"
                or responsePayload.get("provider") != "brevo"
                or not messageId
            ):
                return EdgeSendResult(
                    outcome="AMBIGUOUS" if billing else "RETRYABLE",
                    errorCode="AMBIGUOUS_SEND" if billing else "PROVIDER_MESSAGE_ID_MISSING",
                )
            return EdgeSendResult(
                outcome="ACCEPTED",
                messageId=messageId,
            )

        errorCode = self._safeErrorCode(responsePayload, f"EDGE_HTTP_{status}")
        if billing and errorCode == 'AMBIGUOUS_SEND':
            return EdgeSendResult(outcome='AMBIGUOUS',errorCode='AMBIGUOUS_SEND')
        if status in {408, 429} or status >= 500:
            return EdgeSendResult(
                outcome="RETRYABLE",
                errorCode=errorCode,
            )
        return EdgeSendResult(
            outcome="PERMANENT",
            errorCode=errorCode,
        )

    def sendBilling(self, payload: dict) -> EdgeSendResult:
        edgeUrl = os.environ.get("BILLING_NOTIFICATION_EMAIL_URL", "")
        if not edgeUrl:
            raise RuntimeError("BILLING_EDGE_CONFIGURATION_MISSING")
        return EdgeEmailClient(edgeUrl=edgeUrl,apiKey=self.apiKey,
            requestPost=self.requestPost,timeoutSeconds=self.timeoutSeconds).sendTrialExpiry(payload,billing=True)

    def _requireConfiguration(self) -> None:
        if not self.edgeUrl or not self.apiKey:
            raise RuntimeError("EDGE_CONFIGURATION_MISSING")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.apiKey}",
            "apikey": self.apiKey,
            "Content-Type": "application/json",
        }

    @staticmethod
    def _responsePayload(response) -> dict | None:
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _safeErrorCode(payload: dict | None, fallback: str) -> str:
        if payload is None:
            return fallback
        candidate = str(payload.get("errorCode") or "").strip().upper()
        if re.fullmatch(r"[A-Z0-9_]{1,64}", candidate):
            return candidate
        return fallback
