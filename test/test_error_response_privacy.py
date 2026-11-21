from unittest.mock import Mock

import pytest
from fastapi import HTTPException

import utils.exceptionHandler as exceptionHandler
from utils.exceptionHandler import (
    CustomException,
    raiseFeatureGateHttpException,
    raiseHttpException,
)


@pytest.fixture
def logged(monkeypatch):
    # Patched on the module because other test files stub `utils.logger` for the session.
    recorder = Mock()
    monkeypatch.setattr(exceptionHandler, "logger", recorder, raising=False)
    return recorder


def _failure() -> CustomException:
    try:
        raise ValueError("internal detail")
    except ValueError as exc:
        return CustomException(exc, statusCode=500, uiMessage="Something went wrong.")


def test_error_response_keeps_internal_detail_in_the_logs_only(logged):
    with pytest.raises(HTTPException) as raised:
        raiseHttpException(_failure())
    assert raised.value.detail == {"status": 500, "message": "Something went wrong."}
    assert "internal detail" in str(logged.mock_calls)


def test_feature_gate_response_keeps_internal_detail_in_the_logs_only(logged):
    with pytest.raises(HTTPException) as raised:
        raiseFeatureGateHttpException(403, "Upgrade needed.", "gate refused userId=u1", "PLAN_REQUIRED")
    assert raised.value.detail == {"status": 403, "message": "Upgrade needed.", "errorCode": "PLAN_REQUIRED"}
    assert "gate refused userId=u1" in str(logged.mock_calls)
