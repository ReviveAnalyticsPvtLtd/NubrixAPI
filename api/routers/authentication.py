"""
authentication.py

This module defines the FastAPI routes for user authentication, registration, onboarding, password management, and session handling. It delegates business logic to the AuthenticationService and handles HTTP responses and exceptions.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["router"]


from utils.exceptionHandler import CustomException, raiseHttpException
from utils.logger import logger
from api.services.authenticationService import authenticationService, DEFAULT_PROFILE_IMAGE
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import ORJSONResponse
from api.commons import verifyToken, verifyTokenWithExpiry, verifyUser, UserContext
from api.models import (
    OnboardingDetails,
    LoginWithProvider,
    NewCredentials,
    Login,
    SignUp
)

router = APIRouter()

@router.get("/verify")
def verify(user: UserContext = Depends(verifyUser)):
    """
    Verify the live session and refresh its optional credit balance.
    Synchronous on purpose: FastAPI runs it in the threadpool, so its blocking
    database calls never stall the worker's event loop.
    Unified phases also return current account facts and verified subscription
    permissions; callers must not infer those permissions from stale JWT claims.

    Args:
        user: UserContext injected after token verification.

    Returns:
        ORJSONResponse: Success status with credit balance snapshot.
    """
    from api.services.subscriptions.subscriptionCompatibility import usesUnifiedSubscription
    verificationFacts = {}
    if usesUnifiedSubscription():
        try:
            verificationFacts = authenticationService.getVerificationFacts(user.userId)
        except CustomException as exc:
            logger.error("Session verification failed ({}): {}", exc.errorCode, exc.customErrorMessage)
            raise HTTPException(status_code=exc.statusCode, detail={
                "status": exc.statusCode,
                "message": exc.uiMessage,
                "errorCode": exc.errorCode,
            }) from exc
    try:
        from api.services.credits.creditService import creditService
        credits = creditService.getBalanceSnapshot(user.userId)
    except Exception:
        credits = None
    return ORJSONResponse(status_code=200, content={
        "status": "SUCCESS",
        "message": "Token is valid and has not expired.",
        "credits": credits,
        **verificationFacts,
    })

@router.post("/signUp")
async def signup(signupDetails: SignUp):
    """
    Register a new user with the provided signup details.

    Args:
        signupDetails (SignUp): The user's signup information.

    Returns:
        ORJSONResponse: Success status and user ID, or error message.
    """
    try:
        userId = authenticationService.signup(signupDetails=signupDetails)
        return ORJSONResponse(status_code=200, content={
            "status": "SUCCESS", 
            "userId": userId,
            "profileImage": DEFAULT_PROFILE_IMAGE
        })
    except CustomException as e:
        raiseHttpException(e)
    
@router.get("/confirmMail/{userId}")
async def confirmMail(userId: str):
    """
    Resend the confirmation email to the user with the given user ID.

    Args:
        userId (str): The ID of the user to confirm.

    Returns:
        ORJSONResponse: Success status, or error message.
    """
    try:
        authenticationService.confirmMail(userId = userId)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS"}) 
    except CustomException as e:
        raiseHttpException(e)
    
@router.post("/login")
async def login(loginDetails: Login):
    """
    Authenticate a user with email and password.

    Args:
        loginDetails (Login): The user's login credentials.

    Returns:
        ORJSONResponse: Authentication status, user info, and access token, or error message.
    """
    try:
        response = authenticationService.login(loginDetails=loginDetails)
        return ORJSONResponse(status_code=200, content=response)
    except CustomException as e:
        raiseHttpException(e)
    
@router.post("/loginWithProvider")
async def loginWithProvider(loginDetails: LoginWithProvider):
    """
    Authenticate or register a user using a third-party provider.

    Args:
        loginDetails (LoginWithProvider): The provider's login details.

    Returns:
        ORJSONResponse: Authentication status, user info, and access token, or error message.
    """
    try:
        response = authenticationService.loginWithProvider(loginDetails=loginDetails)
        return ORJSONResponse(status_code=200, content=response)
    except CustomException as e:
        raiseHttpException(e)

    
@router.post("/onboarding")
async def onboarding(onboardingDetails: OnboardingDetails, credentials = Depends(verifyToken)):
    """
    Update user onboarding details in the database. Requires authentication.

    Args:
        onboardingDetails (OnboardingDetails): The user's onboarding information.
        credentials: Authorization credentials (injected by FastAPI).

    Returns:
        ORJSONResponse: Success status and message, or error message.
    """
    try:
        authenticationService.onboarding(onboardingDetails = onboardingDetails)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "message": "User onboarded successfully."})        
    except CustomException as e:
        raiseHttpException(e)

@router.get("/initiatePasswordReset")
async def initiatePasswordReset(emailId: str):
    """
    Initiate a password reset process for the given email address.

    Args:
        emailId (str): The email address of the user requesting password reset.

    Returns:
        ORJSONResponse: Success status and message, or error message.
    """
    try:
        authenticationService.initiatePasswordReset(emailId = emailId)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "message": "Password reset initiated successfully."})
    except CustomException as e:
        raiseHttpException(e)    

@router.patch("/resetPassword")
async def resetPassword(newCredentials: NewCredentials):
    """
    Reset the user's password with new credentials.

    Args:
        newCredentials (NewCredentials): The new password and user email.

    Returns:
        ORJSONResponse: Success status and message, or error message.
    """
    try:
        authenticationService.resetPassword(newCredentials = newCredentials)
        return ORJSONResponse(status_code = 200, content = {"status": "SUCCESS", "message": "Password updated successfully!"})
    except CustomException as e:
        raiseHttpException(e)

@router.get("/logout")
async def logout(token = Depends(verifyToken)):
    """
    Log out the user by deleting their session using the provided access token. Requires authentication.

    Args:
        token: Authorization token (injected by FastAPI).

    Returns:
        ORJSONResponse: Success status and message, or error message.
    """
    try:
        authenticationService.logout(token=token)
        return ORJSONResponse(status_code=200, content={"status": "SUCCESS"})
    except CustomException as e:
        raiseHttpException(e)
