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

    def _finalizeCapturedAnnualRenewalPayment(
        self,
        invoice: dict,
        subscription: dict,
        payment: dict,
        userId: str,
    ) -> dict:
        """
        Finalize a dashboard annual renewal after server-side Razorpay capture validation.
        """
        invoiceId = invoice["id"]
        invoiceStatus = (invoice.get("status") or "").lower()
        if invoiceStatus == "paid":
            return {
                "verified": True,
                "finalized": True,
                "alreadyFinalized": True,
                "invoiceStatus": "PAID",
                "awaitingWebhookFinalization": False,
            }

        paymentId = payment.get("id")
        orderId = payment.get("order_id") or invoice.get("razorpay_order_id")
        previousExpiry = parseUtc(subscription.get("current_period_end")) or utcNow()
        previousExpiryNaive = previousExpiry.replace(tzinfo=None)
        newExpiry = previousExpiryNaive + relativedelta(years=1)
        paidAtDt = utcFromTimestamp(payment.get("captured_at")) or utcNow()
        paidAt = paidAtDt.isoformat()
        existingMetadata = invoice.get("metadata_json")
        metadata = dict(existingMetadata) if isinstance(existingMetadata, dict) else {}
        metadata.update({
            "flow": "annual_renewal_dashboard_verify",
            "verified": True,
            "verifiedAt": utcNow().isoformat(),
            "finalized": True,
            "finalizedAt": paidAt,
            "awaitingWebhookFinalization": False,
        })

        self.client.table("subscriptions").update({
            "status": "active",
            "plan_type": "annual",
            "current_period_start": previousExpiryNaive.isoformat(),
            "current_period_end": newExpiry.isoformat(),
            "renewal_due_at": newExpiry.isoformat(),
        }).eq("id", subscription["id"]).execute()

        self.client.table("Invoices").update({
            "status": "PAID",
            "razorpay_order_id": orderId,
            "razorpayPaymentId": paymentId,
            "paidAt": paidAt,
            "metadata_json": metadata,
        }).eq("id", invoiceId).execute()

        previousStatus = subscription.get("status", "")
        self._auditLog(
            userId,
            "billing.annual_renewal_charged",
            paymentId=paymentId,
            amount=payment.get("amount"),
            currency=payment.get("currency", "INR"),
            status="CHARGED",
            metadata={
                "invoiceId": invoiceId,
                "orderId": orderId,
                "previousExpiry": str(previousExpiryNaive),
                "newExpiry": str(newExpiry),
                "flow": "dashboard_verify_captured",
                "restoredFrom": previousStatus if previousStatus in ("past_due", "suspended") else None,
            },
        )

        try:
            from api.services.credits.creditService import creditService
            creditService.resetMonthlyTokens(userId)
        except Exception as creditErr:
            logger.warning(f"Credit reset failed for annual renewal userId={userId}: {creditErr}")

        logger.info(
            f"Annual renewal finalized from dashboard verify for user {userId}, "
            f"invoice {invoiceId}, new expiry {newExpiry}"
        )
        return {
            "verified": True,
            "finalized": True,
            "invoiceStatus": "PAID",
            "awaitingWebhookFinalization": False,
        }

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
            self._upsertCanonicalSubscription(
                userId=userId,
                billingMode="none",
                status="trial",
                currentPeriodStart=currentTime.isoformat(),
                currentPeriodEnd=trialExpiry.isoformat(),
                renewalDueAt=trialExpiry.isoformat(),
                autoRenewEnabled=False,
                paymentCollectionMode="authenticated_checkout",
                subscribedExperts=experts,
                domainCount=4,
                pendingRemovals=[],
                pendingAdditions=[],
                planType="free",
            )
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

    def createSubscription(self, domains: list[str], contact: str, token: str, billingMode: str = "monthly_prepaid") -> dict:
        """
        Create a customer-free Razorpay Order for manual subscription checkout.

        Each purchase is a one-time checkout; no Razorpay Customer object is
        created, fetched, or bound, and no recurring token is enrolled.

        Args:
            domains (list[str]): Domain expert names the user is subscribing to.
            contact (str): User's phone number for checkout prefill only.
            token (str): Authorization token.

        Returns:
            dict: Data required to open Razorpay checkout.
        """
        try:
            normalizedDomains = self._normalizeAndValidateDomains(domains)
            normalizedBillingMode = self._normalizeBillingMode(billingMode)
            decodedToken = jwt.decode(
                token,
                os.environ["SECRET_KEY"],
                algorithms=["HS256"]
            )
            userId = decodedToken.get("userId")
            tokenEmail = decodedToken.get("email")
            normalizedContact = self._normalizePhone(contact)
            self.client.table("Users").update({"phoneNumber": normalizedContact}).eq("userId", userId).execute()
            identity = self._resolveCheckoutIdentity(userId, tokenEmail)
            quantity = len(normalizedDomains)
            subscription = self._getCanonicalSubscription(userId=userId, required=False)
            if self._blocksNewCheckout(subscription, utcNow()):
                raise CustomException(
                    ValueError("An active paid period already exists for this user"),
                    statusCode=409,
                    uiMessage=(
                        "You already have subscription access until the current "
                        "billing period ends."
                    )
                )
            if normalizedBillingMode == "monthly_prepaid":
                return self._createManualInitialCheckout(userId, normalizedDomains, identity)
            snapshot = computeInvoiceSnapshot(
                billingMode=normalizedBillingMode,
                billingReason="initial_purchase",
                domainCount=quantity,
                customerState=None,
            )
            lifecycleId = self._ensureLifecycleId(subscription)
            invoice = self._createFrozenInvoiceFromSnapshot(
                userId=userId,
                subscriptionId=subscription.get("id") if subscription else None,
                billingReason="initial_purchase",
                paymentFlow="razorpay_order_checkout",
                requiresCustomerAuth=True,
                snapshot=snapshot,
                metadata={
                    "domains": normalizedDomains,
                    "flow": "createSubscription",
                    "billingMode": normalizedBillingMode,
                    "manualBilling": {
                        "schemaVersion": 1,
                        "lifecycleId": lifecycleId,
                        "purpose": "initial_purchase",
                        "billingMode": normalizedBillingMode,
                        "domains": normalizedDomains,
                        "coverageState": "estimated",
                    },
                },
            )
            orderPayload = {
                "amount": snapshot.total_amount,
                "currency": "INR",
                "notes": {
                    "userId": userId,
                    "type": "initial_subscription",
                    "domains": ", ".join(normalizedDomains),
                    "invoiceId": invoice["id"],
                    "billingMode": normalizedBillingMode,
                },
            }
            order = self.razorpayClient.order.create(orderPayload)
            self._attachOrderToInvoice(invoiceId=invoice["id"], orderId=order["id"])
            self._auditLog(
                userId, "subscription.created",
                status="CREATED",
                metadata={
                    "orderId": order["id"],
                    "invoiceId": invoice["id"],
                    "billingMode": normalizedBillingMode,
                    "domains": normalizedDomains,
                    "quantity": quantity,
                    "amount": snapshot.total_amount,
                }
            )
            return {
                "userId": userId,
                "userEmail": identity["email"],
                "userContact": identity["contact"],
                "userName": identity["name"],
                "razorpayKey": os.environ["RAZORPAY_KEY_ID"],
                "orderId": order["id"],
                "status": order["status"],
                "quantity": quantity,
                "domains": normalizedDomains,
                "invoiceId": invoice["id"],
                "billingMode": normalizedBillingMode,
                "expiresAt": None,
                "state": "payment_pending",
                "period": {
                    "start": snapshot.period_start,
                    "end": snapshot.period_end,
                    "estimated": True,
                },
                "pricingSnapshot": {
                    "pricingVersion": snapshot.pricing_version,
                    "priceSource": snapshot.pricing_reference_snapshot_json.get("source"),
                },
                "taxSnapshot": {
                    "taxRuleVersion": snapshot.tax.tax_rule_version,
                    "amountBeforeTax": snapshot.amount_before_tax,
                    "taxAmount": snapshot.tax.tax_amount,
                    "totalAmount": snapshot.total_amount,
                    "currency": snapshot.currency,
                },
            }
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception
        
    def verifySubscription(self, payload: dict, token: str) -> dict:
        """
        Verify Razorpay Order checkout signature and activate the subscription.

        Performs HMAC SHA256 verification using Razorpay API secret. On success,
        extracts the saved token (mandate) from the payment entity, sets cycle
        dates using the Anchor Date strategy, and activates the subscription.

        Args:
            payload (dict): Razorpay checkout response payload.
            token (str): Authorization token.
        """
        try:
            decodedToken = jwt.decode(
                token,
                os.environ["SECRET_KEY"],
                algorithms=["HS256"]
            )
            userId = decodedToken.get("userId")
            paymentId = payload.get("razorpayPaymentId")
            orderId = payload.get("razorpayOrderId")
            signature = payload.get("razorpaySignature")
            if not all([paymentId, orderId, signature, userId]):
                raise Exception("Missing Razorpay verification fields")
            message = f"{orderId}|{paymentId}"
            expectedSignature = hmac.new(
                os.environ["RAZORPAY_KEY_SECRET"].encode(),
                message.encode(),
                hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expectedSignature, signature):
                raise Exception("Invalid Razorpay signature")
            order = self.razorpayClient.order.fetch(orderId)
            orderNotesRaw = order.get("notes", {}) or {}
            orderNotes = orderNotesRaw if isinstance(orderNotesRaw, dict) else {}
            orderUserId = orderNotes.get("userId")
            if orderUserId and orderUserId != userId:
                raise Exception(
                    f"Order/user mismatch during verification: order.userId={orderUserId}, "
                    f"token.userId={userId}"
                )
            orderDomains = [d.strip() for d in (orderNotes.get("domains", "") or "").split(",") if d.strip()]
            if not orderDomains:
                raise Exception(
                    "Order metadata is missing domains. "
                    "Client-provided domains are no longer accepted."
                )
            normalizedDomains = self._normalizeAndValidateDomains(orderDomains)
            billingMode = self._normalizeBillingMode(orderNotes.get("billingMode") or "monthly_recurring")
            orderType = orderNotes.get("type", "")
            if orderType != "initial_subscription":
                raise Exception(
                    f"Order {orderId} is not an initial subscription order "
                    f"(type={orderType}). Use the dedicated renewal verify endpoint."
                )
            invoiceId = orderNotes.get("invoiceId")
            if not invoiceId:
                raise PaymentValidationError("Initial subscription order is missing invoiceId")
            if billingMode == "monthly_prepaid":
                payment = self.razorpayClient.payment.fetch(paymentId)
                rows=self.client.table("Invoices").select("*").eq("id",invoiceId).eq("userId",userId).limit(1).execute().data
                if not rows:
                    raise PaymentValidationError("Invoice ownership mismatch")
                validateOrderPaymentAgainstInvoice(order=order,payment=payment,invoice=rows[0],
                    expectedType="initial_subscription",expectedUserId=userId,requestOrderId=orderId,requireCaptured=False)
                result = self._finalizeManualCheckout(invoiceId, orderId, paymentId, payment)
                if result["finalized"]:
                    result["accessToken"] = self._reissueTokenWithUpdatedClaims(token, "active", "pro")
                return result
            invoice = loadPayableInvoice(
                self.client,
                invoiceId=invoiceId,
                userId=userId,
                expectedBillingReason="initial_purchase",
            )
            payment = self.razorpayClient.payment.fetch(paymentId)
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            assertInvoiceBelongsToSubscription(invoice, subscription)
            validateOrderPaymentAgainstInvoice(
                order=order,
                payment=payment,
                invoice=invoice,
                expectedType="initial_subscription",
                expectedUserId=userId,
                expectedCustomerId=None,
                requestOrderId=orderId,
                requireCaptured=True,
            )
            currentTime = utcNow()
            lifecycleId = self._ensureLifecycleId(subscription)
            if billingMode == "monthly_prepaid":
                activationAt = currentTime
                expiry = activationAt + relativedelta(months=1)
                self._upsertCanonicalSubscription(
                    userId=userId,
                    billingMode="monthly_prepaid",
                    status="active",
                    currentPeriodStart=activationAt.isoformat(),
                    currentPeriodEnd=expiry.isoformat(),
                    renewalDueAt=expiry.isoformat(),
                    autoRenewEnabled=False,
                    paymentCollectionMode="authenticated_checkout",
                    subscribedExperts=normalizedDomains,
                    domainCount=len(normalizedDomains),
                    pendingRemovals=[],
                    pendingAdditions=[],
                    planType="pro",
                )
                canonical = self._getCanonicalSubscription(userId=userId, required=True)
                existingState = dict(subscriptionBillingState(canonical) or {})
                manualBillingState = existingState.get("manualBilling") or {}
                manualBillingState.update({
                    "schemaVersion": 1,
                    "lifecycleId": lifecycleId,
                    "activationAt": activationAt.isoformat(),
                    "finalPaidEnd": expiry.isoformat(),
                })
                existingState["manualBilling"] = manualBillingState
                self.client.table("subscriptions").update({
                    "billing_state": existingState,
                    "auto_renew_enabled": False,
                }).eq("id", canonical["id"]).execute()
                periodStartIso = activationAt.isoformat()
                periodEndIso = expiry.isoformat()
            else:
                activationAt = currentTime
                expiry = activationAt + relativedelta(years=1)
                self._upsertCanonicalSubscription(
                    userId=userId,
                    billingMode="annual_prepaid",
                    status="active",
                    currentPeriodStart=activationAt.isoformat(),
                    currentPeriodEnd=expiry.isoformat(),
                    renewalDueAt=expiry.isoformat(),
                    autoRenewEnabled=False,
                    paymentCollectionMode="authenticated_checkout",
                    subscribedExperts=normalizedDomains,
                    domainCount=len(normalizedDomains),
                    pendingRemovals=[],
                    pendingAdditions=[],
                    planType="annual",
                )
                canonical = self._getCanonicalSubscription(userId=userId, required=True)
                existingState = dict(subscriptionBillingState(canonical) or {})
                manualBillingState = existingState.get("manualBilling") or {}
                manualBillingState.update({
                    "schemaVersion": 1,
                    "lifecycleId": lifecycleId,
                    "activationAt": activationAt.isoformat(),
                    "finalPaidEnd": expiry.isoformat(),
                })
                existingState["manualBilling"] = manualBillingState
                self.client.table("subscriptions").update({
                    "billing_state": existingState,
                    "auto_renew_enabled": False,
                }).eq("id", canonical["id"]).execute()
                periodStartIso = activationAt.isoformat()
                periodEndIso = expiry.isoformat()
            if invoiceId:
                self._markInvoicePaid(
                    invoiceId=invoiceId,
                    paymentId=paymentId,
                    paidAt=str(utcNow()),
                )
                # Persist actual committed coverage dates + activation identity
                # on the invoice without changing the frozen amount/tax.
                invoiceRows = self.client.table("Invoices") \
                    .select("id, metadata_json") \
                    .eq("id", invoiceId) \
                    .limit(1) \
                    .execute().data
                if invoiceRows:
                    existingMetadata = invoiceRows[0].get("metadata_json")
                    metadata = dict(existingMetadata) if isinstance(existingMetadata, dict) else {}
                    manualBilling = dict(metadata.get("manualBilling") or {})
                    manualBilling.update({
                        "schemaVersion": 1,
                        "lifecycleId": lifecycleId,
                        "purpose": "initial_purchase",
                        "billingMode": billingMode,
                        "domains": normalizedDomains,
                        "coverageState": "active",
                        "activatedAt": activationAt.isoformat(),
                    })
                    metadata["manualBilling"] = manualBilling
                    self.client.table("Invoices").update({
                        "period_start": periodStartIso,
                        "period_end": periodEndIso,
                        "metadata_json": metadata,
                    }).eq("id", invoiceId).execute()
            self._auditLog(
                userId, "subscription.verified",
                paymentId=paymentId,
                status="ACTIVE",
                metadata={
                    "orderId": orderId,
                    "invoiceId": invoiceId,
                    "billingMode": billingMode,
                    "lifecycleId": lifecycleId,
                    "domains": normalizedDomains,
                    "quantity": len(normalizedDomains),
                    "activationAt": activationAt.isoformat(),
                }
            )
            planType = "pro" if billingMode == "monthly_prepaid" else "annual"
            try:
                from api.services.credits.creditService import creditService
                creditService.initializeCreditBalance(
                    userId=userId,
                    planTier=planType,
                    domainCount=len(normalizedDomains),
                )
            except Exception as creditErr:
                logger.warning(f"Credit initialization failed for paid user {userId}: {creditErr}")
            newToken = self._reissueTokenWithUpdatedClaims(token, "active", planType)
            return {"accessToken": newToken}
        except PaymentValidationError as e:
            exception = self._paymentValidationException(e)
            logger.error(exception)
            raise exception
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def addDomains(self, domains: list[str], token: str) -> dict:
        """
        Initiate adding one or more domains to an active subscription via
        a Razorpay Order for prorated charges.

        Domains are activated only after payment confirmation via the
        verifyDomainUpgrade() method.

        Args:
            domains (list[str]): The domain expert names to add.
            token (str): Authorization token.

        Returns:
            dict: Order details required for Razorpay embedded checkout.
        """
        try:
            normalizedDomains = self._normalizeAndValidateDomains(domains)
            decodedToken = jwt.decode(
                token,
                os.environ["SECRET_KEY"],
                algorithms=["HS256"]
            )
            userId = decodedToken.get("userId")
            tokenEmail = decodedToken.get("email")
            user = self.client.table("Users") \
                .select("userId") \
                .eq("userId", userId) \
                .execute().data
            if not user:
                raise Exception("User not found")
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            if subscription.get("billing_mode") == "monthly_prepaid":
                return self._createManualExpertCheckout(userId, normalizedDomains, subscription, tokenEmail)
            subscription = self._reconcilePendingAdditions(subscription)
            if not self._isSubscriptionActive(subscription.get("status")):
                raise Exception("Subscription must be active to add domains")
            currentExperts = subscriptionExperts(subscription)
            pendingAdditions = subscriptionPendingAdditions(subscription)
            activePending = [item["domain"] for item in pendingAdditions
                             if item.get("state") not in ("failed", "activated")]
            for d in normalizedDomains:
                if d in currentExperts:
                    raise Exception(f"Domain '{d}' is already in your subscription")
                if d in activePending:
                    staleItem = next(
                        (item for item in pendingAdditions
                         if item["domain"] == d and item.get("state") == "awaiting_payment"),
                        None
                    )
                    if staleItem:
                        staleOrderId = staleItem.get("orderId")
                        staleOrderStatus = None
                        if staleOrderId:
                            try:
                                staleOrder = self.razorpayClient.order.fetch(staleOrderId)
                                staleOrderStatus = staleOrder["status"]
                            except Exception:
                                staleOrderStatus = "fetch_failed"
                        if staleOrderStatus == "paid":
                            notes = staleOrder.get("notes", {})
                            self._activatePaidDomains(
                                userId=userId,
                                domains=[x.strip() for x in notes.get("domains", "").split(",") if x.strip()],
                                targetQuantity=int(notes.get("targetQuantity", 0)),
                                referenceId=staleOrderId,
                            )
                            subscription = self._getCanonicalSubscription(userId=userId, required=True)
                            currentExperts = subscriptionExperts(subscription)
                            pendingAdditions = subscriptionPendingAdditions(subscription)
                            if d in currentExperts:
                                continue
                        else:
                            pendingAdditions.remove(staleItem)
                            activePending = [item["domain"] for item in pendingAdditions
                                             if item.get("state") not in ("failed", "activated")]
                            self._auditLog(
                                userId, "domain.stale_pending_cleared",
                                status="CLEARED",
                                metadata={
                                    "domain": d,
                                    "staleOrderId": staleOrderId,
                                    "staleOrderStatus": staleOrderStatus,
                                }
                            )
                            logger.info(
                                f"Cleared stale pending addition for domain '{d}', "
                                f"order {staleOrderId} (status={staleOrderStatus})"
                            )
                    else:
                        raise Exception(f"Domain '{d}' already has a pending addition request")
            domainCount = subscriptionDomainCount(subscription) or len(currentExperts)
            totalAfterAdd = domainCount + len(activePending) + len(normalizedDomains)
            if totalAfterAdd > 4:
                raise Exception(f"Maximum of 4 domains. Current: {domainCount}, "
                                f"pending: {len(activePending)}, requested: {len(normalizedDomains)}")
            cycleStartRaw = subscription.get("current_period_start")
            subscriptionExpiryRaw = subscription.get("current_period_end")
            if not cycleStartRaw or not subscriptionExpiryRaw:
                raise Exception("Subscription period window is missing for proration")
            cycleStart = parser.isoparse(cycleStartRaw)
            subscriptionExpiry = parser.isoparse(subscriptionExpiryRaw)
            if cycleStart.tzinfo is None:
                cycleStart = cycleStart.replace(tzinfo=datetime.timezone.utc)
            else:
                cycleStart = cycleStart.astimezone(datetime.timezone.utc)
            if subscriptionExpiry.tzinfo is None:
                subscriptionExpiry = subscriptionExpiry.replace(tzinfo=datetime.timezone.utc)
            else:
                subscriptionExpiry = subscriptionExpiry.astimezone(datetime.timezone.utc)
            now = datetime.datetime.now(datetime.timezone.utc)
            billingMode = self._normalizeBillingMode(subscription.get("billing_mode") or "monthly_prepaid")
            identity = self._resolveCheckoutIdentity(userId, tokenEmail)
            snapshot = computeInvoiceSnapshot(
                billingMode=billingMode,
                billingReason="proration",
                domainCount=len(normalizedDomains),
                customerState=None,
                prorationAnchorStart=cycleStart,
                prorationAnchorEnd=subscriptionExpiry,
            )
            totalProrated = snapshot.total_amount
            perDomainProrated = int(totalProrated / max(len(normalizedDomains), 1))
            domainLabel = ", ".join(normalizedDomains)
            invoice = self._createFrozenInvoiceFromSnapshot(
                userId=userId,
                subscriptionId=subscription.get("id"),
                billingReason="proration",
                paymentFlow="razorpay_order_checkout",
                requiresCustomerAuth=(billingMode == "annual_prepaid"),
                snapshot=snapshot,
                metadata={
                    "domains": normalizedDomains,
                    "flow": "addDomains",
                    "billingMode": billingMode,
                },
            )
            order = self.razorpayClient.order.create({
                "amount": totalProrated,
                "currency": "INR",
                "notes": {
                    "userId": userId,
                    "domains": domainLabel,
                    "type": "domain_upgrade_proration",
                    "currentQuantity": str(domainCount),
                    "targetQuantity": str(totalAfterAdd),
                    "invoiceId": invoice["id"],
                    "billingMode": billingMode,
                },
            })
            self._attachOrderToInvoice(invoiceId=invoice["id"], orderId=order["id"])
            for d in normalizedDomains:
                pendingAdditions.append({
                    "domain": d,
                    "state": "awaiting_payment",
                    "orderId": order["id"],
                    "proratedAmount": perDomainProrated,
                    "requestedAt": str(now),
                })
            self.client.table("subscriptions").update({
                "pending_additions": pendingAdditions
            }).eq("id", subscription["id"]).execute()
            self._auditLog(
                userId, "domain.add_requested",
                amount=totalProrated,
                status="AWAITING_PAYMENT",
                metadata={
                    "domains": normalizedDomains,
                    "perDomainProrated": perDomainProrated,
                    "totalProrated": totalProrated,
                    "daysRemaining": max((subscriptionExpiry - now).days, 1),
                    "orderId": order["id"],
                    "invoiceId": invoice["id"],
                }
            )
            logger.info(f"Domain upgrade order created for user {userId}: {normalizedDomains}")
            return {
                "razorpayKey": os.environ["RAZORPAY_KEY_ID"],
                "orderId": order["id"],
                "currency": order["currency"],
                "upgradeState": "awaiting_payment",
                "domains": normalizedDomains,
                "totalProratedAmount": totalProrated,
                "perDomainProratedAmount": perDomainProrated,
                "currentQuantity": domainCount,
                "targetQuantity": totalAfterAdd,
                "daysRemaining": max((subscriptionExpiry - now).days, 1),
                "userEmail": identity["email"],
                "userName": identity["name"],
                "userContact": identity["contact"],
                "invoiceId": invoice["id"],
                "billingMode": billingMode,
                "expiresAt": subscriptionExpiry.isoformat(),
                "pricingSnapshot": {
                    "pricingVersion": snapshot.pricing_version,
                    "priceSource": snapshot.pricing_reference_snapshot_json.get("source"),
                },
                "taxSnapshot": {
                    "taxRuleVersion": snapshot.tax.tax_rule_version,
                    "amountBeforeTax": snapshot.amount_before_tax,
                    "taxAmount": snapshot.tax.tax_amount,
                    "totalAmount": snapshot.total_amount,
                    "currency": snapshot.currency,
                },
            }
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def verifyDomainUpgrade(self, payload: dict, token: str) -> None:
        """
        Verify Razorpay Order checkout signature and activate the added domains.

        Performs HMAC SHA256 verification using Razorpay API secret. The paid
        domains are derived only from server/provider state (Razorpay notes and
        pendingAdditions), never from the client callback payload.

        Args:
            payload (dict): Razorpay checkout response payload.
            token (str): Authorization token.
        """
        try:
            decodedToken = jwt.decode(
                token,
                os.environ["SECRET_KEY"],
                algorithms=["HS256"]
            )
            userId = decodedToken.get("userId")
            paymentId = payload.get("razorpayPaymentId")
            orderId = payload.get("razorpayOrderId")
            signature = payload.get("razorpaySignature")
            if not all([paymentId, orderId, signature, userId]):
                raise Exception("Missing Razorpay verification fields")
            message = f"{orderId}|{paymentId}"
            expectedSignature = hmac.new(
                os.environ["RAZORPAY_KEY_SECRET"].encode(),
                message.encode(),
                hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expectedSignature, signature):
                raise Exception("Invalid Razorpay signature")
            order = self.razorpayClient.order.fetch(orderId)
            orderNotesRaw = order.get("notes", {}) or {}
            orderNotes = orderNotesRaw if isinstance(orderNotesRaw, dict) else {}
            if orderNotes.get("type") != "domain_upgrade_proration":
                raise Exception(
                    f"Order {orderId} is not a domain upgrade order "
                    f"(type={orderNotes.get('type')})"
                )
            noteUserId = orderNotes.get("userId")
            if noteUserId and noteUserId != userId:
                raise Exception(
                    f"Order/user mismatch during domain upgrade: order.userId={noteUserId}, "
                    f"token.userId={userId}"
                )
            invoiceId = orderNotes.get("invoiceId")
            if orderNotes.get("billingMode") == "monthly_prepaid":
                if noteUserId != userId:
                    raise ValueError("ORDER_OWNERSHIP_MISMATCH")
                payment = self.razorpayClient.payment.fetch(paymentId)
                return self._finalizeManualCheckout(invoiceId, orderId, paymentId, payment)
            user = self.client.table("Users").select("userId").eq("userId", userId).execute().data
            if not user:
                raise Exception("User not found")
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            pendingAdditions = subscriptionPendingAdditions(subscription)
            if subscription.get('billing_mode') == 'monthly_prepaid':
                from api.services.billing.manualBillingRepository import getManualBillingRepository
                return getManualBillingRepository().cancelExpertAddition(userId,normalizedDomain)
            pendingDomains = [
                item.get("domain")
                for item in pendingAdditions
                if item.get("orderId") == orderId
                and item.get("state") not in ("activated", "failed", "expired")
                and item.get("domain")
            ]
            orderDomains = [
                d.strip()
                for d in (orderNotes.get("domains", "") or "").split(",")
                if d.strip()
            ]
            serverDomains = self._normalizeAndValidateDomains(orderDomains or pendingDomains)
            if not serverDomains:
                raise Exception("No server-side pending domains found for domain upgrade")
            if pendingDomains and sorted(serverDomains) != sorted(self._normalizeAndValidateDomains(pendingDomains)):
                raise Exception(
                    f"Domain upgrade mismatch: order domains={serverDomains}, "
                    f"pending domains={pendingDomains}"
                )

            invoice = None
            if invoiceId:
                invoiceRows = self.client.table("Invoices") \
                    .select(
                        "id, userId, status, total_amount, amount, currency, "
                        "razorpay_order_id, billing_reason"
                    ) \
                    .eq("id", invoiceId) \
                    .limit(1) \
                    .execute().data
                if not invoiceRows:
                    raise Exception(f"Frozen invoice not found for domain upgrade: {invoiceId}")
                invoice = invoiceRows[0]
                if invoice.get("userId") != userId:
                    raise Exception("Invoice ownership mismatch during domain upgrade verification")
                invoiceStatus = (invoice.get("status") or "").lower()
                if invoiceStatus in ("paid", "void"):
                    logger.info(
                        f"Domain upgrade invoice {invoiceId} already resolved ({invoiceStatus}), "
                        f"skipping verification"
                    )
                    return
                if invoiceStatus not in ("upcoming", "payment_pending"):
                    raise Exception(
                        f"Domain upgrade invoice {invoiceId} is not payable "
                        f"(status={invoiceStatus})"
                    )
                invoiceOrderId = invoice.get("razorpay_order_id")
                if invoiceOrderId and invoiceOrderId != orderId:
                    raise Exception(
                        f"Invoice/order mismatch: invoice.order_id={invoiceOrderId}, "
                        f"request.orderId={orderId}"
                    )

            payment = self.razorpayClient.payment.fetch(paymentId)
            if payment.get("status") != "captured":
                raise CustomException(ValueError("Payment is awaiting capture"),statusCode=409,uiMessage="Payment has not been captured yet.")
            if payment.get("order_id") != orderId:
                raise ValueError("PAYMENT_ORDER_MISMATCH")
            paymentOrderId = payment.get("order_id")
            if paymentOrderId and paymentOrderId != orderId:
                raise Exception(
                    f"Payment/order mismatch: payment.order_id={paymentOrderId}, "
                    f"request.orderId={orderId}"
                )
            if invoice:
                expectedAmount = invoice.get("total_amount") or invoice.get("amount")
                actualAmount = payment.get("amount")
                expectedCurrency = invoice.get("currency") or "INR"
                actualCurrency = payment.get("currency", "INR")
                if expectedAmount is not None and int(actualAmount or 0) != int(expectedAmount):
                    raise Exception(
                        f"Amount mismatch: expected={expectedAmount}, actual={actualAmount}"
                    )
                if str(actualCurrency).upper() != str(expectedCurrency).upper():
                    raise Exception(
                        f"Currency mismatch: expected={expectedCurrency}, actual={actualCurrency}"
                    )

            currentCount = subscriptionDomainCount(subscription)
            targetQuantity = int(orderNotes.get("targetQuantity") or (currentCount + len(serverDomains)))
            self._activatePaidDomains(
                userId=userId,
                domains=serverDomains,
                targetQuantity=targetQuantity,
                referenceId=orderId,
            )
            if invoiceId:
                self._markInvoicePaid(
                    invoiceId=invoiceId,
                    paymentId=paymentId,
                    paidAt=str(utcNow()),
                )
            self._auditLog(
                userId, "domain.upgrade_verified",
                paymentId=paymentId,
                status="VERIFIED",
                metadata={
                    "orderId": orderId,
                    "invoiceId": invoiceId,
                    "domains": serverDomains,
                }
            )
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

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

    def createAnnualRenewalPaymentSession(self, invoiceId: str, token: str) -> dict:
        """
        Create or reuse a Razorpay Order for an annual renewal invoice
        checkout from the dashboard.

        Validates invoice ownership, invoice lifecycle state, and subscription
        billing mode before creating the order. If an unexpired order already
        exists on the invoice, it is reused to prevent duplicate charges.

        Args:
            invoiceId (str): Internal invoice primary key.
            token (str): Authorization JWT token.

        Returns:
            dict: Checkout session payload for the frontend.
        """
        try:
            decodedToken = jwt.decode(
                token,
                os.environ["SECRET_KEY"],
                algorithms=["HS256"]
            )
            userId = decodedToken.get("userId")
            tokenEmail = decodedToken.get("email")

            invoice = self.client.table("Invoices") \
                .select(
                    "id, userId, subscription_id, billing_reason, status, "
                    "total_amount, amount, currency, period_start, period_end, "
                    "razorpay_order_id, "
                    "amount_before_tax, tax_amount, tax_breakdown_json, "
                    "pricing_version, pricing_reference_snapshot_json"
                ) \
                .eq("id", invoiceId) \
                .limit(1) \
                .execute().data
            if not invoice:
                raise Exception(f"Invoice {invoiceId} not found")
            invoice = invoice[0]

            if invoice["userId"] != userId:
                raise Exception("Invoice does not belong to the authenticated user")

            invoiceStatus = (invoice.get("status") or "").lower()
            if invoiceStatus not in ("upcoming", "payment_pending"):
                raise Exception(
                    f"Invoice {invoiceId} is not payable (status={invoiceStatus})"
                )

            if invoice.get("billing_reason") != "renewal":
                raise Exception(
                    f"Invoice {invoiceId} is not a renewal invoice"
                )

            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            if subscription.get("billing_mode") != "annual_prepaid":
                raise Exception("Annual renewal checkout requires an annual_prepaid subscription")
            assertInvoiceBelongsToSubscription(invoice, subscription)

            existingOrderId = invoice.get("razorpay_order_id")
            if existingOrderId:
                try:
                    existingOrder = self.razorpayClient.order.fetch(existingOrderId)
                    if self._isAnnualRenewalOrderReusable(existingOrder):
                        identity = self._resolveCheckoutIdentity(userId, tokenEmail)
                        self._auditLog(
                            userId, "annual_renewal.session_reused",
                            status="REUSED",
                            metadata={
                                "invoiceId": invoiceId,
                                "orderId": existingOrderId,
                            }
                        )
                        return {
                            "userId": userId,
                            "userEmail": identity["email"],
                            "userContact": identity["contact"],
                            "userName": identity["name"],
                            "razorpayKey": os.environ["RAZORPAY_KEY_ID"],
                            "orderId": existingOrderId,
                            "invoiceId": invoiceId,
                            "amount": invoice.get("total_amount") or invoice.get("amount"),
                            "currency": invoice.get("currency", "INR"),
                        }
                    logger.info(
                        f"Existing order {existingOrderId} is not safely reusable for "
                        f"invoice {invoiceId}; creating a fresh order"
                    )
                except Exception as fetchError:
                    logger.warning(
                        f"Failed to reuse existing order {existingOrderId} "
                        f"for invoice {invoiceId}: {fetchError}"
                    )

            identity = self._resolveCheckoutIdentity(userId, tokenEmail)

            totalAmount = invoice.get("total_amount") or invoice.get("amount")
            if not totalAmount or int(totalAmount) <= 0:
                raise Exception(f"Invoice {invoiceId} has invalid amount: {totalAmount}")

            order = self.razorpayClient.order.create({
                "amount": int(totalAmount),
                "currency": invoice.get("currency", "INR"),
                "notes": {
                    "userId": userId,
                    "type": "annual_renewal",
                    "invoiceId": invoiceId,
                    "subscriptionId": subscription["id"],
                    "billingReason": "renewal",
                },
            })

            self._attachOrderToInvoice(invoiceId=invoiceId, orderId=order["id"])

            self._auditLog(
                userId, "annual_renewal.session_created",
                status="CREATED",
                metadata={
                    "invoiceId": invoiceId,
                    "orderId": order["id"],
                    "amount": totalAmount,
                    "currency": invoice.get("currency", "INR"),
                }
            )

            return {
                "userId": userId,
                "userEmail": identity["email"],
                "userContact": identity["contact"],
                "userName": identity["name"],
                "razorpayKey": os.environ["RAZORPAY_KEY_ID"],
                "orderId": order["id"],
                "invoiceId": invoiceId,
                "amount": totalAmount,
                "currency": invoice.get("currency", "INR"),
                "pricingSnapshot": {
                    "pricingVersion": invoice.get("pricing_version"),
                    "amountBeforeTax": invoice.get("amount_before_tax"),
                    "taxAmount": invoice.get("tax_amount"),
                    "totalAmount": totalAmount,
                },
            }
        except PaymentValidationError as e:
            exception = self._paymentValidationException(e)
            logger.error(exception)
            raise exception
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def verifyAnnualRenewalPayment(self, payload: dict, token: str) -> dict:
        """
        Verify the Razorpay Order checkout signature for an annual renewal
        payment and finalize captured payments immediately.

        Validates:
            - HMAC SHA256 signature.
            - Order notes match the invoice.
            - Payment amount/currency match the frozen invoice snapshot.

        The invoice is NOT marked paid here. Final paid transition happens
        via the payment.captured webhook to guarantee Razorpay settlement.

        Args:
            payload (dict): Checkout callback payload with invoiceId,
                            razorpayOrderId, razorpayPaymentId, razorpaySignature.
            token (str): Authorization JWT token.
        """
        try:
            decodedToken = jwt.decode(
                token,
                os.environ["SECRET_KEY"],
                algorithms=["HS256"]
            )
            userId = decodedToken.get("userId")

            invoiceId = payload.get("invoiceId")
            orderId = payload.get("razorpayOrderId")
            paymentId = payload.get("razorpayPaymentId")
            signature = payload.get("razorpaySignature")

            if not all([invoiceId, orderId, paymentId, signature, userId]):
                raise Exception("Missing required verification fields")

            message = f"{orderId}|{paymentId}"
            expectedSignature = hmac.new(
                os.environ["RAZORPAY_KEY_SECRET"].encode(),
                message.encode(),
                hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expectedSignature, signature):
                raise Exception("Invalid Razorpay signature")

            invoice = self.client.table("Invoices") \
                .select(
                    "id, userId, subscription_id, total_amount, amount, currency, status, "
                    "razorpay_order_id, billing_reason, metadata_json"
                ) \
                .eq("id", invoiceId) \
                .limit(1) \
                .execute().data
            if not invoice:
                raise Exception(f"Invoice {invoiceId} not found during verification")
            invoice = invoice[0]

            if invoice["userId"] != userId:
                raise Exception("Invoice ownership mismatch during verification")

            invoiceStatus = (invoice.get("status") or "").lower()
            if invoiceStatus in ("paid", "void"):
                logger.info(
                    f"Invoice {invoiceId} already resolved ({invoiceStatus}), "
                    f"skipping verification"
                )
                return {
                    "verified": True,
                    "finalized": invoiceStatus == "paid",
                    "alreadyFinalized": True,
                    "invoiceStatus": invoiceStatus.upper(),
                    "awaitingWebhookFinalization": False,
                }
            if invoiceStatus not in ("upcoming", "payment_pending"):
                raise Exception(
                    f"Invoice {invoiceId} is not in payable state for verification "
                    f"(status={invoiceStatus})"
                )
            if invoice.get("billing_reason") != "renewal":
                raise Exception(f"Invoice {invoiceId} is not a renewal invoice")

            invoiceOrderId = invoice.get("razorpay_order_id")
            if invoiceOrderId and invoiceOrderId != orderId:
                raise Exception(
                    f"Order mismatch: invoice bound to {invoiceOrderId}, "
                    f"received {orderId}"
                )

            userRecord = self.client.table("Users") \
                .select("userId") \
                .eq("userId", userId) \
                .limit(1) \
                .execute().data
            if not userRecord:
                raise Exception("Authenticated user not found during verification")
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            assertInvoiceBelongsToSubscription(invoice, subscription)

            order = self.razorpayClient.order.fetch(orderId)
            orderNotesRaw = order.get("notes", {}) or {}
            orderNotes = orderNotesRaw if isinstance(orderNotesRaw, dict) else {}
            if orderNotes.get("type") != "annual_renewal":
                raise Exception(
                    f"Order {orderId} is not marked as annual renewal "
                    f"(type={orderNotes.get('type')})"
                )
            noteInvoiceId = orderNotes.get("invoiceId")
            if noteInvoiceId and noteInvoiceId != invoiceId:
                raise Exception(
                    f"Order/invoice mismatch: order.invoiceId={noteInvoiceId}, "
                    f"request.invoiceId={invoiceId}"
                )
            noteUserId = orderNotes.get("userId")
            if noteUserId and noteUserId != userId:
                raise Exception(
                    f"Order/user mismatch: order.userId={noteUserId}, request.userId={userId}"
                )

            payment = self.razorpayClient.payment.fetch(paymentId)
            paymentOrderId = payment.get("order_id")
            if paymentOrderId and paymentOrderId != orderId:
                raise Exception(
                    f"Payment/order mismatch: payment.order_id={paymentOrderId}, "
                    f"request.orderId={orderId}"
                )
            paymentNotesRaw = payment.get("notes", {}) or {}
            paymentNotes = paymentNotesRaw if isinstance(paymentNotesRaw, dict) else {}
            if paymentNotes.get("type") and paymentNotes.get("type") != "annual_renewal":
                raise Exception(
                    f"Payment note type mismatch: {paymentNotes.get('type')}"
                )
            if paymentNotes.get("invoiceId") and paymentNotes.get("invoiceId") != invoiceId:
                raise Exception(
                    f"Payment/invoice mismatch: payment.invoiceId={paymentNotes.get('invoiceId')}, "
                    f"request.invoiceId={invoiceId}"
                )

            expectedAmount = invoice.get("total_amount") or invoice.get("amount")
            actualAmount = payment.get("amount")
            expectedCurrency = invoice.get("currency") or "INR"
            actualCurrency = payment.get("currency", "INR")

            if expectedAmount is not None and int(actualAmount or 0) != int(expectedAmount):
                raise Exception(
                    f"Amount mismatch: expected={expectedAmount}, actual={actualAmount}"
                )
            if str(actualCurrency).upper() != str(expectedCurrency).upper():
                raise Exception(
                    f"Currency mismatch: expected={expectedCurrency}, actual={actualCurrency}"
                )

            if (payment.get("status") or "").lower() == "captured":
                result = self._finalizeCapturedAnnualRenewalPayment(
                    invoice=invoice,
                    subscription=subscription,
                    payment=payment,
                    userId=userId,
                )
                if result.get("finalized") and not result.get("alreadyFinalized"):
                    result["accessToken"] = self._reissueTokenWithUpdatedClaims(
                        token, "active", "annual"
                    )
                return result

            existingMetadata = invoice.get("metadata_json")
            metadata = dict(existingMetadata) if isinstance(existingMetadata, dict) else {}
            metadata.update({
                "flow": "annual_renewal_dashboard_verify",
                "verified": True,
                "verifiedAt": utcNow().isoformat(),
                "awaitingWebhookFinalization": True,
            })

            self.client.table("Invoices").update({
                "razorpay_order_id": orderId,
                "razorpayPaymentId": paymentId,
                "metadata_json": metadata,
            }).eq("id", invoiceId).execute()

            self._auditLog(
                userId, "annual_renewal.verified",
                paymentId=paymentId,
                status="VERIFIED_PENDING_WEBHOOK",
                metadata={
                    "invoiceId": invoiceId,
                    "orderId": orderId,
                    "amount": actualAmount,
                    "currency": actualCurrency,
                }
            )
            logger.info(
                f"Annual renewal verified for user {userId}, "
                f"invoice {invoiceId}, awaiting webhook finalization"
            )
            return {
                "verified": True,
                "finalized": False,
                "invoiceStatus": "PAYMENT_PENDING",
                "awaitingWebhookFinalization": True,
            }
        except PaymentValidationError as e:
            exception = self._paymentValidationException(e)
            logger.error(exception)
            raise exception
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

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

    # -- generic manual renewal routes ------------------------------------------

    def _createManualExpertCheckout(self, userId, domains, subscription, email):
        now=utcNow()
        end=parseUtc(subscription.get("current_period_end"))
        if end is None or end <= now or not self._isSubscriptionActive(subscription.get("status")):
            raise CustomException(ValueError("Paid access required"),statusCode=403,uiMessage="Your paid subscription period has ended.")
        identity=self._resolveCheckoutIdentity(userId,email)
        snapshot=computeInvoiceSnapshot(billingMode="monthly_prepaid",billingReason="proration",
            domainCount=len(domains),customerState=None,
            prorationAnchorStart=parseUtc(subscription["current_period_start"]),prorationAnchorEnd=end)
        deadline=min(end,now+datetime.timedelta(minutes=30))
        lifecycle=subscriptionBillingState(subscription)["manualBilling"]["lifecycleId"]
        invoice=self._createFrozenInvoiceFromSnapshot(userId=userId,subscriptionId=subscription["id"],
            billingReason="proration",paymentFlow="razorpay_order_checkout",requiresCustomerAuth=True,snapshot=snapshot,
            metadata={"domains":domains,"billingMode":"monthly_prepaid","manualBilling":{
                "schemaVersion":1,"lifecycleId":lifecycle,"purpose":"expert_addition","billingMode":"monthly_prepaid",
                "domains":domains,"revision":1,"expiresAt":deadline.isoformat()}})
        order=self._manualCheckoutOrder(invoice,subscription,domains,"expert_addition",deadline)
        return {"userId":userId,"userEmail":identity["email"],"userName":identity["name"],"userContact":identity["contact"],
            "razorpayKey":os.environ["RAZORPAY_KEY_ID"],"orderId":order["id"],"invoiceId":invoice["id"],
            "amount":snapshot.total_amount,"currency":snapshot.currency,"domains":domains,
            "expiresAt":deadline.isoformat(),"state":"payment_pending"}

    def _createManualInitialCheckout(self, userId, domains, identity):
        from api.services.billing.manualBillingRepository import getManualBillingRepository
        import uuid
        subscription = getManualBillingRepository().ensureCanonicalSubscription(userId)
        now = utcNow()
        rows = self.client.table("Invoices").select("*").eq("userId", userId).eq("billing_reason", "initial_purchase").in_("status", ["UPCOMING", "PAYMENT_PENDING"]).execute().data or []
        invoice = None
        for row in rows:
            frozen = (row.get("metadata_json") or {}).get("manualBilling") or {}
            if frozen.get("billingMode") != "monthly_prepaid":
                continue
            expires = parseUtc(frozen.get("expiresAt"))
            if expires and expires > now:
                if sorted(frozen.get("domains", [])) != sorted(domains):
                    raise CustomException(ValueError("A different checkout is already open"), statusCode=409,
                                          uiMessage="Finish or cancel the existing checkout first.")
                invoice = row
                break
            self.client.table("Invoices").update({"status": "EXPIRED"}).eq("id", row["id"]).execute()
        if invoice is None:
            snapshot = computeInvoiceSnapshot(billingMode="monthly_prepaid", billingReason="initial_purchase",
                                              domainCount=len(domains), customerState=None)
            deadline = now + datetime.timedelta(minutes=30)
            invoice = self._createFrozenInvoiceFromSnapshot(userId=userId, subscriptionId=subscription["id"],
                billingReason="initial_purchase", paymentFlow="razorpay_order_checkout", requiresCustomerAuth=True,
                snapshot=snapshot, metadata={"billingMode": "monthly_prepaid", "domains": domains,
                    "manualBilling": {"schemaVersion": 1, "lifecycleId": str(uuid.uuid4()),
                        "purpose": "initial_purchase", "billingMode": "monthly_prepaid", "domains": domains,
                        "coverageState": "estimated", "revision": 1, "expiresAt": deadline.isoformat()}})
        frozen = invoice["metadata_json"]["manualBilling"]
        deadline = parseUtc(frozen["expiresAt"])
        order = self._manualCheckoutOrder(invoice, subscription, domains, "initial_purchase", deadline)
        return {"userId": userId, "userEmail": identity["email"], "userName": identity["name"],
                "userContact": identity["contact"], "razorpayKey": os.environ["RAZORPAY_KEY_ID"],
                "orderId": order["id"], "status": order.get("status", "created"), "invoiceId": invoice["id"],
                "domains": domains, "quantity": len(domains), "billingMode": "monthly_prepaid",
                "expiresAt": deadline.isoformat(), "state": "payment_pending",
                "amount": invoice["total_amount"], "currency": invoice.get("currency", "INR"),
                "period": {"start": invoice.get("period_start"), "end": invoice.get("period_end"), "estimated": True}}

    def _manualCheckoutOrder(self, invoice: dict, subscription: dict,
                             domains: list, purpose: str, deadline) -> dict:
        from api.services.billing.manualBillingRepository import getManualBillingRepository, _payloadHash
        repository = getManualBillingRepository()
        metadata = invoice.get("metadata_json") or {}
        frozen = metadata.get("manualBilling") or {}
        invoiceId = str(invoice["id"])
        snapshot = {
            "invoiceId": invoiceId, "subscriptionId": subscription["id"],
            "lifecycleId": frozen["lifecycleId"], "cycleId": invoice.get("period_start"),
            "revision": frozen.get("revision", 1), "billingMode": "monthly_prepaid",
            "domains": domains, "amount": int(invoice.get("total_amount") or invoice.get("amount") or 0),
            "currency": invoice.get("currency") or "INR", "expiresAt": deadline.isoformat(),
            "periodStart": invoice.get("period_start"), "periodEnd": invoice.get("period_end"),
        }
        intent = repository.reserveCheckoutIntent(subscription["user_id"], purpose, invoiceId,
                                                  _payloadHash(snapshot), snapshot)
        if intent.razorpayOrderId:
            return {"id": intent.razorpayOrderId, "status": "created"}
        if not repository.claimProviderOrderCreation(intent.attemptId):
            raise CustomException(ValueError("Provider order creation requires reconciliation"),
                                  statusCode=409, uiMessage="Checkout creation is being reconciled. Please try again later.")
        order = self.razorpayClient.order.create({
            "amount": intent.amount, "currency": intent.currency,
            "receipt": intent.attemptId,
            "notes": {"userId": intent.userId, "invoiceId": invoiceId, "attemptId": intent.attemptId,
                      "billingMode": "monthly_prepaid", "domains": ", ".join(domains),
                      "type": {"renewal":"manual_renewal","expert_addition":"domain_upgrade_proration"}.get(purpose,"initial_subscription")},
        })
        repository.bindProviderOrder(intent.attemptId, order)
        return order

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
        result = repository.finalizeCapturedPayment(VerifiedPaymentEvidence(
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

    def createRenewalPaymentSession(self, invoiceId: str, token: str) -> dict:
        """
        Create a checkout session for an owned renewal invoice.

        Generic across billing modes; the monthly deadline is the current
        period end and late sessions are rejected. Annual delegates to the
        annual wrapper policy.
        """
        try:
            decodedToken = jwt.decode(
                token, os.environ["SECRET_KEY"], algorithms=["HS256"]
            )
            userId = decodedToken.get("userId")
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            billingMode = (subscription.get("billing_mode") or "").lower()
            if billingMode != "monthly_prepaid":
                return self.createAnnualRenewalPaymentSession(
                    invoiceId=invoiceId, token=token
                )
            invoiceRows = (
                self.client.table("Invoices")
                .select(
                    "id, userId, status, billing_reason, total_amount, amount, "
                    "currency, razorpay_order_id, period_start, period_end, metadata_json, subscription_id"
                )
                .eq("id", invoiceId)
                .limit(1)
                .execute()
                .data
            )
            if not invoiceRows:
                raise Exception(f"Invoice {invoiceId} not found")
            invoice = invoiceRows[0]
            if invoice.get("userId") != userId:
                raise Exception("Invoice does not belong to the authenticated user")
            status = (invoice.get("status") or "").upper()
            if status not in ("UPCOMING", "PAYMENT_PENDING"):
                raise Exception(f"Invoice {invoiceId} is not payable (status={status})")
            if (invoice.get("billing_reason") or "") != "renewal":
                raise Exception(f"Invoice {invoiceId} is not a renewal invoice")
            # Only the CURRENT cycle's renewal is payable through this route.
            if parseUtc(invoice.get("period_start")) != parseUtc(subscription.get("current_period_end")):
                raise CustomException(
                    ValueError("Invoice is not the current cycle's renewal"),
                    statusCode=409,
                    uiMessage="This invoice is no longer the active renewal.",
                )
            currentEnd = parseUtc(subscription.get("current_period_end"))
            now = utcNow()
            if currentEnd is not None and now >= currentEnd:
                raise CustomException(
                    ValueError("Renewal window closed at the current period end"),
                    statusCode=409,
                    uiMessage=(
                        "Your subscription period has ended. Purchase again to "
                        "start a new subscription."
                    ),
                )
            if subscription.get("renewal_opt_out"):
                raise CustomException(
                    ValueError("Renewal was declined for this subscription"),
                    statusCode=409,
                    uiMessage="You cancelled renewal. Resume renewal first.",
                )
            identity = self._resolveCheckoutIdentity(userId, decodedToken.get("email"))
            totalAmount = invoice.get("total_amount") or invoice.get("amount")
            frozen = (invoice.get("metadata_json") or {}).get("manualBilling") or {}
            order = self._manualCheckoutOrder(invoice, subscription, frozen.get("domains", []), "renewal", currentEnd)
            self._auditLog(
                userId, "renewal.session_created",
                status="CREATED",
                metadata={"invoiceId": invoiceId, "orderId": order["id"], "amount": totalAmount},
            )
            return {
                "userId": userId,
                "userEmail": identity["email"],
                "userContact": identity["contact"],
                "userName": identity["name"],
                "razorpayKey": os.environ["RAZORPAY_KEY_ID"],
                "orderId": order["id"],
                "invoiceId": invoiceId,
                "amount": totalAmount,
                "currency": invoice.get("currency", "INR"),
                "expiresAt": (currentEnd.isoformat() if currentEnd else None),
                "state": "payment_pending",
                "period": {
                    "start": invoice.get("period_start"),
                    "end": invoice.get("period_end"),
                    "estimated": False,
                },
            }
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def verifyRenewalPayment(self, payload: dict, token: str) -> dict:
        """
        Verify and finalize a captured renewal checkout.

        Monthly: a captured payment freezes the future period without
        touching current dates/quota. Annual: delegates to the annual
        wrapper preserving its lifecycle/credit baseline.
        """
        try:
            decodedToken = jwt.decode(
                token, os.environ["SECRET_KEY"], algorithms=["HS256"]
            )
            userId = decodedToken.get("userId")
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            billingMode = (subscription.get("billing_mode") or "").lower()
            if billingMode != "monthly_prepaid":
                return self.verifyAnnualRenewalPayment(payload=payload, token=token)

            invoiceId = payload.get("invoiceId")
            orderId = payload.get("razorpayOrderId")
            paymentId = payload.get("razorpayPaymentId")
            signature = payload.get("razorpaySignature")
            if not all([invoiceId, orderId, paymentId, signature, userId]):
                raise Exception("Missing required verification fields")
            message = f"{orderId}|{paymentId}"
            expectedSignature = hmac.new(
                os.environ["RAZORPAY_KEY_SECRET"].encode(),
                message.encode(),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expectedSignature, signature):
                raise Exception("Invalid Razorpay signature")

            invoiceRows = (
                self.client.table("Invoices")
                .select(
                    "id, userId, status, billing_reason, subscription_id, total_amount, amount, currency, "
                    "razorpay_order_id, period_start, period_end, metadata_json"
                )
                .eq("id", invoiceId)
                .limit(1)
                .execute()
                .data
            )
            if not invoiceRows:
                raise Exception(f"Invoice {invoiceId} not found")
            invoice = invoiceRows[0]
            if invoice.get("userId") != userId:
                raise Exception("Invoice ownership mismatch")
            if invoice.get("billing_reason") != "renewal":
                raise Exception("Invoice is not a renewal invoice")
            order = self.razorpayClient.order.fetch(orderId)
            orderNotes = order.get("notes") or {}
            if orderNotes.get("type") != "manual_renewal":
                raise Exception(
                    f"Order {orderId} is not a manual renewal order"
                )
            if orderNotes.get("userId") != userId:
                raise Exception("Order/user mismatch")
            invoiceOrderId = invoice.get("razorpay_order_id")
            if invoiceOrderId and invoiceOrderId != orderId:
                raise Exception("Invoice/order mismatch")

            payment = self.razorpayClient.payment.fetch(paymentId)
            return self._finalizeManualCheckout(invoiceId, orderId, paymentId, payment)
        except CustomException:
            raise
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def _finalizeCapturedManualRenewal(
        self, invoiceId, orderId, paymentId, paymentEntity, subscription=None, now=None,
    ) -> dict:
        """Browser and webhook share one atomic capture/grant transaction."""
        return self._finalizeManualCheckout(invoiceId, orderId, paymentId, paymentEntity, now)

    def _finalizeCapturedInitialPurchase(
        self,
        invoiceId: str,
        orderId: str | None,
        paymentId: str,
        paymentEntity: dict,
        userId: str,
    ) -> dict:
        """Webhook backup: activate a captured initial purchase once.

        Recovers the paid period when the browser closes before
        verifySubscription completes. Idempotent: an already-active paid
        window for this invoice is the replay guard.
        """
        invoiceRows = (
            self.client.table("Invoices")
            .select(
                "id, userId, status, billing_reason, metadata_json, "
                "period_start, period_end, total_amount, amount, razorpay_order_id"
            )
            .eq("id", invoiceId)
            .limit(1)
            .execute()
            .data
        )
        if not invoiceRows:
            raise Exception(f"Invoice {invoiceId} not found")
        invoice = invoiceRows[0]
        if invoice.get("userId") != userId:
            raise Exception("Invoice ownership mismatch")
        billing = (invoice.get("metadata_json") or {}).get("manualBilling") or {}
        if billing.get("billingMode") == "monthly_prepaid":
            return self._finalizeManualCheckout(invoiceId, orderId, paymentId, paymentEntity)
        status = (invoice.get("status") or "").upper()
        if status == "PAID":
            return {"state": "already_finalized"}
        if status not in ("UPCOMING", "PAYMENT_PENDING"):
            raise Exception(f"Invoice {invoiceId} is not payable (status={status})")
        if (invoice.get("billing_reason") or "") != "initial_purchase":
            raise Exception(f"Invoice {invoiceId} is not an initial purchase")
        if orderId:
            invoiceOrderId = invoice.get("razorpay_order_id")
            if invoiceOrderId and invoiceOrderId != orderId:
                raise Exception("Invoice/order mismatch")

        existingMetadata = invoice.get("metadata_json")
        metadata = dict(existingMetadata) if isinstance(existingMetadata, dict) else {}
        manualBilling = dict(metadata.get("manualBilling") or {})
        domains = manualBilling.get("domains") or metadata.get("domains") or []
        billingMode = manualBilling.get("billingMode") or metadata.get("billingMode") or "monthly_prepaid"
        if not domains:
            raise Exception("Invoice metadata is missing the purchased domains")

        # Already-active guard: the canonical row's paid window must belong
        # to this invoice/lifecycle, not a newer purchase.
        subscription = self._getCanonicalSubscription(userId=userId, required=True)
        currentEnd = parseUtc(subscription.get("current_period_end"))
        now = utcNow()
        if currentEnd is not None and currentEnd > now:
            invoicePaidAt = invoice.get("paidAt")
            if not invoicePaidAt:
                # A live paid window exists that was not created by this
                # invoice: do not overwrite a newer purchase.
                raise Exception(
                    "An active paid period already exists for this user; "
                    "the captured payment requires reconciliation"
                )

        # Reuse the verified activation path: same dates/pricing math as
        # verifySubscription with the payment entity as provider evidence.
        if billingMode == "monthly_prepaid":
            activationAt = now
            expiry = activationAt + relativedelta(months=1)
            planType = "pro"
        else:
            activationAt = now
            expiry = activationAt + relativedelta(years=1)
            planType = "annual"
        lifecycleId = self._ensureLifecycleId(subscription)
        self._upsertCanonicalSubscription(
            userId=userId,
            billingMode=billingMode,
            status="active",
            currentPeriodStart=activationAt.isoformat(),
            currentPeriodEnd=expiry.isoformat(),
            renewalDueAt=expiry.isoformat(),
            autoRenewEnabled=False,
            paymentCollectionMode="authenticated_checkout",
            subscribedExperts=domains,
            domainCount=len(domains),
            pendingRemovals=[],
            pendingAdditions=[],
            planType=planType,
        )
        canonical = self._getCanonicalSubscription(userId=userId, required=True)
        existingState = dict(subscriptionBillingState(canonical) or {})
        manualBillingState = dict(existingState.get("manualBilling") or {})
        manualBillingState.update({
            "schemaVersion": 1,
            "lifecycleId": lifecycleId,
            "activationAt": activationAt.isoformat(),
            "finalPaidEnd": expiry.isoformat(),
        })
        existingState["manualBilling"] = manualBillingState
        self.client.table("subscriptions").update({
            "billing_state": existingState,
            "auto_renew_enabled": False,
        }).eq("id", canonical["id"]).execute()

        manualBilling.update({
            "coverageState": "active",
            "activatedAt": activationAt.isoformat(),
        })
        metadata["manualBilling"] = manualBilling
        self.client.table("Invoices").update({
            "status": "PAID",
            "razorpayPaymentId": paymentId,
            "paidAt": now.isoformat(),
            "period_start": activationAt.isoformat(),
            "period_end": expiry.isoformat(),
            "metadata_json": metadata,
        }).eq("id", invoiceId).execute()

        try:
            from api.services.credits.creditService import creditService
            creditService.initializeCreditBalance(
                userId=userId,
                planTier=planType,
                domainCount=len(domains),
            )
        except Exception as creditErr:
            logger.warning(
                f"Credit initialization failed for webhook-recovered paid "
                f"user {userId}: {creditErr}"
            )
        self._auditLog(
            userId, "subscription.verified",
            paymentId=paymentId,
            status="ACTIVE",
            metadata={
                "orderId": orderId,
                "invoiceId": invoiceId,
                "billingMode": billingMode,
                "lifecycleId": lifecycleId,
                "domains": domains,
                "flow": "webhook_recovery",
            },
        )
        return {"state": "activated", "finalized": True}


subscriptionService = SubscriptionService()
