"""
credits.py

Router for credit balance, usage, and top-up purchase endpoints.

Balance and usage are user-facing reads of the LLM credit state. The top-up
endpoints wrap the Razorpay purchase flow, so they use verifyToken to match
the subscription payment code they delegate to rather than verifyUser.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["router"]


from api.commons import verifyUser, verifyToken, UserContext
from api.models import CreateTopupOrderRequest, VerifyTopupPaymentRequest
from utils.exceptionHandler import CustomException, raiseHttpException
from fastapi.responses import ORJSONResponse
from fastapi import APIRouter, Depends, HTTPException, Header
from utils.logger import logger


router = APIRouter()

_TOPUP_ERROR_STATUS = {
    "TOPUP_NOT_ELIGIBLE": 403,
    "TOPUP_PACK_UNKNOWN": 400,
    "TOPUP_PACK_INACTIVE": 400,
}


@router.get("/balance")
async def getCreditBalance(user: UserContext = Depends(verifyUser)):
    """
    Return the current credit balance for the authenticated user.

    Returns:
        ORJSONResponse: Balance details including remaining/used tokens, the
            monthly token quota, the same figures as derived credits, period
            start/end, and plan tier.
    """
    try:
        from api.services.credits.creditService import creditService
        from api.services.credits.creditPresentation import buildCreditBalanceView

        snapshot = creditService.getBalanceSnapshot(user.userId)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "data": buildCreditBalanceView(snapshot),
            },
        )
    except CustomException:
        raise
    except Exception as e:
        exception = CustomException(e)
        logger.error(exception)
        raiseHttpException(exception)


@router.get("/usage")
async def getCreditUsage(user: UserContext = Depends(verifyUser)):
    """
    Return a summary of recent token usage for the authenticated user.

    Includes Supabase-backed token totals (with credits derived at the ratio in
    config/credits.json) and, when available, a per-operation and per-model
    breakdown from the Langfuse Metrics API.

    Returns:
        ORJSONResponse: Usage summary with token and credit totals and optional
            Langfuse breakdown.
    """
    try:
        from api.services.credits.creditService import creditService
        from api.services.credits.creditPresentation import buildCreditBalanceView
        from api.services.credits.langfuseUsageService import getUsageBreakdown
        from datetime import datetime, timezone

        snapshot = creditService.getBalanceSnapshot(user.userId)

        breakdown = None
        langfuseAvailable = False
        periodStart = snapshot.get("periodStart")
        periodEnd = snapshot.get("periodEnd")

        if snapshot.get("initialized") and periodStart and periodEnd:
            breakdown = getUsageBreakdown(
                userId=user.userId,
                fromTimestamp=periodStart,
                toTimestamp=periodEnd,
            )

        if breakdown is None:
            now = datetime.now(timezone.utc)
            monthStart = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            breakdown = getUsageBreakdown(
                userId=user.userId,
                fromTimestamp=monthStart.isoformat(),
                toTimestamp=now.isoformat(),
            )

        if breakdown is not None:
            langfuseAvailable = breakdown.get("langfuseAvailable", False)

        balanceView = buildCreditBalanceView(snapshot)

        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "data": {
                    **balanceView,
                    "langfuseAvailable": langfuseAvailable,
                    "breakdown": {
                        "byOperation": breakdown.get("byOperation", []) if breakdown else [],
                        "byModel": breakdown.get("byModel", []) if breakdown else [],
                    },
                },
            },
        )
    except CustomException:
        raise
    except Exception as e:
        exception = CustomException(e)
        logger.error(exception)
        raiseHttpException(exception)


def _topupStatusCode(message: str) -> int:
    """
    Map a top-up failure message to an HTTP status code.

    Matches on the markers topupService prefixes onto its failures. Anything
    unrecognised is an unexpected fault and stays a 500.

    Args:
        message (str): The raised exception's message.

    Returns:
        int: 403/400 for a recognised marker, 500 otherwise.
    """
    for marker, code in _TOPUP_ERROR_STATUS.items():
        if marker in (message or ""):
            return code
    return 500


def _topupErrorCode(message: str) -> str:
    """
    Extract the machine-readable error code from a top-up failure message.

    Args:
        message (str): The raised exception's message.

    Returns:
        str: The matched marker, or 'TOPUP_FAILED'.
    """
    for marker in _TOPUP_ERROR_STATUS:
        if marker in (message or ""):
            return marker
    return "TOPUP_FAILED"


def _raiseTopupException(e: Exception) -> None:
    """
    Re-raise a top-up failure with the right status code and errorCode.

    Args:
        e (Exception): The wrapped failure from topupService.

    Raises:
        HTTPException: Always.
    """
    message = str(e)
    raise HTTPException(
        status_code=_topupStatusCode(message),
        detail={
            "status": "FAILURE",
            "message": message.split(": ", 1)[-1] if ": " in message else message,
            "errorCode": _topupErrorCode(message),
        },
    )


@router.get("/topup/packs")
async def getTopupPacks(token=Depends(verifyToken)):
    """
    List the purchasable credit top-up packs.

    Prices are returned for everyone alongside an `eligible` flag, so an
    ineligible user can still be shown what a paid plan would unlock.

    Args:
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Packs with tax-inclusive totals and the eligibility flag.
    """
    try:
        from api.services.credits.topupService import topupService

        result = topupService.listPacks(token=token)
        return ORJSONResponse(
            status_code=200,
            content={"status": "SUCCESS", "data": result},
        )
    except CustomException as e:
        logger.error(e)
        _raiseTopupException(e)


@router.post("/topup/order")
async def createTopupOrder(
    request: CreateTopupOrderRequest,
    token=Depends(verifyToken),
    requestKey: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128)
):
    """
    Create a Razorpay order for a credit top-up pack.

    The client sends only a packId — token counts and prices are resolved
    server-side from config/credits.json. Tokens are granted after payment.

    Args:
        request (CreateTopupOrderRequest): The pack to buy.
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Checkout payload for Razorpay embedded checkout.
    """
    try:
        from api.services.credits.topupService import topupService

        result = topupService.createTopupOrder(packId=request.packId, token=token, requestKey=requestKey)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Credit top-up order created.",
                "data": result
            }
        )
    except CustomException as e:
        logger.error(e)
        _raiseTopupException(e)


@router.post("/topup/verify")
async def verifyTopupPayment(
    payload: VerifyTopupPaymentRequest,
    token=Depends(verifyToken)
):
    """
    Verify a credit top-up checkout signature and credit the purchased tokens.

    Safe to race with the payment.captured webhook: the grant is idempotent,
    and `granted: false` means the webhook already applied it.

    Args:
        payload (VerifyTopupPaymentRequest): Razorpay checkout callback.
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Grant result including the tokens credited.
    """
    try:
        from api.services.credits.topupService import topupService

        result = topupService.verifyTopupPayment(payload=payload.dict(), token=token)
        message = (
            f"{result['credits']} credits added to your balance."
            if result["granted"]
            else "Payment already processed."
        )
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": message,
                "data": result
            }
        )
    except CustomException as e:
        logger.error(e)
        _raiseTopupException(e)
