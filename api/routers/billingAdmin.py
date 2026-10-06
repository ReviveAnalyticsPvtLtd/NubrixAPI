"""
Admin-only billing reconciliation and observability endpoints.

These endpoints expose billing operational actions for support/engineering:
reconciliation reports, webhook replay, investigation notes, and billing
metric snapshots.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["router"]


from api.models import (
    MarkReconciliationInvestigatedRequest,
    ReplayWebhookEventRequest,
    SubscriptionRefundInitiateRequest,
    SubscriptionRefundQuoteRequest,
)
from api.services.billing.billingMetricsService import BillingMetricsService
from api.services.billing.reconciliationService import ReconciliationService
from api.services.billing.subscriptionRefundService import SubscriptionRefundService
from utils.exceptionHandler import CustomException, raiseHttpException
from fastapi.responses import ORJSONResponse
from fastapi import APIRouter, Depends, Header, HTTPException, status
from api.commons import verifyToken
from utils.logger import logger
from jose import jwt
import os


router = APIRouter()


def verifyBillingAdmin(token=Depends(verifyToken)) -> str:
    """
    Require a valid session token whose JWT userId is explicitly allowlisted.

    Configure `BILLING_ADMIN_USER_IDS` as a comma-separated list of internal
    user IDs. Keeping this allowlist explicit avoids accidentally exposing
    manual billing operations to normal authenticated users.
    """
    allowedUserIds = {
        userId.strip()
        for userId in os.environ.get("BILLING_ADMIN_USER_IDS", "").split(",")
        if userId.strip()
    }
    if not allowedUserIds:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Billing admin access is not configured",
        )
    try:
        decodedToken = jwt.decode(
            token,
            os.environ["SECRET_KEY"],
            algorithms=["HS256"],
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid billing admin token",
        )
    userId = decodedToken.get("userId")
    if userId not in allowedUserIds:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Billing admin access denied",
        )
    return userId


def _loadPaidIntervalsForInvoices(userId: str, invoiceIds: list[str]) -> list[dict]:
    """Resolve the owned paid-service intervals targeted by a refund case.

    Amounts come from each invoice's frozen snapshot; ownership is enforced
    against the target user. Top-ups (billing_reason='add_on') are excluded
    here — their clawback is a separately approved path.
    """
    from api.commons import client
    from api.services.subscriptions.paymentValidationService import parseUtc

    intervals = []
    if not invoiceIds:
        return intervals
    rows = (
        client.table("Invoices")
        .select(
            "id, userId, billing_reason, period_start, period_end, "
            "total_amount, amount, currency, status, metadata_json, "
            "razorpayPaymentId"
        )
        .eq("userId", userId)
        .in_("id", invoiceIds)
        .execute()
        .data
    )
    byId = {row["id"]: row for row in rows or []}
    for invoiceId in invoiceIds:
        row = byId.get(invoiceId)
        if row is None:
            raise CustomException(
                ValueError(f"Invoice {invoiceId} not found for user {userId}"),
                statusCode=404,
                uiMessage="Target invoice not found.",
            )
        if (row.get("billing_reason") or "") == "add_on":
            continue  # top-up refunds use the separate clawback path
        if (row.get("status") or "").upper() != "PAID":
            continue  # only captured money is refundable
        start = parseUtc(row.get("period_start"))
        end = parseUtc(row.get("period_end"))
        if start is None or end is None or end <= start:
            continue
        metadata = row.get("metadata_json") or {}
        manualBilling = (
            metadata.get("manualBilling") or {} if isinstance(metadata, dict) else {}
        )
        coverageState = manualBilling.get("coverageState")
        kind = "future" if start > _refundNow() else "current"
        if coverageState == "revoked":
            kind = "revoked"
        intervals.append({
            "invoiceId": row["id"],
            "paymentId": row.get("razorpayPaymentId"),
            "start": start,
            "end": end,
            "amount": int(row.get("total_amount") or row.get("amount") or 0),
            "currency": row.get("currency") or "INR",
            "kind": kind,
        })
    return intervals


def _refundNow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


@router.post("/refunds/quote")
async def quoteSubscriptionRefund(
    payload: SubscriptionRefundQuoteRequest,
    adminUserId=Depends(verifyBillingAdmin),
):
    """
    Quote a staff-approved unused-time refund: per-payment amounts, cutoff
    estimate and coverage effects. Quote only — no entitlement change.
    """
    try:
        paidIntervals = _loadPaidIntervalsForInvoices(
            payload.userId, payload.invoiceIds
        )
        service = SubscriptionRefundService.forProduction()
        quote = service.quoteUnusedTimeRefund(
            staffId=adminUserId,
            payload=payload.dict(),
            paidIntervals=paidIntervals,
            subscription={"user_id": payload.userId},
        )
        logger.info(
            f"Refund quote issued by admin={adminUserId} for user="
            f"{payload.userId} case={payload.caseReference} amount={quote.amount}"
        )
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "data": {
                    "quoteId": quote.quoteId,
                    "userId": quote.userId,
                    "caseReference": quote.caseReference,
                    "currency": quote.currency,
                    "cutoff": quote.cutoff.isoformat(),
                    "expiresAt": quote.expiresAt.isoformat(),
                    "amount": quote.amount,
                    "items": list(quote.items),
                    "accessExpired": quote.accessExpired,
                    "currentAccessPreserved": quote.currentAccessPreserved,
                },
            },
        )
    except CustomException as e:
        raiseHttpException(e)
    except Exception as e:
        raiseHttpException(CustomException(e))


@router.post("/refunds/initiate")
async def initiateSubscriptionRefund(
    payload: SubscriptionRefundInitiateRequest,
    adminUserId=Depends(verifyBillingAdmin),
    idempotencyKey: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """
    Execute an approved unused-time refund: fresh cutoff recomputation under
    lock, durable reservation, affected coverage closure, then provider
    submission outside DB locks.
    """
    try:
        if not idempotencyKey or not idempotencyKey.strip():
            raise CustomException(
                ValueError("Idempotency-Key header is required"),
                statusCode=422,
                uiMessage="Idempotency-Key header is required.",
            )
        paidIntervals = _loadPaidIntervalsForInvoices(
            payload.userId, payload.invoiceIds
        )
        service = SubscriptionRefundService.forProduction()
        intent = service.initiateUnusedTimeRefund(
            staffId=adminUserId,
            payload=payload.dict(),
            requestKey=idempotencyKey.strip(),
        )
        logger.info(
            f"Refund initiated by admin={adminUserId} for user={payload.userId} "
            f"case={payload.caseReference} intent={intent.refundIntentId} "
            f"state={intent.refundState}"
        )
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "data": {
                    "refundIntentId": intent.refundIntentId,
                    "userId": intent.userId,
                    "refundState": intent.refundState,
                    "cutoff": intent.cutoff.isoformat(),
                    "amount": intent.amount,
                    "items": list(intent.items),
                    "accessExpired": intent.accessExpired,
                    "currentAccessPreserved": intent.currentAccessPreserved,
                    "accessRestored": intent.accessRestored,
                },
            },
        )
    except CustomException as e:
        raiseHttpException(e)
    except Exception as e:
        raiseHttpException(CustomException(e))


@router.get("/reconciliation/report")
async def getReconciliationReport(_adminUserId=Depends(verifyBillingAdmin)):
    """
    Return stale attempts, provider/internal mismatches, and webhook anomalies.
    """
    try:
        report = ReconciliationService().generateReport()
        return ORJSONResponse(
            status_code=200,
            content={"status": "SUCCESS", "data": report},
        )
    except CustomException as e:
        raiseHttpException(e)
    except Exception as e:
        raiseHttpException(CustomException(e))


@router.get("/metrics")
async def getBillingMetrics(_adminUserId=Depends(verifyBillingAdmin)):
    """
    Return a current billing metrics snapshot and evaluated alert list.
    """
    try:
        service = BillingMetricsService()
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "data": {
                    "metrics": service.collectMetrics(),
                    "alerts": service.evaluateAlerts(),
                },
            },
        )
    except CustomException as e:
        raiseHttpException(e)
    except Exception as e:
        raiseHttpException(CustomException(e))


@router.post("/reconciliation/webhooks/replay")
async def replayWebhookEvent(
    payload: ReplayWebhookEventRequest,
    adminUserId=Depends(verifyBillingAdmin),
):
    """
    Replay a stored webhook event through the idempotent handler pipeline.
    """
    try:
        result = ReconciliationService().replayWebhookEvent(
            eventId=payload.eventId,
            adminUserId=adminUserId,
        )
        return ORJSONResponse(
            status_code=200,
            content={"status": "SUCCESS", "data": result},
        )
    except CustomException as e:
        raiseHttpException(e)
    except Exception as e:
        raiseHttpException(CustomException(e))


@router.post("/reconciliation/mark-investigated")
async def markInvestigated(
    payload: MarkReconciliationInvestigatedRequest,
    adminUserId=Depends(verifyBillingAdmin),
):
    """
    Mark an anomaly as investigated and persist the operator's audit note.
    """
    try:
        result = ReconciliationService().markInvestigated(
            entityType=payload.entityType,
            entityId=payload.entityId,
            adminUserId=adminUserId,
            note=payload.note,
        )
        return ORJSONResponse(
            status_code=200,
            content={"status": "SUCCESS", "data": result},
        )
    except CustomException as e:
        raiseHttpException(e)
    except Exception as e:
        raiseHttpException(CustomException(e))


@router.post("/credits/force-reset")
async def forceResetAllQuotas(
    resetUsage: bool = False,
    adminUserId=Depends(verifyBillingAdmin),
):
    """
    Recompute monthly_token_quota for all users from credits.json, then flush
    all Redis credit hashes so they rebuild with updated values.

    Query params:
        resetUsage (bool, default false): when true, also zero out used_tokens
            and restore remaining_tokens to the full quota for all users,
            giving everyone a fresh monthly bucket immediately. The billing
            period itself is left untouched.
    """
    try:
        from api.services.credits.creditService import creditService

        result = creditService.forceResetAllQuotas(resetUsage=resetUsage)
        logger.info(
            f"Force credit reset triggered by admin={adminUserId}, "
            f"resetUsage={resetUsage}: {result}"
        )
        return ORJSONResponse(
            status_code=200,
            content={"status": "SUCCESS", "data": result},
        )
    except CustomException as e:
        raiseHttpException(e)
    except Exception as e:
        raiseHttpException(CustomException(e))
