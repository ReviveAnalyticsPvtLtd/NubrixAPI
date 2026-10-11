"""
commons.py

This module sets up the Supabase client, security dependencies, and provides token verification logic for authentication-protected routes.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = [
    "client",
    "verifyToken",
    "verifyTokenWithExpiry",
    "verifyUser",
    "requireActiveSubscription",
    "requireTrialOrAbove",
    "requirePaidPlan",
    "requireCredits",
    "UserContext",
    "updateProjectModifiedAt",
]

from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi import status, HTTPException
from supabase.lib.client_options import ClientOptions
from supabase import create_client
from utils.logger import logger
from utils.exceptionHandler import (
    raiseAccountAccessRevokedHttpException,
    raiseFeatureGateHttpException,
)
from dataclasses import dataclass
from datetime import datetime, timezone
from fastapi import Depends
import os
import gc

security = HTTPBearer()
client = create_client(
    supabase_url = os.environ["SUPABASE_URL"],
    supabase_key = os.environ["SUPABASE_KEY"],
    options = ClientOptions(
        auto_refresh_token = False,
        persist_session = False,
    )
)

from api.services.subscriptions.entitlementService import (
    EntitlementUnavailableError,
    SubscriptionEntitlement,
    SubscriptionEntitlementService,
)

subscriptionEntitlementService = SubscriptionEntitlementService(client)

from jose import jwt, JWTError

def _verifyTokenInternal(credentials: HTTPAuthorizationCredentials, checkExpiry: bool) -> str:
    """
    Internal helper to verify the token, cross-validate with JWT payload, and optionally check for expiry.
    """
    gc.collect()
    token = credentials.credentials
    
    # 1. Decode JWT to verify integrity and extract email
    try:
        payload = jwt.decode(token, os.environ["SECRET_KEY"], algorithms=["HS256"])
        tokenEmail = payload.get("email")
        tokenUserId = payload.get("userId")
        if not tokenEmail or not tokenUserId:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, 
                detail={"status": "FAILURE", "message": "Invalid token payload: missing identity"}
            )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail={"status": "FAILURE", "message": "Invalid token: failed to decode"}
        )

    userRows = (
        client.table("Users")
        .select("isBanned")
        .eq("userId", tokenUserId)
        .limit(1)
        .execute().data
    )
    if userRows and userRows[0].get("isBanned"):
        raiseAccountAccessRevokedHttpException(str(tokenUserId))

    # 2. Check existence in database
    response = client.table("Sessions").select("*").eq("accessToken", token).limit(1).execute()
    
    if not response.data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail={"status": "FAILURE", "message": "Invalid or expired token"}
        )
    
    sessionData = response.data[0]
    dbEmail = sessionData.get("email")
    dbUserId = sessionData.get("userId")

    # 3. Cross-validate identity (JWT vs DB)
    if tokenEmail != dbEmail or tokenUserId != dbUserId:
        logger.error(
            "Token identity mismatch: JWT userId={}, DB userId={}",
            tokenUserId,
            dbUserId,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, 
            detail={"status": "FAILURE", "message": "Token validation failed: identity mismatch"}
        )
    
    # 4. Optional Expiry Check
    if checkExpiry:
        expiresAtStr = sessionData.get("expiresAt")
        if expiresAtStr:
            try:
                # Use dateutil parser to handle all possible timestamp formats robustly
                from dateutil import parser
                expiresAt = parser.parse(expiresAtStr)
                if expiresAt.tzinfo is None:
                    expiresAt = expiresAt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > expiresAt:
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED, 
                        detail={"status": "FAILURE", "message": "Token has expired"}
                    )
            except ValueError:
                logger.error(f"Failed to parse expiry time: {expiresAtStr}")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, 
                    detail={"status": "FAILURE", "message": "Invalid token expiry format"}
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, 
                detail={"status": "FAILURE", "message": "Token expiry information missing"}
            )

    client.table("Sessions").update({"lastActivity": str(datetime.now(timezone.utc))}).eq("accessToken", token).execute()
    return token

def verifyToken(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Verify the provided access token against the Sessions table.
    """
    return _verifyTokenInternal(credentials, checkExpiry=False)

