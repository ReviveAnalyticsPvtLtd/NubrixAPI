import pytest
from pydantic import ValidationError

import api.adminModels as adminModels


def _requestModel():
    model = getattr(adminModels, "AdminFreeTrialReductionRequest", None)
    assert model is not None, "trial-reduction request model is missing"
    return model


def _responseModel():
    model = getattr(adminModels, "AdminFreeTrialReductionResponse", None)
    assert model is not None, "trial-reduction response model is missing"
    return model


def test_trial_reduction_request_requires_explicit_confirmation_and_reason():
    requestModel = _requestModel()

    payload = requestModel.model_validate({
        "userId": " user-1 ",
        "days": 5,
        "reason": "  Abuse remediation  ",
        "confirmation": "REDUCE",
    })

    assert payload.userId == "user-1"
    assert payload.days == 5
    assert payload.reason == "Abuse remediation"
    assert payload.confirmation == "REDUCE"

    for invalid in (
        {"userId": "user-1", "days": 5, "reason": "Valid reason"},
        {
            "userId": "user-1",
            "days": 5,
            "reason": "Valid reason",
            "confirmation": "reduce",
        },
        {
            "userId": "user-1",
            "days": 5,
            "reason": "   ",
            "confirmation": "REDUCE",
        },
    ):
        with pytest.raises(ValidationError):
            requestModel.model_validate(invalid)


@pytest.mark.parametrize("days", [0, 31, -1, 1.5, True, "5"])
def test_trial_reduction_request_rejects_days_outside_strict_1_to_30(days):
    with pytest.raises(ValidationError):
        _requestModel().model_validate({
            "userId": "user-1",
            "days": days,
            "reason": "Abuse remediation",
            "confirmation": "REDUCE",
        })


def test_trial_reduction_response_exposes_only_the_safe_result():
    responseModel = _responseModel()
    payload = {
        "reductionId": "reduction-1",
        "userId": "user-1",
        "outcome": "REDUCED",
        "daysRemoved": 5,
        "previousExpiry": "2026-09-10T00:00:00+00:00",
        "newExpiry": "2026-09-05T00:00:00+00:00",
        "accessStillBanned": False,
        "errorCode": None,
    }

    response = responseModel.model_validate(payload)

    assert response.model_dump() == payload
    with pytest.raises(ValidationError):
        responseModel.model_validate({
            **payload,
            "rawSubscription": {"razorpay_token_id": "secret"},
        })
