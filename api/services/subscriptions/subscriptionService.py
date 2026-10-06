"""
subscriptionService.py

This module provides the SubscriptionService class, which encapsulates
all business logic related to user subscriptions, including free trials,
manual subscription checkout, paid coverage, and payment audit logging.
"""

__version__ = "1.0.0"
__author__ = "Rauhan Ahmed Siddiqui"
__all__ = ["subscriptionService"]


from dateutil.relativedelta import relativedelta
from utils.exceptionHandler import CustomException
from utils.logger import logger
from api.services.billing.billingEngine import computeInvoiceSnapshot
from api.services.billing.billingEventService import BillingEventService
from api.services.subscriptions.subscriptionFieldUtils import (
    CANONICAL_SUBSCRIPTION_SELECT,
    mapBillingModeToPlanType,
    normalizeDomainList,
    subscriptionBillingState,
    subscriptionDomainCount,
    subscriptionExperts,
    subscriptionPendingAdditions,
    subscriptionPendingRemovals,
    toSubscriptionBillingPayload,
)
from api.services.subscriptions.paymentValidationService import (
    PaymentValidationError,
    assertInvoiceBelongsToSubscription,
    blocksNewCheckout,
    isAccessActive,
    loadPayableInvoice,
    parseUtc,
    utcFromTimestamp,
    utcNow,
    validateOrderPaymentAgainstInvoice,
)
from api.commons import client
from jose import jwt
import requests
import razorpay
import datetime
from dateutil import parser
import hashlib
import hmac
import json
import os