def verifyTokenWithExpiry(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Verify the provided access token and ensure it has not expired.
    """
    return _verifyTokenInternal(credentials, checkExpiry=True)


@dataclass
class UserContext:
    """Authenticated user identity backed by a live Sessions row."""
    userId: str
    email: str
    token: str


def verifyUser(credentials: HTTPAuthorizationCredentials = Depends(security)) -> UserContext:
    token = _verifyTokenInternal(credentials, checkExpiry=False)
    payload = jwt.decode(token, os.environ["SECRET_KEY"], algorithms=["HS256"])
    return UserContext(
        userId=payload["userId"],
        email=payload["email"],
        token=token,
    )


def _loadCurrentEntitlement(user: UserContext) -> SubscriptionEntitlement:
    try:
        return subscriptionEntitlementService.get(user.userId)
    except EntitlementUnavailableError:
        raiseFeatureGateHttpException(
            statusCode=status.HTTP_503_SERVICE_UNAVAILABLE,
            uiMessage="Subscription access could not be verified. Please try again.",
            backendLogMessage=(
                f"Entitlement lookup unavailable for userId={user.userId}"
            ),
            errorCode="ENTITLEMENT_UNAVAILABLE",
        )

def requireActiveSubscription(
    user: UserContext = Depends(verifyUser),
) -> UserContext:
    """Gate: requires an active (or cancelled-but-in-period) subscription."""
    entitlement = _loadCurrentEntitlement(user)
    if not entitlement.activeSubscription:
        raiseFeatureGateHttpException(
            statusCode=status.HTTP_403_FORBIDDEN,
            uiMessage="This feature requires an active subscription.",
            backendLogMessage=(
                f"Feature gate requireActiveSubscription blocked userId={user.userId}, "
                f"status={entitlement.status}, planType={entitlement.planType}"
            ),
            errorCode="FEATURE_BLOCKED",
        )
    return user


def requireTrialOrAbove(
    user: UserContext = Depends(verifyUser),
) -> UserContext:
    """Gate: requires at least a trial subscription."""
    entitlement = _loadCurrentEntitlement(user)
    if not entitlement.trialOrAbove:
        raiseFeatureGateHttpException(
            statusCode=status.HTTP_403_FORBIDDEN,
            uiMessage="Start a free trial or subscribe to access this feature.",
            backendLogMessage=(
                f"Feature gate requireTrialOrAbove blocked userId={user.userId}, "
                f"status={entitlement.status}, planType={entitlement.planType}"
            ),
            errorCode="FEATURE_BLOCKED",
        )
    return user


def requirePaidPlan(
    user: UserContext = Depends(verifyUser),
) -> UserContext:
    """Gate: requires a paid plan (pro or annual)."""
    entitlement = _loadCurrentEntitlement(user)
    if not entitlement.paidPlan:
        raiseFeatureGateHttpException(
            statusCode=status.HTTP_403_FORBIDDEN,
            uiMessage="This feature requires a paid plan.",
            backendLogMessage=(
                f"Feature gate requirePaidPlan blocked userId={user.userId}, "
                f"status={entitlement.status}, planType={entitlement.planType}"
            ),
            errorCode="FEATURE_BLOCKED",
        )
    return user


def requireCredits(operationType: str):
    """
    Dependency factory that returns a FastAPI dependency checking feature
    eligibility first and then remaining monthly tokens for the operation.

    Usage::

        @router.post("/generateChart")
        async def generateChart(body: ..., user=Depends(requireCredits("reporting_query"))):
            ...

    Order of checks:
        1. Current feature entitlement (paid coverage or an eligible trial).
           An expired/cancelled-ended subscription gets the feature-access
           error even with stored top-ups — top-ups never buy time.
        2. Remaining tokens against the configured operation minimum. 402
           when below it; the body carries topupAvailable.
        3. An unreadable balance (remaining = -1) is a retryable 503, never
           fail-open access.

    Raises HTTPException 402 when the user's remaining tokens are below the
    configured minimum for the operation.
    """
    def _dependency(user: UserContext = Depends(verifyUser)) -> UserContext:
        from api.services.credits.creditService import creditService
        from api.services.credits.creditConfig import getOperationMinimum

        # 1. Eligibility first — no quota read happens for ineligible users.
        entitlement = _loadCurrentEntitlement(user)
        if not entitlement.trialOrAbove:
            raiseFeatureGateHttpException(
                statusCode=status.HTTP_403_FORBIDDEN,
                uiMessage="This feature requires an active subscription.",
                backendLogMessage=(
                    f"Credit gate blocked userId={user.userId}, "
                    f"status={entitlement.status}, planType={entitlement.planType}"
                ),
                errorCode="FEATURE_BLOCKED",
            )

        minimum = getOperationMinimum(operationType)
        remaining = creditService.getRemainingTokens(user.userId)

        if remaining == -1:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "status": "FAILURE",
                    "message": (
                        "Your credit balance could not be verified right now. "
                        "Please try again."
                    ),
                    "errorCode": "CREDIT_STATE_UNAVAILABLE",
                },
            )

        if remaining < minimum:
            snapshot = creditService.getBalanceSnapshot(user.userId)
            try:
                current = subscriptionEntitlementService.get(user.userId)
                topupAvailable = current.topupEligible
            except EntitlementUnavailableError:
                logger.warning(
                    "Top-up eligibility unavailable for userId={}",
                    user.userId,
                )
                topupAvailable = False
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "status": "FAILURE",
                    "message": (
                        "Monthly token quota exhausted. "
                        + (
                            "Buy a credit top-up to continue, or wait until your quota "
                            if topupAvailable
                            else "Your quota "
                        )
                        + (f"refills when the paid period starts ({snapshot['nextRefillAt']})."
                            if snapshot.get('nextRefillAt') else
                            "will refill after a new paid period starts. Complete renewal checkout to continue.")
                    ),
                    "errorCode": "MONTHLY_QUOTA_EXHAUSTED",
                    "topupAvailable": topupAvailable,
                    "remaining": remaining,
                    "required": minimum,
                    "resetAt": snapshot.get("periodEnd"),
                },
            )
        return user

    return _dependency


def updateProjectModifiedAt(projectId: str) -> None:
    """
    Update the modifiedAt field of a project to the current UTC timestamp.

    This function should be called at the end of any service method that
    mutates project data (e.g., loading data, editing metadata, creating
    dashboard pages, generating charts, etc.).

    Args:
        projectId (str): The project identifier whose modifiedAt field should be updated.
    """
    try:
        client.table("Projects").update({
            "modifiedAt": datetime.now(timezone.utc).isoformat()
        }).eq("projectId", projectId).execute()
    except Exception as e:
        logger.error(f"Failed to update modifiedAt for project {projectId}: {e}")
