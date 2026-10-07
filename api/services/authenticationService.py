"""
authenticationService.py

This module provides the AuthenticationService class, which encapsulates all business logic
related to user authentication, including sign up, login, third-party provider login,
onboarding, password reset, and logout functionalities.

It raises CustomException with appropriate status codes and UI messages
to be handled at the API layer.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["authenticationService"]


from api.services.billing.manualBillingPresentation import subscriptionDisplayFacts

from utils.exceptionHandler import CustomException, accountAccessRevokedException
from utils.logger import logger
from api.commons import client
from api.services.subscriptions.subscriptionFieldUtils import mapBillingModeToPlanType, CANONICAL_SUBSCRIPTION_SELECT
from api.services.subscriptions.paymentValidationService import (
    calculateSubscriptionDaysLeft,
    mergeSubscriptionLifecycleSnapshot,
    normalizeChurnedSubscription,
)
from api.models import (
    OnboardingDetails,
    LoginWithProvider,
    NewCredentials,
    Login,
    SignUp
)
from jose import jwt
import datetime
import hashlib
import uuid
import os
from urllib.parse import urlparse

DEFAULT_PROFILE_IMAGE = "https://www.gravatar.com/avatar/00000000000000000000000000000000?d=mp&f=y"

class AuthenticationService:
    """
    Service class for user authentication and session management.

    Handles user registration, login, provider-based login, onboarding, password reset, and logout operations.
    Interacts with the Supabase client and manages user/session records in the database.
    """
    def __init__(self) -> None:
        """
        Initialize the AuthenticationService and set up the Supabase client.
        """
        logger.info("Initializing Authentication Service.")
        self.client = client

    def _createTemporaryClient(self):
        """
        Create a temporary Supabase client to perform user-level authentication operations
        without polluting the global client's admin session state.
        """
        from supabase import create_client, ClientOptions
        return create_client(
            supabase_url=os.environ["SUPABASE_URL"],
            supabase_key=os.environ["SUPABASE_KEY"],
            options=ClientOptions(
                auto_refresh_token=False,
                persist_session=False,
            )
        )

    @staticmethod
    def _mapSubscriptionStatus(status: str | None) -> str:
        normalized = (status or "").strip().lower()
        mapping = {
            "none": "NONE",
            "trial": "TRIAL",
            "active": "ACTIVE",
            "renewal_upcoming": "ACTIVE",
            "payment_pending": "ACTIVE",
            "past_due": "EXPIRED",
            "suspended": "EXPIRED",
            "paused": "PAUSED",
            "cancelled": "CANCELLED",
            "expired": "EXPIRED",
        }
        return mapping.get(normalized, "NONE")

    @staticmethod
    def _mapBillingModeToPlan(billingMode: str | None, status: str | None = None) -> str:
        return mapBillingModeToPlanType(billingMode, status)

    @staticmethod
    def _normalizeProviderProfileImage(profileImage: str | None) -> str | None:
        if not isinstance(profileImage, str):
            return None
        normalized = profileImage.strip()
        if not normalized:
            return None
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        return normalized

    def _getSubscriptionSnapshot(self, userId: str) -> dict | None:
        response = self.client.table("subscriptions") \
            .select(CANONICAL_SUBSCRIPTION_SELECT) \
            .eq("user_id", userId) \
            .eq("is_canonical", True) \
            .limit(1) \
            .execute().data
        return response[0] if response else None

    def _refreshLifecycleSnapshot(self, userId: str, subscription: dict | None) -> int:
        if not subscription:
            return 0
        if subscription.get("billing_mode") == "monthly_prepaid":
            from api.services.billing.manualBillingRepository import getManualBillingRepository
            from api.services.subscriptions.entitlementService import EntitlementUnavailableError
            try:
                getManualBillingRepository().getCoverageSnapshot(userId)
                refreshed = self._getSubscriptionSnapshot(userId)
                if not refreshed:
                    raise EntitlementUnavailableError("Canonical subscription missing")
                subscription.clear()
                subscription.update(refreshed)
            except Exception as exc:
                raise EntitlementUnavailableError("Paid coverage refresh unavailable") from exc
            return calculateSubscriptionDaysLeft(subscription.get("current_period_end"))
        expiryStr = subscription.get("current_period_end")
        daysLeft = calculateSubscriptionDaysLeft(expiryStr)
        currentStatus = (subscription.get("status") or "").lower()

        effectiveStatus = currentStatus
        from api.services.subscriptions.paymentValidationService import isPeriodExpired
        if isPeriodExpired(subscription) and expiryStr is not None and currentStatus in ("trial", "active"):
            effectiveStatus = "expired"

        billingState = mergeSubscriptionLifecycleSnapshot(
            subscription.get("billing_state"),
            currentPeriodEnd=expiryStr,
            status=effectiveStatus,
        )
        updatePayload = {"billing_state": billingState}
        if effectiveStatus != currentStatus:
            updatePayload["status"] = effectiveStatus
            updatePayload["plan_type"] = mapBillingModeToPlanType(
                subscription.get("billing_mode"), effectiveStatus,
            )

        try:
            self.client.table("subscriptions").update(updatePayload).eq("id", subscription["id"]).eq("is_canonical", True).execute()
            subscription["billing_state"] = billingState
            if effectiveStatus != currentStatus:
                subscription["status"] = effectiveStatus
            if effectiveStatus == "expired":
                normalizeChurnedSubscription(
                    self.client, subscription, "period_ended",
                )
        except Exception as e:
            logger.warning(f"Failed to update lifecycle snapshot for user {userId}: {e}")
        return daysLeft

    def _createDefaultSubscriptionRow(self, userId: str) -> None:
        """
        Create a canonical placeholder subscription row for a newly created user.

        This enforces the hard-cutover rule: subscription lifecycle fields must be
        sourced from the subscriptions table only (never from Users columns).
        """
        from api.services.billing.manualBillingRepository import getManualBillingRepository
        getManualBillingRepository().ensureCanonicalSubscription(userId)

    @staticmethod
    def _getCreditSnapshot(userId: str) -> dict:
        """
        Return a safe credit-balance snapshot for embedding in login
        responses.  Never raises — returns defaults on failure.
        """
        try:
            from api.services.credits.creditService import creditService
            return creditService.getBalanceSnapshot(userId)
        except Exception as e:
            logger.warning(f"Credit snapshot failed during login for {userId}: {e}")
            return {
                "planTier": "none",
                "monthlyQuota": 0,
                "usedCredits": 0,
                "remainingCredits": 0,
                "usagePercentage": 0.0,
                "periodStart": None,
                "periodEnd": None,
                "lastResetAt": None,
                "initialized": False,
            }

    def _ensureSubscriptionSnapshot(self, userId: str) -> dict:
        """
        Ensure a canonical subscription row exists and return it.
        """
        snapshot = self._getSubscriptionSnapshot(userId)
        if snapshot:
            return snapshot
        try:
            self._createDefaultSubscriptionRow(userId)
        except Exception as createError:
            logger.warning(
                f"Default subscription row create attempt failed for user {userId}: {createError}"
            )
        snapshot = self._getSubscriptionSnapshot(userId)
        if snapshot:
            return snapshot
        raise CustomException(
            ValueError("Missing subscription row"),
            statusCode=409,
            uiMessage="Subscription data is not available. Please contact support."
        )

    def signup(self, signupDetails: SignUp) -> str:
        """
        Register a new user.

        Raises:
            CustomException:
                409 - User already exists
                422 - Invalid signup details
                500 - Generic signup failure
        """
        try:
            if not signupDetails.email or not signupDetails.password:
                raise CustomException(
                    ValueError("Invalid signup payload"),
                    statusCode=422,
                    uiMessage="Invalid signup details. Please check the form."
                )
            passwordString = signupDetails.password + os.environ["SECRET_KEY"]
            hashedPassword = hashlib.md5(passwordString.encode("utf-8")).hexdigest()
            allUsers = []
            page = 1
            while True:
                response = self.client.auth.admin.list_users(page=page, per_page=1000)
                if response == []:
                    break
                allUsers.extend(response)
                page += 1
            allEmails = [x.email for x in allUsers]
            if signupDetails.email in allEmails:
                raise CustomException(
                    ValueError("User already exists"),
                    statusCode=409,
                    uiMessage="An account with this email already exists."
                )
            response = self.client.auth.sign_up(
                {"email": signupDetails.email, "password": hashedPassword}
            )
            return response.user.id
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception
        
    def confirmMail(self, userId: str) -> None:
        """
        Resend the confirmation email to the user with the given user ID.

        Args:
            userId (str): The ID of the user to confirm.

        Raises:
            CustomException: For any errors during the process.
        """
        try:
            allUsers = list()
            page = 1
            while True:
                response = self.client.auth.admin.list_users(page = page, per_page = 1000)
                if response == []:
                    break
                else:
                    allUsers.extend(response)
                    page += 1
            email = list(filter(lambda x: True if x.id == userId else False, allUsers))[0].email
            response = self.client.auth.resend({
            "type": "signup",
            "email": email,
            "options": {
                "email_redirect_to": "https://www.nubrixai.com/login"
            }
            })
            return 
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception
        
    def login(self, loginDetails: Login) -> dict:
        """
        Authenticate a user with email and password.

        Raises:
            CustomException:
                401 - Invalid credentials
                422 - Invalid login payload
                500 - Login failure
        """
        try:
            if not loginDetails.email or not loginDetails.password:
                raise CustomException(
                    ValueError("Invalid login payload"),
                    statusCode=422,
                    uiMessage="Invalid login details. Please check the form."
                )
            passwordString = loginDetails.password + os.environ["SECRET_KEY"]
            hashedPassword = hashlib.md5(passwordString.encode("utf-8")).hexdigest()
            allUsers = []
            page = 1
            while True:
                response = self.client.auth.admin.list_users(page=page, per_page=1000)
                if response == []:
                    break
                allUsers.extend(response)
                page += 1
            user = next((x for x in allUsers if x.email == loginDetails.email), None)
            if not user:
                raise CustomException(
                    ValueError("Invalid credentials"),
                    statusCode=401,
                    uiMessage="The email or password you entered is incorrect. If you originally signed up using a social provider, please click 'Continue with Google/Microsoft' below."
                )
            email_confirmed_at = (
                user.get("email_confirmed_at") or user.get("confirmed_at")
                if isinstance(user, dict)
                else getattr(user, "email_confirmed_at", None) or getattr(user, "confirmed_at", None)
            )
            if not email_confirmed_at:
                raise CustomException(
                    ValueError("Email not confirmed"),
                    statusCode=401,
                    uiMessage="Please confirm your email address before logging in."
                )
            userRows = self.client.table("Users") \
                .select("userId, email, password, onboarded, currentWorkspaceId, profileImage, isBanned") \
                .eq("email", loginDetails.email) \
                .limit(1) \
                .execute().data
            if not userRows:
                try:
                    tempClient = self._createTemporaryClient()
                    tempClient.auth.sign_in_with_password(
                        {"email": loginDetails.email, "password": hashedPassword}
                    )
                except Exception as auth_error:
                    logger.warning(f"Failed to authenticate user {loginDetails.email} via Supabase: {auth_error}")
                    raise CustomException(
                        ValueError("Invalid credentials"),
                        statusCode=401,
                        uiMessage="The email or password you entered is incorrect. If you originally signed up using a social provider, please click 'Continue with Google/Microsoft' below."
                    )
                workspaceId = str(uuid.uuid4())
                self.client.table("Users").insert({
                    "userId": user.id,
                    "email": loginDetails.email,
                    "password": hashedPassword,
                    "currentWorkspaceId": workspaceId,
                    "profileImage": DEFAULT_PROFILE_IMAGE
                }).execute()
                self.client.table("Workspaces").insert({
                    "id": workspaceId,
                    "ownerId": user.id,
                    "ownerEmail": loginDetails.email,
                    "workspaceName": "Default"
                }).execute()
                self._ensureSubscriptionSnapshot(user.id)
                dataSlice = {
                    "userId": user.id,
                    "email": loginDetails.email,
                    "password": hashedPassword,
                    "onboarded": False,
                    "currentWorkspaceId": workspaceId,
                    "profileImage": DEFAULT_PROFILE_IMAGE
                }
            else:
                dataSlice = userRows[0]
                if dataSlice.get("password") != hashedPassword:
                    raise CustomException(
                        ValueError("Invalid credentials"),
                        statusCode=401,
                        uiMessage="The email or password you entered is incorrect. If you originally signed up using a social provider, please click 'Continue with Google/Microsoft' below."
                    )
            if dataSlice.get("isBanned"):
                raise accountAccessRevokedException(str(dataSlice["userId"]))
            sessionStartTime = datetime.datetime.now(datetime.timezone.utc)
            expiresAt = sessionStartTime + datetime.timedelta(hours=24)
            subscription = self._ensureSubscriptionSnapshot(dataSlice["userId"])
            subscriptionDaysLeft = self._refreshLifecycleSnapshot(
                dataSlice["userId"],
                subscription,
            )
            subscriptionStatus = self._mapSubscriptionStatus(subscription.get("status") if subscription else None)
            subscriptionPlan = self._mapBillingModeToPlan(
                subscription.get("billing_mode") if subscription else None,
                subscription.get("status") if subscription else None,
            )
            # Compatibility metadata only. Backend authorization loads current
            # entitlement state from subscriptions and must not trust these claims.
            tokenPayload = {
                "userId": dataSlice["userId"],
                "email": loginDetails.email,
                "sessionStartTime": str(sessionStartTime),
                "exp": int(expiresAt.timestamp()),
                "sub_status": (subscription.get("status") or "none").lower(),
                "plan_type": subscriptionPlan,
            }
            accessToken = jwt.encode(tokenPayload, os.environ["SECRET_KEY"], "HS256")
            self.client.table("Sessions").insert({
                "userId": dataSlice["userId"],
                "email": dataSlice["email"],
                "accessToken": accessToken,
                "sessionStartTime": str(sessionStartTime),
                "lastActivity": str(sessionStartTime),
                "createdAt": str(sessionStartTime),
                "expiresAt": str(expiresAt)
            }).execute()
            profileImage = dataSlice.get("profileImage") or DEFAULT_PROFILE_IMAGE
            credits = self._getCreditSnapshot(dataSlice["userId"])
            return {
                "status": "SUCCESS",
                "userId": dataSlice["userId"],
                "email": dataSlice["email"],
                "accessToken": accessToken,
                "onboarded": int(bool(dataSlice.get("onboarded"))),
                "currentWorkspaceId": dataSlice["currentWorkspaceId"],
                "subscriptionStatus": subscriptionStatus,
                "subscriptionStart": subscription.get("current_period_start") if subscription else None,
                "subscriptionExpiry": subscription.get("current_period_end") if subscription else None,
                "subscriptionDaysLeft": subscriptionDaysLeft,
                "subscriptionPlan": subscriptionPlan,
                    **subscriptionDisplayFacts(subscription),
                "profileImage": profileImage,
                "credits": credits,
            }
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(
                e,
                uiMessage="Login failed. Please try again later."
            )
            logger.error(exception)
            raise exception
        
    def loginWithProvider(self, loginDetails: LoginWithProvider) -> dict:
        """
        Authenticate or register a user using a third-party provider (Google/Azure AD).
        
        If the user does not exist:
        - Creates a new user record with a 12-day free trial.
        - Creates a default workspace.
        - Logs them in.
        
        If the user exists:
        - Logs them in and returns the standard session details.

        Raises:
            CustomException:
                422 - Invalid provider payload
                400 - Unsupported or disabled provider
                500 - Provider login failure
        """
        try:
            if not loginDetails.email or not loginDetails.provider:
                raise CustomException(
                    ValueError("Invalid provider login payload"),
                    statusCode=422,
                    uiMessage="Invalid login details. Please check the form."
                )

            provider = loginDetails.provider.value
            if provider not in ["google", "azure-ad"]:
                raise CustomException(
                    ValueError(f"Unsupported authentication provider: {loginDetails.provider}"),
                    statusCode=400,
                    uiMessage="Unsupported authentication provider. Please use Google or Azure AD."
                )
            profileImage = self._normalizeProviderProfileImage(
                getattr(loginDetails, "profileImage", None)
            )

            response = self.client.table("Users").select("*").eq("email", loginDetails.email).execute()
            userData = {}
            sessionStartTime = datetime.datetime.now(datetime.timezone.utc)

            if response.data:
                userData = response.data[0]
                if userData.get("isBanned"):
                    raise accountAccessRevokedException(str(userData["userId"]))
                subscription = self._ensureSubscriptionSnapshot(userData["userId"])
                subscriptionDaysLeft = self._refreshLifecycleSnapshot(
                    userData["userId"],
                    subscription,
                )
                subscriptionStatus = self._mapSubscriptionStatus(subscription.get("status") if subscription else None)
                subscriptionPlan = self._mapBillingModeToPlan(
                    subscription.get("billing_mode") if subscription else None,
                    subscription.get("status") if subscription else None,
                )
                if profileImage and (not userData.get("profileImage") or userData.get("profileImage") == DEFAULT_PROFILE_IMAGE):
                    self.client.table("Users") \
                        .update({"profileImage": profileImage}) \
                        .eq("userId", userData["userId"]) \
                        .execute()
                    userData["profileImage"] = profileImage
            else:
                userId = str(uuid.uuid4())
                workspaceId = str(uuid.uuid4())
                passwordString = f"{loginDetails.sub}{os.environ['SECRET_KEY']}"
                hashedPassword = hashlib.md5(passwordString.encode("utf-8")).hexdigest()
                
                userData = {
                    "userId": userId,
                    "email": loginDetails.email,
                    "password": hashedPassword,
                    "createdAt": str(sessionStartTime),
                    "onboarded": False,
                    "currentWorkspaceId": workspaceId,
                    "profileImage": profileImage or DEFAULT_PROFILE_IMAGE
                }
                self.client.table("Users").insert(userData).execute()
                self.client.table("Workspaces").insert({
                    "id": workspaceId,
                    "ownerId": userId,
                    "ownerEmail": loginDetails.email,
                    "workspaceName": "Default"
                }).execute()
                subscription = self._ensureSubscriptionSnapshot(userId)
                subscriptionDaysLeft = self._refreshLifecycleSnapshot(userId, subscription)
                subscriptionStatus = self._mapSubscriptionStatus(subscription.get("status"))
                subscriptionPlan = self._mapBillingModeToPlan(
                    subscription.get("billing_mode"),
                    subscription.get("status"),
                )

            sessionStartTime = datetime.datetime.now(datetime.timezone.utc)
            expiresAt = sessionStartTime + datetime.timedelta(hours=24)
            # Compatibility metadata only. Backend authorization loads current
            # entitlement state from subscriptions and must not trust these claims.
            tokenPayload = {
                "userId": userData["userId"],
                "email": userData["email"],
                "sessionStartTime": str(sessionStartTime),
                "exp": int(expiresAt.timestamp()),
                "sub_status": (subscription.get("status") or "none").lower(),
                "plan_type": subscriptionPlan,
            }
            accessToken = jwt.encode(tokenPayload, os.environ["SECRET_KEY"], "HS256")
            self.client.table("Sessions").insert({
                "userId": userData["userId"],
                "email": userData["email"],
                "accessToken": accessToken,
                "sessionStartTime": str(sessionStartTime),
                "lastActivity": str(sessionStartTime),
                "createdAt": str(sessionStartTime),
                "expiresAt": str(expiresAt)
            }).execute()

            profileImage = userData.get("profileImage") or DEFAULT_PROFILE_IMAGE
            credits = self._getCreditSnapshot(userData["userId"])
            return {
                "status": "SUCCESS",
                "userId": userData["userId"],
                "email": userData["email"],
                "accessToken": accessToken,
                "onboarded": 1 if userData.get("onboarded") else 0,
                "currentWorkspaceId": userData["currentWorkspaceId"],
                "subscriptionStatus": subscriptionStatus,
                "subscriptionStart": subscription.get("current_period_start") if subscription else None,
                "subscriptionExpiry": subscription.get("current_period_end") if subscription else None,
                "subscriptionDaysLeft": subscriptionDaysLeft,
                "subscriptionPlan": subscriptionPlan,
                    **subscriptionDisplayFacts(subscription),
                "profileImage": profileImage,
                "credits": credits,
            }
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(
                e,
                uiMessage="Login with provider failed. Please try again later."
            )
            logger.error(exception)
            raise exception
        
    def onboarding(self, onboardingDetails = OnboardingDetails) -> None:
        """
        Update user onboarding details in the database.

        Args:
            onboardingDetails (OnboardingDetails): The user's onboarding information.

        Raises:
            CustomException: For any errors during onboarding update.
        """
        try:
            dataToUpdate = {
                "onboarded": 1,
                "usage": onboardingDetails.usage,
                "fullName": onboardingDetails.fullName,
                "role": onboardingDetails.role,
                "companyName": onboardingDetails.companyName,
                "industryType": onboardingDetails.industryType,
                "companySize": onboardingDetails.companySize,
                "country": onboardingDetails.country,
                "goals": onboardingDetails.goals,
                "source": onboardingDetails.source
            }
            response = self.client.table("Users").update(dataToUpdate).eq("email", onboardingDetails.email).execute()
            return 
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception        
        
    def initiatePasswordReset(self, emailId: str) -> None:
        """
        Initiate a password reset process for the given email address.

        Args:
            emailId (str): The email address of the user requesting password reset.

        Raises:
            CustomException: For any errors during password reset initiation.
        """
        try:
            self.client.auth.reset_password_for_email(
                emailId,
                {
                    "redirect_to": "https://www.nubrixai.com/login/create-new-password"
                }
            )
            return 
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception
        
    def resetPassword(self, newCredentials: NewCredentials) -> None:
        """
        Reset the user's password with new credentials.

        Args:
            newCredentials (NewCredentials): The new password and user email.

        Raises:
            CustomException: For any errors during password reset.
        """
        try:
            passwordString = newCredentials.newPassword + os.environ["SECRET_KEY"]
            hashedPassword = hashlib.md5(passwordString.encode("utf-8")).hexdigest()
            allUsers = list()
            page = 1
            while True:
                response = self.client.auth.admin.list_users(page = page, per_page = 1000)
                if response == []:
                    break
                else:
                    allUsers.extend(response)
                    page += 1
            filteredResult = list(filter(lambda x: True if x.email == newCredentials.email else False, allUsers))[0]
            response = self.client.auth.admin.update_user_by_id(
                filteredResult.id,
                {"password": hashedPassword}
            )
            response = self.client.table("Users").update({"password": hashedPassword}).eq("email", newCredentials.email).execute()
            return
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception
    
    def logout(self, token: str) -> None:
        """
        Log out a user by deleting their session.

        Raises:
            CustomException:
                401 - User not logged in
                500 - Logout failure
        """
        try:
            if not token:
                raise CustomException(
                    ValueError("No active session"),
                    statusCode=401,
                    uiMessage="You are not logged in."
                )
            self.client.table("Sessions").delete().eq("accessToken", token).execute()
            return
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(
                e,
                uiMessage="Logout failed. Try again later."
            )
            logger.error(exception)
            raise exception

authenticationService = AuthenticationService()