class SubscriptionService:
    """
    Service class for user subscription management.

    Handles free trials, customer-free one-time subscription payments,
    and checkout signature verification.
    """

    def __init__(self) -> None:
        """
        Initialize the SubscriptionService.
        """
        logger.info("Initializing Subscription Service.")
        self.client = client
        self.razorpayClient = razorpay.Client(
            auth=(
                os.environ.get("RAZORPAY_KEY_ID", ""),
                os.environ.get("RAZORPAY_KEY_SECRET", "")
            )
        )
        self.BASE_PLAN_ID = os.environ.get("RAZORPAY_PRO_PLAN_ID", "")
        self.VALID_DOMAINS = {"banking", "manufacturing", "supplychain", "telecom"}

    def _getCanonicalSubscription(self, userId: str, required: bool = False) -> dict | None:
        """
        Fetch canonical subscription row for a user.

        Args:
            userId (str): Internal user ID.
            required (bool): Whether to raise when row is missing.

        Returns:
            dict | None: Subscription row or None.
        """
        response = self.client.table("subscriptions") \
            .select(CANONICAL_SUBSCRIPTION_SELECT) \
            .eq("user_id", userId) \
            .eq("is_canonical", True) \
            .limit(1) \
            .execute().data
        if response:
            return response[0]
        if required:
            raise Exception("Subscription data is missing for this user")
        return None

    def _upsertCanonicalSubscription(
        self,
        userId: str,
        billingMode: str,
        status: str,
        currentPeriodStart: str | None,
        currentPeriodEnd: str | None,
        renewalDueAt: str | None,
        autoRenewEnabled: bool,
        paymentCollectionMode: str,
        subscribedExperts=None,
        domainCount=None,
        pendingRemovals=None,
        pendingAdditions=None,
        billingState=None,
        cancellationReason=None,
        planType: str | None = None,
    ) -> None:
        """
        Upsert canonical subscription row for a user.
        """
        existing = self._getCanonicalSubscription(userId=userId, required=False)
        resolvedPlanType = planType or mapBillingModeToPlanType(billingMode, status)
        payload = {
            "user_id": userId,
            "is_canonical": True,
            "billing_mode": billingMode,
            "status": status,
            "plan_type": resolvedPlanType,
            "current_period_start": currentPeriodStart,
            "current_period_end": currentPeriodEnd,
            "renewal_due_at": renewalDueAt,
            "auto_renew_enabled": autoRenewEnabled,
            "payment_collection_mode": paymentCollectionMode,
            "default_currency": "INR",
        }
        payload.update(toSubscriptionBillingPayload(
            subscribedExperts=subscribedExperts,
            domainCount=domainCount,
            pendingRemovals=pendingRemovals,
            pendingAdditions=pendingAdditions,
            billingState=billingState,
            cancellationReason=cancellationReason,
        ))
        if existing:
            self.client.table("subscriptions").update(payload).eq("id", existing["id"]).execute()
        else:
            self.client.table("subscriptions").insert(payload).execute()

    @staticmethod
    def _normalizeBillingMode(billingMode: str | None) -> str:
        """
        Normalize and validate billing mode for subscription purchase flows.

        ``monthly_recurring`` is retained only as a legacy request alias that
        maps to manual monthly checkout; new storage uses ``monthly_prepaid``.
        """
        normalized = (billingMode or "monthly_prepaid").strip().lower()
        if normalized == "monthly_recurring":
            normalized = "monthly_prepaid"
        if normalized not in {"monthly_prepaid", "annual_prepaid"}:
            raise Exception(f"Unsupported billingMode: {billingMode}")
        return normalized

    def _reissueTokenWithUpdatedClaims(
        self, oldToken: str, newStatus: str, newPlanType: str
    ) -> str:
        """
        Re-mint compatibility plan claims for existing clients that consume the
        returned accessToken. Backend feature gates ignore these mutable claims and
        load current entitlement state from the canonical subscriptions row.

        The old session remains valid by design during the compatibility period;
        using it must not preserve old plan access.
        """
        oldSession = self.client.table("Sessions") \
            .select("userId, email, sessionStartTime, expiresAt") \
            .eq("accessToken", oldToken) \
            .limit(1) \
            .execute().data
        if not oldSession:
            raise Exception("Session not found for token reissue")
        session = oldSession[0]
        payload = jwt.decode(oldToken, os.environ["SECRET_KEY"], algorithms=["HS256"])
        payload["sub_status"] = newStatus
        payload["plan_type"] = newPlanType
        newToken = jwt.encode(payload, os.environ["SECRET_KEY"], "HS256")
        now = str(datetime.datetime.now(datetime.timezone.utc))
        self.client.table("Sessions").upsert({
            "userId": session["userId"],
            "email": session["email"],
            "accessToken": newToken,
            "sessionStartTime": session.get("sessionStartTime", now),
            "lastActivity": now,
            "createdAt": now,
            "expiresAt": session.get("expiresAt", now),
        }, on_conflict="accessToken").execute()
        return newToken

    def _createFrozenInvoiceFromSnapshot(
        self,
        userId: str,
        subscriptionId: str | None,
        billingReason: str,
        paymentFlow: str,
        requiresCustomerAuth: bool,
        snapshot,
        metadata: dict | None = None,
        expectedSubscriptionVersion: int | None = None,
    ) -> dict:
        """
        Create an internal invoice row with immutable pricing/tax snapshot.
        """
        payload = {
            "userId": userId,
            "subscription_id": subscriptionId,
            "billing_reason": billingReason,
            "payment_flow": paymentFlow,
            "requires_customer_auth": requiresCustomerAuth,
            "period_start": snapshot.period_start,
            "period_end": snapshot.period_end,
            "amount_before_tax": snapshot.amount_before_tax,
            "tax_amount": snapshot.tax.tax_amount,
            "total_amount": snapshot.total_amount,
            "amount": snapshot.total_amount,
            "currency": snapshot.currency,
            "status": "PAYMENT_PENDING",
            "tax_breakdown_json": snapshot.tax.to_dict(),
            "tax_rule_version": snapshot.tax.tax_rule_version,
            "place_of_supply_snapshot": snapshot.tax.place_of_supply_snapshot,
            "pricing_version": snapshot.pricing_version,
            "pricing_reference_snapshot_json": snapshot.pricing_reference_snapshot_json,
            "metadata_json": metadata or {},
        }
        if expectedSubscriptionVersion is not None:
            from api.services.billing.manualBillingRepository import getManualBillingRepository
            return getManualBillingRepository().createFrozenRenewalInvoice(payload,expectedSubscriptionVersion)
        result = self.client.table("Invoices").insert(payload).execute().data
        if not result:
            raise Exception("Failed to create frozen invoice snapshot")
        return result[0]

    def _attachOrderToInvoice(self, invoiceId: str, orderId: str, receipt: str | None = None) -> None:
        """
        Attach created Razorpay order metadata to an existing internal invoice.
        """
        updateData = {
            "razorpay_order_id": orderId,
            "status": "PAYMENT_PENDING",
        }
        if receipt:
            updateData["provider_receipt"] = receipt
        self.client.table("Invoices").update(updateData).eq("id", invoiceId).execute()

    def _isAnnualRenewalOrderReusable(self, order: dict) -> bool:
        """
        Determine whether an existing annual renewal order can be reused safely.

        Razorpay can block additional attempts when an order is in `attempted`
        state with an associated `authorized` payment. To avoid checkout failures,
        attempted orders are reused only when no blocking payment status exists.
        """
        status = str(order.get("status", "")).lower()
        if status == "created":
            return True
        if status != "attempted":
            return False

        orderId = order.get("id")
        if not orderId:
            return False

        try:
            paymentsResp = self.razorpayClient.order.payments(orderId)
            payments = paymentsResp.get("items", []) if isinstance(paymentsResp, dict) else []
        except Exception as e:
            logger.warning(
                f"Unable to fetch payments for attempted order {orderId}; "
                f"will create a fresh order. error={e}"
            )
            return False

        blockingStatuses = {"authorized", "captured"}
        for payment in payments:
            paymentStatus = str(payment.get("status", "")).lower()
            if paymentStatus in blockingStatuses:
                return False
        return True

    def _markInvoicePaid(self, invoiceId: str, paymentId: str, paidAt: str | None = None) -> None:
        """
        Mark an internal invoice as paid.
        """
        updateData = {
            "status": "PAID",
            "razorpayPaymentId": paymentId,
        }
        if paidAt:
            updateData["paidAt"] = paidAt
        self.client.table("Invoices").update(updateData).eq("id", invoiceId).execute()

    def _markPayableRenewalInvoicesForRepricing(self, subscriptionId: str) -> None:
        """
        Expire payable renewal invoices when pending removals change so the
        next dashboard prep recomputes the frozen renewal price.
        """
        invoices = self.client.table("Invoices") \
            .select(
                "id, status, billing_reason, metadata_json"
            ) \
            .eq("subscription_id", subscriptionId) \
            .eq("billing_reason", "renewal") \
            .execute().data

        payableStatuses = {"upcoming", "payment_pending", "expired"}
        for invoice in invoices or []:
            status = (invoice.get("status") or "").lower()
            if status not in payableStatuses:
                continue
            existingMetadata = invoice.get("metadata_json")
            metadata = dict(existingMetadata) if isinstance(existingMetadata, dict) else {}
            metadata.update({
                "repricingRequired": True,
                "repricingReason": "pending_removals_changed",
                "repricingSource": "removeDomain",
                "repricingMarkedAt": utcNow().isoformat(),
            })
            self.client.table("Invoices").update({
                "status": "EXPIRED",
                "metadata_json": metadata,
            }).eq("id", invoice["id"]).execute()

    def _finalizeCapturedAnnualRenewalPayment(self, invoice, subscription, payment, userId):
        return self._finalizeManualCheckout(str(invoice['id']), payment['order_id'], payment['id'], payment)

    @staticmethod
    def _isSubscriptionActive(status: str | None) -> bool:
        """
        Determine whether a canonical subscription status is active-like.
        """
        return (status or "").lower() in {"active", "renewal_upcoming", "payment_pending"}

    @staticmethod
    def _isAccessActive(subscription: dict | None, now: datetime.datetime | None = None) -> bool:
        return isAccessActive(subscription, now)

    @staticmethod
    def _blocksNewCheckout(subscription: dict | None, now: datetime.datetime | None = None) -> bool:
        return blocksNewCheckout(subscription, now)

    @staticmethod
    def _paymentValidationException(error: PaymentValidationError) -> CustomException:
        return CustomException(error, statusCode=400, uiMessage=str(error))

    def _ensureLifecycleId(self, subscription: dict | None) -> str:
        """
        Resolve the immutable purchase lifecycle ID for the canonical row.

        The lifecycle ID lives in ``billing_state.manualBilling.lifecycleId``;
        it survives routine version bumps and status changes. A new purchase
        after reset assigns a fresh ID. Missing -> generate (caller persists
        it with the invoice/subscription mutation).
        """
        billingState = subscriptionBillingState(subscription)
        manualBilling = billingState.get("manualBilling") or {}
        lifecycleId = manualBilling.get("lifecycleId")
        if lifecycleId:
            return str(lifecycleId)
        import uuid as uuidModule

        return str(uuidModule.uuid4())

    def _auditLog(self, userId: str, eventType: str, **kwargs) -> None:
        """
        Insert an audit row into the unified billing ledger.

        Args:
            userId (str): The user ID associated with this log entry.
            eventType (str): The event type (e.g. 'subscription.created', 'domain.add_requested').
            **kwargs: Optional fields — status, plus any key-value pairs to store in metadata.
                      Reserved keys: paymentId, invoiceId, amount, currency
                      are auto-mapped into metadata with 'razorpay' prefix where applicable.
        """
        try:
            status = kwargs.pop("status", None)
            existingMeta = kwargs.pop("metadata", None) or {}

            metaFields = {}
            razorpayPrefixed = {"paymentId", "invoiceId"}
            for key in ("paymentId", "invoiceId", "amount", "currency"):
                val = kwargs.pop(key, None)
                if val is not None:
                    metaKey = f"razorpay{key[0].upper()}{key[1:]}" if key in razorpayPrefixed else key
                    metaFields[metaKey] = val

            metadata = {**metaFields, **existingMeta, **kwargs}

            BillingEventService(self.client).log_event(
                user_id=userId,
                event_type=eventType,
                event_status=status,
                metadata=metadata if metadata else None,
            )
        except Exception as e:
            logger.error(f"billing_events insert failed for user {userId}, event {eventType}: {e}")

    def _normalizePhone(self, phone: str) -> str:
        """
        Normalize a phone number to E.164-style format for Razorpay compatibility.

        Strips formatting characters (spaces, dashes, parentheses) and
        attempts to prefix +91 for 10-digit Indian numbers. Returns the
        original value with a warning log if the format is unrecognized.

        Args:
            phone (str): Raw phone number input.

        Returns:
            str: Normalized phone number string, or empty string if input is empty.
        """
        if not phone or not isinstance(phone, str):
            return ""
        cleaned = phone.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if not cleaned:
            return ""
        if cleaned.startswith("+"):
            return cleaned
        digitsOnly = "".join(c for c in cleaned if c.isdigit())
        if not digitsOnly:
            logger.warning(f"Phone normalization failed — no digits found in input, returning as-is")
            return phone.strip()
        if len(digitsOnly) == 10:
            return f"+91{digitsOnly}"
        if len(digitsOnly) == 12 and digitsOnly.startswith("91"):
            return f"+{digitsOnly}"
        logger.warning(f"Phone normalization — unrecognized format, returning cleaned value")
        return cleaned

    def _resolveCheckoutIdentity(self, userId: str, tokenEmail: str) -> dict:
        """
        Resolve the canonical identity for checkout from the Users table.

        The DB record is the single source of truth for email, name, and
        phone. The token email is used as fallback only when the DB email
        field is empty.

        Args:
            userId (str): The internal user ID.
            tokenEmail (str): The email from the decoded JWT token (fallback).

        Returns:
            dict: Canonical identity with keys 'email', 'name', 'contact'.
        """
        record = self.client.table("Users") \
            .select("email, fullName, phoneNumber") \
            .eq("userId", userId).execute()
        if not record.data:
            return {"email": tokenEmail, "name": tokenEmail, "contact": ""}
        user = record.data[0]
        canonicalEmail = user.get("email") or tokenEmail
        canonicalName = user.get("fullName") or canonicalEmail
        rawPhone = user.get("phoneNumber") or ""
        canonicalContact = self._normalizePhone(rawPhone)
        return {
            "email": canonicalEmail,
            "name": canonicalName,
            "contact": canonicalContact,
        }

    def _normalizeAndValidateDomains(self, domains: list[str]) -> list[str]:
        """
        Normalize and validate domain input for subscription workflows.

        Normalization rules:
        - Trim surrounding whitespace
        - Lowercase domain names

        Validation rules:
        - Must be a non-empty list of strings
        - Domain count must be between 1 and 4
        - No duplicate domains allowed
        - All domains must be in VALID_DOMAINS

        Args:
            domains (list[str]): Raw domains from request payload.

        Returns:
            list[str]: Normalized domain list preserving input order.
        """
        if not isinstance(domains, list):
            raise Exception("Domains must be provided as a list")
        normalizedDomains = []
        for domain in domains:
            if not isinstance(domain, str):
                raise Exception("All domains must be strings")
            normalized = domain.strip().lower()
            if not normalized:
                raise Exception("Domain values must be non-empty strings")
            normalizedDomains.append(normalized)
        if not normalizedDomains or len(normalizedDomains) > 4:
            raise Exception("Domain count must be between 1 and 4")
        if len(set(normalizedDomains)) != len(normalizedDomains):
            raise Exception("Duplicate domains not allowed. Domains must be unique.")
        invalidDomains = set(normalizedDomains) - self.VALID_DOMAINS
        if invalidDomains:
            raise Exception(f"Invalid domains: {', '.join(invalidDomains)}")
        return normalizedDomains

    def _normalizeSingleDomain(self, domain: str) -> str:
        """
        Normalize and validate a single domain value.

        Args:
            domain (str): Raw domain input.

        Returns:
            str: Normalized domain value.
        """
        return self._normalizeAndValidateDomains([domain])[0]

    _STALE_ORDER_THRESHOLD_MINUTES = 30

    def _reconcilePendingAdditions(self, subscription: dict) -> dict:
        """
        Reconcile awaiting_payment entries against Razorpay Order status.

        Activates paid domains, removes expired entries, and cleans up
        stale orders older than _STALE_ORDER_THRESHOLD_MINUTES that the
        user abandoned (closed the checkout without paying). Called at
        the start of addDomains() and removeDomain() to resolve any
        missed activations.

        Args:
            subscription (dict): Subscription row with pending_additions.

        Returns:
            dict: The (possibly refreshed) subscription row.
        """
        pendingAdditions = subscriptionPendingAdditions(subscription)
        userId = subscription.get("user_id")
        changed = False
        for item in pendingAdditions:
            if item.get("state") != "awaiting_payment":
                continue
            orderId = item.get("orderId")
            if not orderId:
                continue
            try:
                order = self.razorpayClient.order.fetch(orderId)
            except Exception as fetchErr:
                logger.error(f"Failed to fetch order {orderId}: {fetchErr}")
                continue
            if order["status"] == "paid":
                notes = order.get("notes", {})
                self._activatePaidDomains(
                    userId=userId,
                    domains=[d.strip() for d in notes.get("domains", "").split(",") if d.strip()],
                    targetQuantity=int(notes.get("targetQuantity", 0)),
                    referenceId=orderId,
                )
                changed = True
            elif order["status"] in ("expired", "cancelled"):
                item["state"] = "expired"
                changed = True
            elif order["status"] == "created":
                requestedAt = item.get("requestedAt")
                if requestedAt:
                    try:
                        requestedAtUtc = parseUtc(requestedAt)
                        age = utcNow() - requestedAtUtc if requestedAtUtc else datetime.timedelta.max
                        if age > datetime.timedelta(minutes=self._STALE_ORDER_THRESHOLD_MINUTES):
                            item["state"] = "expired"
                            changed = True
                            logger.info(
                                f"Expired stale awaiting_payment order {orderId} "
                                f"for user {userId} (age: {age})"
                            )
                    except (ValueError, TypeError):
                        pass
        if changed:
            cleanedPending = [item for item in pendingAdditions if item.get("state") not in ("expired", "activated")]
            self.client.table("subscriptions").update({
                "pending_additions": cleanedPending
            }).eq("id", subscription["id"]).execute()
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
        return subscription

    def _activatePaidDomains(self, userId: str, domains: list[str], targetQuantity: int, referenceId: str) -> None:
        """
        Activate domains after confirmed payment. Idempotent -- safe to call
        multiple times for the same referenceId (order ID or legacy payment link ID).

        The billing engine picks up the updated domainCount at renewal, so no
        Razorpay API call is needed here.

        Args:
            userId (str): The user ID.
            domains (list[str]): Domain names to activate.
            targetQuantity (int): The expected total domain count after activation.
            referenceId (str): The Razorpay Order ID (or legacy Payment Link ID) for idempotency.
        """
        subscription = self._getCanonicalSubscription(userId=userId, required=True)
        pendingAdditions = subscriptionPendingAdditions(subscription)
        matchKey = "orderId" if any(item.get("orderId") == referenceId for item in pendingAdditions) else "paymentLinkId"
        matchEntries = [item for item in pendingAdditions if item.get(matchKey) == referenceId]
        if matchEntries and all(item.get("state") == "activated" for item in matchEntries):
            return
        if not matchEntries or any(item.get("state") in ("cancelled","expired","failed") for item in matchEntries):
            raise ValueError("DOMAIN_ATTEMPT_CLOSED_OR_MISSING")
        if not self._isSubscriptionActive(subscription.get("status")) or parseUtc(subscription.get("current_period_end")) <= utcNow():
            raise ValueError("DOMAIN_COVERAGE_EXPIRED")
        if set(domains) != {item.get("domain") for item in matchEntries}:
            raise ValueError("DOMAIN_ATTEMPT_MISMATCH")
        currentExperts = subscriptionExperts(subscription)
        activatedDomains = []
        for domain in domains:
            if domain not in currentExperts:
                currentExperts.append(domain)
                activatedDomains.append(domain)
        for item in pendingAdditions:
            if item.get(matchKey) == referenceId:
                item["state"] = "activated"
        updatedDomainCount = len(currentExperts)
        self.client.table("subscriptions").update({
            "subscribed_experts": currentExperts,
            "domain_count": updatedDomainCount,
            "pending_additions": pendingAdditions,
        }).eq("id", subscription["id"]).execute()
        try:
            from api.services.credits.creditService import creditService
            creditService.applyDomainCountChange(
                userId=userId,
                domainCount=updatedDomainCount,
                grantImmediately=True,
            )
        except Exception as creditErr:
            logger.warning(
                f"Credit allowance update failed after domain activation "
                f"for userId={userId}: {creditErr}"
            )
        for domain in activatedDomains:
            self._auditLog(
                userId, "domain.add_activated",
                status="ACTIVATED",
                metadata={
                    "domain": domain,
                    "referenceId": referenceId,
                    "newDomainCount": updatedDomainCount,
                }
            )
        logger.info(f"Activated domains {activatedDomains} for user {userId} via {referenceId}")

    @staticmethod
    def _sendFreeTrialEmail(email: str, name: str) -> None:
        """
        Send free trial email to a user.

        Args:
            email (str): The email address of the user.
            name (str): The name of the user.
        """
        url = os.environ.get("FREE_TRIAL_EMAIL_URL")
        if not url:
            logger.warning("FREE_TRIAL_EMAIL_URL not configured, skipping free trial email.")
            return
        try:
            response = requests.post(
                url=url,
                json={
                    "email": email,
                    "name": name
                },
                headers={
                    "Authorization": f"Bearer {os.environ.get('SUPABASE_KEY_OLD', '')}"
                },
                timeout=10
            )
            if response.status_code >= 400:
                logger.warning(f"Free trial email failed to send to {email}, status={response.status_code}: {response.text}")
            else:
                logger.info(f"Free trial email successfully dispatched to {email}")
        except Exception as e:
            exception = CustomException(e)
            logger.error(f"Error while sending free trial email to {email}: {exception}")
            raise exception

    def activateFreeTrial(self, token: str) -> dict:
        """
        Activate a free trial for a user.

        Args:
            token (str): Authorization token.

        Returns:
            dict: The affected subscription fields.
        """
        try:
            decodedToken = jwt.decode(
                token,
                os.environ["SECRET_KEY"],
                algorithms = ["HS256"]
            )
            userId = decodedToken.get("userId")
            userEmail = decodedToken.get("email")
            currentTime = datetime.datetime.now(datetime.timezone.utc)
            trialDurationDays = 12
            trialExpiry = currentTime + datetime.timedelta(days=trialDurationDays)
            experts = ["banking", "manufacturing", "supplychain", "telecom"]
            from api.services.billing.manualBillingRepository import getManualBillingRepository
            repository = getManualBillingRepository()
            repository.ensureCanonicalSubscription(userId)
            try:
                activated = repository.activateTrial(userId, tuple(experts))
            except ValueError as exc:
                raise CustomException(exc, statusCode=409,
                    uiMessage="A free trial is not available for this account.") from exc
            trialExpiry = activated['current_period_end']
            records = self.client.table("Users").select("fullName").eq("userId", userId).limit(1).execute()
            name = records.data[0]["fullName"] if records.data else userEmail
            self._sendFreeTrialEmail(email=userEmail, name=name)
            self._auditLog(userId, "free_trial.activated", status="TRIAL")
            try:
                from api.services.credits.creditService import creditService
                creditService.initializeCreditBalance(
                    userId=userId,
                    planTier="free",
                    domainCount=4,
                )
            except Exception as creditErr:
                logger.warning(f"Credit initialization failed for trial user {userId}: {creditErr}")
            newToken = self._reissueTokenWithUpdatedClaims(token, "trial", "free")
            return {
                "subscriptionPlan": "free",
                "subscriptionStatus": "TRIAL",
                "subscriptionStart": currentTime.isoformat(),
                "subscriptionExpiry": trialExpiry.isoformat(),
                "subscriptionDaysLeft": trialDurationDays,
                "subscribedExperts": experts,
                "accessToken": newToken,
            }
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def createSubscription(self, domains: list[str], contact: str, token: str,
                           billingMode: str = "monthly_prepaid", requestKey: str | None = None) -> dict:
        normalized = self._normalizeAndValidateDomains(domains)
        mode = self._normalizeBillingMode(billingMode)
        decoded = jwt.decode(token, os.environ["SECRET_KEY"], algorithms=["HS256"])
        return self._reservedCheckout(decoded, 'initial_purchase', mode,
            {'domains': normalized}, requestKey, contact)

    def verifySubscription(self, payload: dict, token: str) -> dict:
        return self._verifyDurableCheckout(payload, token, 'initial_purchase')

    def addDomains(self, domains: list[str], token: str, requestKey: str | None = None) -> dict:
        normalized = self._normalizeAndValidateDomains(domains)
        decoded = jwt.decode(token, os.environ["SECRET_KEY"], algorithms=["HS256"])
        subscription = self._getCanonicalSubscription(decoded['userId'], required=True)
        return self._reservedCheckout(decoded, 'expert_addition', subscription['billing_mode'],
            {'domains': normalized}, requestKey)

    def verifyDomainUpgrade(self, payload: dict, token: str) -> dict:
        return self._verifyDurableCheckout(payload, token, 'expert_addition')

    def removeDomain(self, domains: list[str], token: str) -> dict:
        """
        Schedule one or more domain removals at the end of the current billing cycle.

        Purely a DB operation -- the billing engine reads the reduced domainCount
        at renewal. The user retains access until cycle end. At least one domain
        must remain active.

        Args:
            domains (list[str]): The domain expert names to remove.
            token (str): Authorization token.

        Returns:
            dict: Current domains, pending removals, and effective timing.
        """
        try:
            normalizedDomains = self._normalizeAndValidateDomains(domains)
            decodedToken = jwt.decode(
                token,
                os.environ["SECRET_KEY"],
                algorithms=["HS256"]
            )
            userId = decodedToken.get("userId")
            userRecord = self.client.table("Users") \
                .select("userId") \
                .eq("userId", userId) \
                .execute()
            if not userRecord.data:
                raise Exception("User not found")
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            if subscription.get("billing_mode") == "monthly_prepaid":
                from api.services.billing.manualBillingRepository import getManualBillingRepository
                return getManualBillingRepository().scheduleExpertRemoval(userId,normalizedDomains)
            subscription = self._reconcilePendingAdditions(subscription)
            if not self._isSubscriptionActive(subscription.get("status")):
                raise Exception("Subscription must be active to remove a domain")
            currentExperts = subscriptionExperts(subscription)
            pendingRemovals = subscriptionPendingRemovals(subscription)
            for domain in normalizedDomains:
                if domain not in currentExperts:
                    raise Exception(f"Domain '{domain}' is not in your active domains")
                if domain in pendingRemovals:
                    raise CustomException(
                        ValueError(f"Domain '{domain}' is already scheduled for removal"),
                        statusCode=409,
                        uiMessage=f"Domain '{domain}' is already scheduled for removal."
                    )
            activeDomains = [d for d in currentExperts if d not in pendingRemovals]
            if len(activeDomains) - len(normalizedDomains) < 1:
                raise Exception("Cannot remove all domains. At least one must remain active. Use cancel subscription instead.")
            # A paid next-period selection is immutable: reject edits that
            # would change a purchased future snapshot.
            if (subscription.get("billing_mode") or "").lower() == "monthly_prepaid":
                currentEnd = subscription.get("current_period_end")
                if currentEnd:
                    paidFuture = (
                        self.client.table("Invoices")
                        .select("id, metadata_json")
                        .eq("userId", userId)
                        .eq("billing_reason", "renewal")
                        .eq("period_start", currentEnd)
                        .eq("status", "PAID")
                        .limit(1)
                        .execute()
                        .data
                    )
                    # Revoked (refunded) future coverage does not block
                    # removals: only a still-valid paid future selection is
                    # immutable.
                    blockingFuture = None
                    for candidate in paidFuture or []:
                        candidateMetadata = candidate.get("metadata_json")
                        manualBilling = (
                            candidateMetadata.get("manualBilling")
                            if isinstance(candidateMetadata, dict)
                            else None
                        ) or {}
                        if manualBilling.get("coverageState") != "revoked":
                            blockingFuture = candidate
                            break
                    if blockingFuture:
                        raise CustomException(
                            ValueError(
                                "The next period is already paid; its expert "
                                "selection cannot change."
                            ),
                            statusCode=409,
                            uiMessage=(
                                "Your next month is already paid with its "
                                "current experts. Removals apply to an unpaid "
                                "next period only."
                            ),
                        )
            pendingRemovals.extend(normalizedDomains)
            self.client.table("subscriptions").update({
                "pending_removals": pendingRemovals
            }).eq("id", subscription["id"]).execute()
            self._markPayableRenewalInvoicesForRepricing(subscription["id"])
            domainCount = subscriptionDomainCount(subscription) or len(currentExperts)
            for domain in normalizedDomains:
                self._auditLog(
                    userId, "domain.remove_scheduled",
                    status="ACTIVE",
                    metadata={
                        "domain": domain,
                        "currentDomainCount": domainCount,
                        "effectiveAt": "cycle_end",
                    }
                )
            logger.info(f"Domains {normalizedDomains} scheduled for removal at cycle_end for user {userId}")
            return {
                "currentDomains": currentExperts,
                "pendingRemovals": pendingRemovals,
                "effectiveAt": "cycle_end",
            }
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception




    def cancelPendingAddition(self, domain: str, token: str) -> dict:
        """
        Cancel an unpaid pending domain addition durably.

        The whole shared-order bundle for the original order is closed with
        a cancelled state (evidence preserved), so a later capture can never
        activate experts from that attempt. A newly priced attempt may be
        created for any remaining desired selection.

        Args:
            domain (str): The domain to cancel.
            token (str): Authorization token.

        Returns:
            dict: Cancellation result with the closed bundle evidence.
        """
        try:
            normalizedDomain = self._normalizeSingleDomain(domain)
            decodedToken = jwt.decode(
                token,
                os.environ["SECRET_KEY"],
                algorithms=["HS256"]
            )
            userId = decodedToken.get("userId")
            userRecord = self.client.table("Users") \
                .select("userId") \
                .eq("userId", userId) \
                .execute()
            if not userRecord.data:
                raise Exception("User not found")
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            pendingAdditions = subscriptionPendingAdditions(subscription)
            targetItem = None
            for item in pendingAdditions:
                if (
                    item["domain"] == normalizedDomain
                    and item.get("state") not in ("activated", "cancelled", "expired")
                ):
                    targetItem = item
                    break
            if targetItem is None:
                raise Exception(f"No cancellable pending addition found for domain '{normalizedDomain}'")
            if targetItem.get("state") == "paid_captured":
                raise CustomException(
                    ValueError("This addition was already paid and captured"),
                    statusCode=409,
                    uiMessage=(
                        "This addition payment was captured and needs support "
                        "review; contact us for a refund."
                    ),
                )
            cancelledOrderId = targetItem.get("orderId")
            cancelledAt = utcNow().isoformat()
            # Close the WHOLE shared-order attempt durably: a later capture
            # against the old order cannot activate any of its domains.
            closedDomains = []
            for item in pendingAdditions:
                if (
                    item.get("orderId") == cancelledOrderId
                    and item.get("state") not in ("activated", "cancelled", "expired")
                ):
                    item["state"] = "cancelled"
                    item["cancelledAt"] = cancelledAt
                    closedDomains.append(item["domain"])
            self.client.table("subscriptions").update({
                "pending_additions": pendingAdditions
            }).eq("id", subscription["id"]).execute()
            # Void the shared invoice so its Pay action disappears and a new
            # session against it is rejected.
            if cancelledOrderId:
                payable = (
                    self.client.table("Invoices")
                    .select("id, status, metadata_json")
                    .eq("razorpay_order_id", cancelledOrderId)
                    .in_("status", ["UPCOMING", "PAYMENT_PENDING"])
                    .limit(1)
                    .execute()
                    .data
                )
                for invoice in payable or []:
                    existingMetadata = invoice.get("metadata_json")
                    metadata = dict(existingMetadata) if isinstance(existingMetadata, dict) else {}
                    metadata["voidReason"] = "pending_addition_cancelled"
                    metadata["voidedAt"] = cancelledAt
                    metadata["closedAttemptDomains"] = closedDomains
                    self.client.table("Invoices").update({
                        "status": "VOID",
                        "metadata_json": metadata,
                    }).eq("id", invoice["id"]).execute()
            self._auditLog(
                userId, "domain.add_cancelled",
                status="CANCELLED",
                metadata={
                    "domain": normalizedDomain,
                    "closedAttemptDomains": closedDomains,
                    "orderId": cancelledOrderId,
                    "currentDomainCount": subscriptionDomainCount(subscription),
                    "effectiveAt": "immediate",
                    "durableClosure": True,
                }
            )
            logger.info(
                f"Pending addition bundle cancelled for domain '{normalizedDomain}', "
                f"user {userId}, order {cancelledOrderId}"
            )
            return {
                "domain": normalizedDomain,
                "cancelled": True,
                "closedAttemptDomains": closedDomains,
                "orderId": cancelledOrderId,
            }
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def cancelSubscription(self, reason: str | None, token: str) -> dict:
        """
        Explicit cancellation with mode-specific policy.

        Monthly: an explicit opt-out of future renewal with an optional
        reason. Keeps every already-paid day, voids unpaid renewal attempts,
        suppresses reminders, and returns the exact final paid end. Status
        stays in its paid phase; the flag carries the intent.

        Annual: preserves the existing policy — mandatory reason and the
        cancelled status at cycle end.

        Args:
            reason (str | None): Optional monthly reason; required for annual.
            token (str): Authorization token.

        Returns:
            dict: Cancellation confirmation with exact effective end.
        """
        try:
            decodedToken = jwt.decode(
                token,
                os.environ["SECRET_KEY"],
                algorithms=["HS256"]
            )
            userId = decodedToken.get("userId")
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            currentStatus = (subscription.get("status") or "").lower()
            billingMode = (subscription.get("billing_mode") or "none").lower()

            if billingMode == "monthly_prepaid":
                from api.services.billing.manualBillingRepository import getManualBillingRepository
                row = getManualBillingRepository().setRenewalOptOut(userId, True, reason, "cancel:"+userId)
                state = subscriptionBillingState(subscription).get("manualBilling", {})
                end = max(filter(None, [parseUtc(row.get("current_period_end")), parseUtc(state.get("paidFutureEnd"))]))
                return {"cancelled": True, "renewalOptOut": True, "effectiveAt": end.isoformat(),
                        "cancellationReason": row.get("cancellation_reason"), "refundInitiated": False,
                        "currentPeriod": {"start":subscription.get("current_period_start"),"end":subscription.get("current_period_end")}}

            # Annual: existing policy — mandatory reason, status cancelled.
            if not reason or not isinstance(reason, str) or not reason.strip():
                raise Exception("Cancellation reason is required")
            reason = reason.strip()
            if not self._isSubscriptionActive(currentStatus):
                raise Exception("No active subscription found for this user")
            planType = mapBillingModeToPlanType(billingMode, "cancelled")
            self.client.table("subscriptions").update({
                "status": "cancelled",
                "plan_type": planType,
                "auto_renew_enabled": False,
                "cancellation_reason": reason,
            }).eq("id", subscription["id"]).execute()
            self._auditLog(
                userId, "subscription.cancellation_scheduled",
                status="CANCELLED",
                metadata={"cancel_at_cycle_end": True, "cancellationReason": reason}
            )
            logger.info(f"Subscription scheduled for cancellation at cycle end for user {userId}")
            newToken = self._reissueTokenWithUpdatedClaims(token, "cancelled", planType)
            return {
                "cancelled": True,
                "effectiveAt": "cycle_end",
                "cancellationReason": reason,
                "accessToken": newToken,
            }
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def _voidPayableMonthlyRenewalInvoices(self, subscription: dict, userId: str) -> list[str]:
        """Void unpaid monthly renewal invoices/attempts on cancellation.

        Keeps the audit trail (VOID + reason); never deletes evidence.
        """
        payable = (
            self.client.table("Invoices")
            .select("id, status, metadata_json")
            .eq("userId", userId)
            .eq("billing_reason", "renewal")
            .in_("status", ["UPCOMING", "PAYMENT_PENDING"])
            .execute()
            .data
        )
        voided = []
        for invoice in payable or []:
            existingMetadata = invoice.get("metadata_json")
            metadata = dict(existingMetadata) if isinstance(existingMetadata, dict) else {}
            metadata["voidReason"] = "subscription_cancelled"
            metadata["voidedAt"] = utcNow().isoformat()
            self.client.table("Invoices").update({
                "status": "VOID",
                "metadata_json": metadata,
            }).eq("id", invoice["id"]).execute()
            voided.append(invoice["id"])
        return voided

    def resumeRenewal(self, token: str) -> dict:
        """
        Clear the monthly renewal opt-out before the final paid end.

        Never charges, never reactivates a void order, and never revives
        refunded coverage. A fresh invoice revision may be prepared when
        already within the T-7 window.
        """
        try:
            decodedToken = jwt.decode(
                token,
                os.environ["SECRET_KEY"],
                algorithms=["HS256"]
            )
            userId = decodedToken.get("userId")
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            if (subscription.get("billing_mode") or "").lower() != "monthly_prepaid":
                raise CustomException(
                    ValueError("Resume renewal is a monthly-only action"),
                    statusCode=403,
                    uiMessage="Resume renewal applies to monthly subscriptions.",
                )
            from api.services.billing.manualBillingRepository import getManualBillingRepository
            repeated = not bool(subscription.get("renewal_opt_out"))
            row = getManualBillingRepository().setRenewalOptOut(userId, False, None, "resume:"+userId)
            state = subscriptionBillingState(subscription).get("manualBilling", {})
            end = max(filter(None,[parseUtc(row.get("current_period_end")),parseUtc(state.get("paidFutureEnd"))]))
            if not repeated and utcNow() >= parseUtc(row["current_period_end"]) - datetime.timedelta(days=7):
                self.prepareRenewalInvoice(token)
            return {"renewalOptOut":False,"repeated":repeated,"effectiveAt":end.isoformat(),"creditsRefilled":False}
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def initiateRefund(self, token: str, paymentId: str, amount: int | None = None) -> dict:
        """
        Compatibility surface for the legacy user-facing refund route.

        Refunds are exceptional, staff-approved actions following a support
        email case. Ordinary users never execute refunds here: the endpoint
        returns the support-contact outcome and executes nothing, even when
        the payment is owned by the caller. Staff uses the controlled
        billing-admin refund service.
        """
        try:
            if not paymentId:
                raise Exception("paymentId is required")
            decodedToken = jwt.decode(
                token,
                os.environ["SECRET_KEY"],
                algorithms=["HS256"]
            )
            userId = decodedToken.get("userId")
            if not userId:
                raise Exception("Invalid token payload")
            self._auditLog(
                userId, "refund.user_request_rejected",
                paymentId=paymentId,
                status="SUPPORT_CONTACT_REQUIRED",
                metadata={
                    "reason": "self_service_refunds_disabled",
                    "requestedAmount": amount,
                }
            )
            raise CustomException(
                ValueError("Subscription refunds require support approval"),
                statusCode=403,
                uiMessage=(
                    "Refunds are handled by our support team after review. "
                    "Please email support with your reason; approved unused "
                    "time is returned by our team."
                ),
            )
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def createAnnualRenewalPaymentSession(self, invoiceId: str, token: str,
                                         requestKey: str | None = None) -> dict:
        decoded = jwt.decode(token, os.environ["SECRET_KEY"], algorithms=["HS256"])
        return self._reservedCheckout(decoded, 'renewal', 'annual_prepaid',
            {'invoiceId': invoiceId}, requestKey)

    def verifyAnnualRenewalPayment(self, payload: dict, token: str) -> dict:
        return self._verifyDurableCheckout(payload, token, 'renewal', 'annual_prepaid')

    def getInvoices(self, token: str) -> list[dict]:
        """
        Retrieve all invoices for the authenticated user.

        Args:
            token (str): Authorization token.

        Returns:
            list[dict]: List of invoice records ordered by creation date descending.
        """
        try:
            decodedToken = jwt.decode(
                token,
                os.environ["SECRET_KEY"],
                algorithms = ["HS256"]
            )
            userId = decodedToken.get("userId")
            invoiceFields = (
                "id, userId, status, amount, currency, razorpayPaymentId, "
                "razorpay_order_id, subscription_id, billing_reason, payment_flow, "
                "requires_customer_auth, due_date, expires_at, period_start, period_end, "
                "amount_before_tax, tax_amount, total_amount, tax_breakdown_json, "
                "tax_rule_version, place_of_supply_snapshot, pricing_version, "
                "pricing_reference_snapshot_json, metadata_json, paidAt, createdAt, "
                "created_at, updated_at"
            )
            result = self.client.table("Invoices") \
                .select(invoiceFields) \
                .eq("userId", userId) \
                .order("createdAt", desc=True) \
                .execute()
            return result.data
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def _reservedCheckout(self, decoded, purpose, mode, payload, requestKey=None, contact=None):
        from api.services.billing.manualBillingContracts import CheckoutRequest
        from api.services.billing.manualBillingRepository import getManualBillingRepository
        repository = getManualBillingRepository()
        userId = decoded['userId']
        try:
            repository.ensureCanonicalSubscription(userId)
            from api.services.billing.manualPaymentService import ManualPaymentService
            try:
                intent = ManualPaymentService.forProduction(self.razorpayClient, repository).createCheckout(
                    CheckoutRequest(userId, purpose, mode, payload, requestKey))
            except (RuntimeError, TimeoutError, ConnectionError) as exc:
                raise CustomException(exc, statusCode=503,
                    uiMessage='Checkout creation is being reconciled. Please try again later.') from exc
            identity = self._resolveCheckoutIdentity(userId, decoded.get('email'))
            if contact is not None:
                identity['contact'] = self._normalizePhone(contact)
            return {'userId': userId, 'userEmail': identity['email'], 'userName': identity['name'],
                'userContact': identity['contact'], 'razorpayKey': os.environ['RAZORPAY_KEY_ID'],
                'orderId': intent.razorpayOrderId, 'invoiceId': intent.invoiceId, 'attemptId': intent.attemptId,
                'amount': intent.amount, 'currency': intent.currency, 'billingMode': mode,
                'expiresAt': intent.expiresAt.isoformat(), 'state': 'payment_pending',
                'domains': intent.snapshot.get('domains') or [], 'quantity': len(intent.snapshot.get('domains') or []),
                'tokens': intent.snapshot.get('tokens'), 'packId': intent.snapshot.get('packId'),
                'period': {'start': intent.snapshot.get('periodStart'), 'end': intent.snapshot.get('periodEnd'),
                           'estimated': purpose == 'initial_purchase'}}
        except CustomException:
            raise
        except ValueError as exc:
            marker = str(exc)
            status = 422 if marker.startswith('INVALID_') else 404 if marker == 'OWNED_INVOICE_NOT_FOUND' else 403 if marker in ('CHECKOUT_OWNER_NOT_ELIGIBLE', 'PAID_COVERAGE_REQUIRED') else 409
            raise CustomException(exc, statusCode=status, uiMessage='Checkout request cannot be completed.', errorCode=marker) from exc

    # -- generic manual renewal routes ------------------------------------------







    def _verifyDurableCheckout(self, payload, token, purpose, mode=None):
        from api.services.billing.manualBillingRepository import getManualBillingRepository
        from api.services.billing.manualPaymentService import ManualPaymentService
        try:
            decoded = jwt.decode(token, os.environ['SECRET_KEY'], algorithms=['HS256'])
            userId = decoded.get('userId')
            orderId, paymentId, signature = (payload.get(key) for key in
                ('razorpayOrderId', 'razorpayPaymentId', 'razorpaySignature'))
            if not all((userId, orderId, paymentId, signature)):
                raise ValueError('INVALID_VERIFICATION_FIELDS')
            if not ManualPaymentService.verifyCheckoutSignature(orderId, paymentId, signature):
                raise ValueError('INVALID_CHECKOUT_SIGNATURE')
            repository = getManualBillingRepository()
            attempt = repository.attemptForOrder(orderId)
            if attempt['user_id'] != userId:
                raise CustomException(ValueError('ORDER_OWNERSHIP_MISMATCH'), statusCode=403,
                    uiMessage='This payment belongs to another account.')
            frozen = repository._json(attempt.get('metadata_json'))['manualBilling']
            if frozen['purpose'] != purpose or (mode and frozen['billingMode'] != mode):
                raise ValueError('CHECKOUT_PURPOSE_OR_MODE_MISMATCH')
            if payload.get('invoiceId') and str(payload['invoiceId']) != str(attempt['invoice_id']):
                raise ValueError('INVOICE_ATTEMPT_MISMATCH')
            order = self.razorpayClient.order.fetch(orderId)
            if (not order or order.get('id') != orderId
                    or int(order.get('amount', -1)) != int(attempt['amount'])
                    or str(order.get('currency', '')).upper() != str(attempt['currency']).upper()):
                raise ValueError('PROVIDER_ORDER_EVIDENCE_MISMATCH')
            payment = self.razorpayClient.payment.fetch(paymentId)
            result = self._finalizeManualCheckout(str(attempt['invoice_id']), orderId, paymentId, payment)
            if result['finalized'] and purpose in ('initial_purchase', 'renewal'):
                result['accessToken'] = self._reissueTokenWithUpdatedClaims(token, 'active',
                    'annual' if frozen['billingMode'] == 'annual_prepaid' else 'pro')
            return result
        except CustomException:
            raise
        except ValueError as exc:
            raise CustomException(exc, statusCode=400, uiMessage='Payment verification failed.',
                errorCode=str(exc)) from exc
        except Exception as exc:
            raise CustomException(exc) from exc

    def _finalizeManualCheckout(self, invoiceId, orderId, paymentId, paymentEntity, now=None):
        from api.services.billing.manualBillingRepository import getManualBillingRepository
        from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
        repository = getManualBillingRepository()
        attempt = repository.attemptForOrder(orderId)
        if str(attempt["invoice_id"]) != str(invoiceId):
            raise ValueError("INVOICE_ATTEMPT_MISMATCH")
        frozen = repository._json(attempt.get("metadata_json")).get("manualBilling", {})
        if paymentEntity.get("order_id") != orderId or paymentEntity.get("id") != paymentId:
            raise ValueError("PAYMENT_ORDER_MISMATCH")
        from api.services.billing.manualPaymentService import ManualPaymentService
        result = ManualPaymentService.forProduction(self.razorpayClient, repository).finalizeCapturedPayment(VerifiedPaymentEvidence(
            str(attempt["id"]), str(invoiceId), attempt["user_id"], orderId, paymentId,
            frozen["purpose"], str(paymentEntity.get("currency") or ""),
            str(paymentEntity.get("status") or "").lower(), "server_observation",
            int(paymentEntity.get("amount") or 0), now or utcNow(), None, None, False))
        def period(value):
            if value is None: return None
            return {"start": value.start.isoformat(), "end": value.end.isoformat(),
                    "domains": list(value.domains), "creditPeriodId": value.creditPeriodId}
        return {"verified": True, "finalized": result.finalized, "state": result.state,
                "alreadyFinalized": result.state == "already_finalized",
                "creditsRefilled": result.creditsRefilled, "currentPeriod": period(result.currentPeriod),
                "nextPeriod": period(result.nextPeriod), "anomalyId": result.anomalyId,
                "invoiceStatus": "PAID" if result.finalized else "PAYMENT_PENDING"}

    def prepareRenewalInvoice(self, token: str) -> dict:
        """
        Prepare (or read) the next unpaid renewal invoice on explicit request.

        Monthly: on-demand preparation earlier than T-7 is allowed while
        paid access is valid and renewal has not been declined. Annual:
        delegates to the existing annual preparation policy.
        """
        try:
            decodedToken = jwt.decode(
                token, os.environ["SECRET_KEY"], algorithms=["HS256"]
            )
            userId = decodedToken.get("userId")
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            billingMode = (subscription.get("billing_mode") or "").lower()
            if billingMode == "monthly_prepaid":
                rows=self.client.table("Invoices").select("id,period_start,period_end,metadata_json").eq("userId",userId).eq("subscription_id",subscription["id"]).eq("billing_reason","renewal").eq("status","PAID").eq("period_start",subscription["current_period_end"]).execute().data or []
                for paid in rows:
                    frozen=(paid.get("metadata_json") or {}).get("manualBilling") or {}
                    if frozen.get("coverageState") != "revoked":
                        return {"invoiceId":paid["id"],"state":"paid_scheduled","invoiceStatus":"PAID",
                            "creditsRefilled":False,"nextPeriod":{"start":paid["period_start"],"end":paid["period_end"],"domains":frozen.get("domains",[])}}
                from api.services.billing.monthlyCoverageService import (
                    MonthlyCoverageService,
                )

                result = MonthlyCoverageService().prepareRenewalInvoice(
                    userId=userId,
                    subscription=subscription,
                    now=utcNow(),
                )
                if result["state"] == "payment_pending":
                    # Persist the prepared revision so the dashboard can pay it.
                    result["invoiceId"] = self._persistMonthlyRenewalInvoice(userId, subscription, result)
                return result
            if billingMode == "annual_prepaid":
                from api.services.billing.invoiceService import (
                    createUpcomingRenewalInvoice,
                )

                userRows = (
                    self.client.table("Users")
                    .select("userId, email, fullName")
                    .eq("userId", userId)
                    .limit(1)
                    .execute()
                    .data
                )
                if not userRows:
                    raise Exception("User not found")
                invoice = createUpcomingRenewalInvoice(subscription, userRows[0])
                return {
                    "invoiceId": (invoice or {}).get("id"),
                    "state": "invoice_ready" if invoice else "not_eligible",
                    "creditsRefilled": False,
                }
            raise CustomException(
                ValueError("No active paid subscription for renewal preparation"),
                statusCode=403,
                uiMessage="This action requires an active subscription.",
            )
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def _persistMonthlyRenewalInvoice(
        self, userId: str, subscription: dict, prepared: dict
    ) -> str | None:
        """Persist the prepared monthly renewal invoice revision."""
        nextPeriod = prepared.get("nextPeriod") or {}
        if not nextPeriod.get("start"):
            return
        lifecycleId = self._ensureLifecycleId(subscription)
        pendingRemovals = set(subscriptionPendingRemovals(subscription))
        currentExperts = subscriptionExperts(subscription)
        renewalDomains = [d for d in currentExperts if d not in pendingRemovals]
        domainCount = max(len(renewalDomains), 1)
        snapshot = computeInvoiceSnapshot(
            billingMode="monthly_prepaid",
            billingReason="renewal",
            domainCount=domainCount,
            customerState=None,
            periodStart=parseUtc(nextPeriod["start"]),
            periodEnd=parseUtc(nextPeriod["end"]),
        )
        invoice = self._createFrozenInvoiceFromSnapshot(
            userId=userId,
            subscriptionId=subscription.get("id"),
            billingReason="renewal",
            paymentFlow="razorpay_order_checkout",
            requiresCustomerAuth=True,
            snapshot=snapshot,
            expectedSubscriptionVersion=int(subscription["version"]),
            metadata={
                "flow": "prepareRenewalInvoice",
                "billingMode": "monthly_prepaid",
                "manualBilling": {
                    "schemaVersion": 1,
                    "lifecycleId": lifecycleId,
                    "purpose": "renewal",
                    "billingMode": "monthly_prepaid",
                    "domains": renewalDomains,
                    "coverageState": "scheduled",
                    "revision": 1,
                },
            },
        )
        return invoice["id"]

    def createRenewalPaymentSession(self, invoiceId: str, token: str,
                                   requestKey: str | None = None) -> dict:
        decoded = jwt.decode(token, os.environ["SECRET_KEY"], algorithms=["HS256"])
        subscription = self._getCanonicalSubscription(decoded['userId'], required=True)
        return self._reservedCheckout(decoded, 'renewal', subscription['billing_mode'],
            {'invoiceId': invoiceId}, requestKey)

    def verifyRenewalPayment(self, payload: dict, token: str) -> dict:
        return self._verifyDurableCheckout(payload, token, 'renewal')

    def _finalizeCapturedManualRenewal(
        self, invoiceId, orderId, paymentId, paymentEntity, subscription=None, now=None,
    ) -> dict:
        """Browser and webhook share one atomic capture/grant transaction."""
        return self._finalizeManualCheckout(invoiceId, orderId, paymentId, paymentEntity, now)

    def _finalizeCapturedInitialPurchase(self, invoiceId, orderId, paymentId, paymentEntity, userId):
        return self._finalizeManualCheckout(invoiceId, orderId, paymentId, paymentEntity)


subscriptionService = SubscriptionService()
