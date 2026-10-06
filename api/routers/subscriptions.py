"""
API router for data subscription operations.

This module provides endpoints for creating data blends, retrieving data sources,
and fetching fields from sources.
"""
__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["router"]


from utils.exceptionHandler import CustomException, raiseHttpException
from api.models import VerifySubscriptionRequest, CreateSubscriptionRequest, AddDomainsRequest, VerifyDomainUpgradeRequest, RemoveDomainRequest, CancelPendingAdditionRequest, CancelSubscriptionRequest, RefundRequest, CreateAnnualRenewalSessionRequest, VerifyAnnualRenewalPaymentRequest, PrepareRenewalInvoiceRequest, CreateRenewalSessionRequest, VerifyRenewalPaymentRequest, ResumeRenewalRequest
from api.services.subscriptions.subscriptionService import subscriptionService
from fastapi.responses import ORJSONResponse
from fastapi import APIRouter, Depends, Header
from api.commons import verifyToken

router = APIRouter()
"""
Router for subscription-related endpoints.
"""


@router.get("/activateFreeTrial")
async def activateFreeTrial(token=Depends(verifyToken)):
    """
    Activate a free trial for a user.

    Args:
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Success message with affected subscription fields.
    """
    try:
        result = subscriptionService.activateFreeTrial(token=token)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Free trial activated successfully.",
                "data": result
            }
        )
    except CustomException as e:
        raiseHttpException(e)


@router.post("/createSubscription")
async def createSubscription(request: CreateSubscriptionRequest, token=Depends(verifyToken),
    requestKey: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128)
):
    """
    Create an ordinary Razorpay order for the selected experts.

    Args:
        request (CreateSubscriptionRequest): Domains to subscribe.
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Subscription details required for checkout.
    """
    try:
        result = subscriptionService.createSubscription(
            domains=request.domains,
            contact=request.contact,
            billingMode=request.billingMode,
            token=token, requestKey=requestKey)
        return ORJSONResponse(status_code=200, content=result)
    except CustomException as e:
        raiseHttpException(e)


@router.post("/verifySubscription")
async def verifySubscription(
    payload: VerifySubscriptionRequest,
    token=Depends(verifyToken)
):
    """
    Verify Razorpay order checkout signature and activate token.

    Args:
        payload (VerifySubscriptionRequest): Razorpay checkout response payload.
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Verification result.
    """
    try:
        result = subscriptionService.verifySubscription(payload=payload.dict(), token=token)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Subscription verified successfully.",
                **(result or {}),
            }
        )
    except CustomException as e:
        raiseHttpException(e)


@router.post("/addDomains")
async def addDomains(payload: AddDomainsRequest, token=Depends(verifyToken),
    requestKey: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128)
):
    """
    Add one or more domains to the authenticated user's subscription
    via a Razorpay Order for prorated billing.

    Args:
        payload (AddDomainsRequest): Domains to add.
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Order details required for embedded checkout.
    """
    try:
        result = subscriptionService.addDomains(domains=payload.domains, token=token, requestKey=requestKey)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Domain upgrade order created.",
                "data": result
            }
        )
    except CustomException as e:
        raiseHttpException(e)


@router.post("/verifyDomainUpgrade")
async def verifyDomainUpgrade(
    payload: VerifyDomainUpgradeRequest,
    token=Depends(verifyToken)
):
    """
    Verify Razorpay Order checkout signature and activate added domains.

    Args:
        payload (VerifyDomainUpgradeRequest): Razorpay checkout response payload.
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Verification result.
    """
    try:
        result = subscriptionService.verifyDomainUpgrade(payload=payload.dict(), token=token)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Domain upgrade payment checked.",
                "data": result,
            }
        )
    except CustomException as e:
        raiseHttpException(e)


@router.post("/removeDomain")
async def removeDomain(payload: RemoveDomainRequest, token=Depends(verifyToken)):
    """
    Schedule a domain removal at the end of the current billing cycle.

    Args:
        payload (RemoveDomainRequest): Domain to remove.
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Current domains, pending removals, and effective timing.
    """
    try:
        result = subscriptionService.removeDomain(domains=payload.domains, token=token)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": f"{len(payload.domains)} domain(s) scheduled for removal at cycle end.",
                "data": result
            }
        )
    except CustomException as e:
        raiseHttpException(e)


@router.post("/cancelPendingAddition")
async def cancelPendingAddition(payload: CancelPendingAdditionRequest, token=Depends(verifyToken)):
    """
    Cancel a pending domain addition request.

    Args:
        payload (CancelPendingAdditionRequest): Domain to cancel.
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Cancellation result.
    """
    try:
        result = subscriptionService.cancelPendingAddition(domain=payload.domain, token=token)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Pending domain addition cancelled.",
                "data": result
            }
        )
    except CustomException as e:
        raiseHttpException(e)

@router.post("/cancelSubscription")
async def cancelSubscription(payload: CancelSubscriptionRequest, token=Depends(verifyToken)):
    """
    Cancel the authenticated user's subscription at the end
    of the current billing cycle.

    Args:
        payload (CancelSubscriptionRequest): Cancellation reason.
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Cancellation result.
    """
    try:
        result = subscriptionService.cancelSubscription(reason=payload.reason, token=token)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Subscription will be cancelled at the end of the current billing cycle.",
                "data": result
            }
        )
    except CustomException as e:
        raiseHttpException(e)


@router.post("/refund")
async def refund(payload: RefundRequest, token=Depends(verifyToken)):
    """
    Initiate a refund for a Razorpay payment.

    Args:
        payload (RefundRequest): Refund details including payment ID and optional amount.
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Refund initiation result.
    """
    try:
        result = subscriptionService.initiateRefund(
            token=token,
            paymentId=payload.paymentId,
            amount=payload.amount
        )
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Refund initiated successfully.",
                "data": result
            }
        )
    except CustomException as e:
        raiseHttpException(e)



@router.get("/invoices")
async def getInvoices(token=Depends(verifyToken)):
    """
    Retrieve all invoices for the authenticated user.

    Args:
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: List of invoices.
    """
    try:
        result = subscriptionService.getInvoices(token=token)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "invoices": result
            }
        )
    except CustomException as e:
        raiseHttpException(e)


@router.post("/prepareRenewalInvoice")
async def prepareRenewalInvoice(
    _request: PrepareRenewalInvoiceRequest | None = None,
    token=Depends(verifyToken),
):
    """
    Prepare (or read) the next renewal invoice on explicit dashboard request.

    Monthly: prepares the next calendar-month renewal invoice on demand,
    including earlier than T-7, while access is valid and renewal is not
    declined. Annual: existing preparation policy.
    """
    try:
        result = subscriptionService.prepareRenewalInvoice(token=token)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Renewal invoice prepared.",
                "data": result,
            }
        )
    except CustomException as e:
        raiseHttpException(e)


@router.post("/createRenewalPaymentSession")
async def createRenewalPaymentSession(
    request: CreateRenewalSessionRequest,
    token=Depends(verifyToken),
    requestKey: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128)
):
    """
    Create a customer-free Razorpay checkout session for an owned renewal
    invoice. Monthly renewals pay before the current period end.
    """
    try:
        result = subscriptionService.createRenewalPaymentSession(
            invoiceId=request.invoiceId,
            token=token, requestKey=requestKey)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Renewal payment session created.",
                "data": result,
            }
        )
    except CustomException as e:
        raiseHttpException(e)


@router.post("/verifyRenewalPayment")
async def verifyRenewalPayment(
    payload: VerifyRenewalPaymentRequest,
    token=Depends(verifyToken),
):
    """
    Verify a renewal checkout signature and finalize the captured payment.

    Monthly early payment schedules the frozen future month; credits refill
    only at its start. Annual keeps its own lifecycle policy.
    """
    try:
        result = subscriptionService.verifyRenewalPayment(
            payload=payload.dict(),
            token=token,
        )
        message = (
            "Renewal payment verified and finalized."
            if result.get("finalized")
            else "Renewal payment verified. Awaiting capture/finalization."
        )
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": message,
                "data": result,
            }
        )
    except CustomException as e:
        raiseHttpException(e)


@router.post("/resumeRenewal")
async def resumeRenewal(
    _request: ResumeRenewalRequest | None = None,
    token=Depends(verifyToken),
):
    """
    Clear the monthly renewal opt-out before the final paid end.

    Restores eligibility for manual renewal invoices/reminders. Never
    charges or reactivates a void order.
    """
    try:
        result = subscriptionService.resumeRenewal(token=token)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Renewal resumed.",
                "data": result,
            }
        )
    except CustomException as e:
        raiseHttpException(e)


@router.post("/createAnnualRenewalPaymentSession")
async def createAnnualRenewalPaymentSession(
    request: CreateAnnualRenewalSessionRequest,
    token=Depends(verifyToken),
    requestKey: str | None = Header(default=None, alias="Idempotency-Key", min_length=1, max_length=128)
):
    """
    Create or reuse a Razorpay Order for an annual renewal invoice.

    Args:
        request (CreateAnnualRenewalSessionRequest): Invoice ID to pay.
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Checkout session payload.
    """
    try:
        result = subscriptionService.createAnnualRenewalPaymentSession(
            invoiceId=request.invoiceId,
            token=token, requestKey=requestKey)
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": "Annual renewal payment session created.",
                "data": result
            }
        )
    except CustomException as e:
        raiseHttpException(e)


@router.post("/verifyAnnualRenewalPayment")
async def verifyAnnualRenewalPayment(
    payload: VerifyAnnualRenewalPaymentRequest,
    token=Depends(verifyToken)
):
    """
    Verify the Razorpay checkout signature for an annual renewal payment.

    Args:
        payload (VerifyAnnualRenewalPaymentRequest): Razorpay checkout callback.
        token: Authorization token dependency.

    Returns:
        ORJSONResponse: Verification result.
    """
    try:
        result = subscriptionService.verifyAnnualRenewalPayment(
            payload=payload.dict(),
            token=token
        )
        message = (
            "Annual renewal payment verified and finalized."
            if result.get("finalized")
            else "Annual renewal payment verified. Awaiting webhook finalization."
        )        
        return ORJSONResponse(
            status_code=200,
            content={
                "status": "SUCCESS",
                "message": message,
                "data": result,
            }
        )
    except CustomException as e:
        raiseHttpException(e)
