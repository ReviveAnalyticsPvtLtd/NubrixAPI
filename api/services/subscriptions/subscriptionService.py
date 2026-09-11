"""
subscriptionService.py

This module provides the SubscriptionService class, which encapsulates
all business logic related to user subscriptions, including free trials,
Razorpay token-based recurring payment management, customer management,
and payment audit logging.
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
    subscriptionAnchorDay,
    subscriptionBillingState,
    subscriptionCustomerId,
    subscriptionDomainCount,
    subscriptionExperts,
    subscriptionPendingAdditions,
    subscriptionPendingRemovals,
    subscriptionTokenId,
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

    Handles free trials, Razorpay token-based recurring payments,
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
            .order("updated_at", desc=True) \
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
        razorpayCustomerId=None,
        razorpayTokenId=None,
        subscriptionAnchorDay=None,
        recurringFailures=None,
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
            razorpayCustomerId=razorpayCustomerId,
            razorpayTokenId=razorpayTokenId,
            subscriptionAnchorDay=subscriptionAnchorDay,
            recurringFailures=recurringFailures,
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
        """
        normalized = (billingMode or "monthly_recurring").strip().lower()
        if normalized not in {"monthly_recurring", "annual_prepaid"}:
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
                "status": "expired",
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

    def _getOrCreateRazorpayCustomer(self, userId: str, email: str, name: str, contact: str = "") -> str:
        """
        Retrieve existing or create a new Razorpay Customer for the user.

        Args:
            userId (str): The internal user ID.
            email (str): The user's email address.
            name (str): The user's full name.
            contact (str): The user's phone number.

        Returns:
            str: The Razorpay Customer ID.
        """
        subscription = self._getCanonicalSubscription(userId=userId, required=True)
        existingCustomerId = subscriptionCustomerId(subscription)
        if existingCustomerId:
            return existingCustomerId
        customer = self.razorpayClient.customer.create({
            "name": name,
            "email": email,
            "contact": contact,
            "fail_existing": 0
        })
        customerId = customer["id"]
        self.client.table("subscriptions").update({
            "razorpay_customer_id": customerId
        }).eq("id", subscription["id"]).execute()
        logger.info(f"Razorpay customer created for user {userId}: {customerId}")
        return customerId

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

    def _syncRazorpayCustomerIdentity(self, customerId: str, canonicalIdentity: dict) -> bool:
        """
        Sync the Razorpay customer object with canonical identity if drift is detected.

        Fetches the current Razorpay customer and compares email, name, and
        contact against canonical values. Updates only the fields that have
        drifted. Failures are logged but do not propagate — checkout can
        proceed even if the sync fails.

        Args:
            customerId (str): The Razorpay customer ID.
            canonicalIdentity (dict): Canonical identity with 'email', 'name', 'contact'.

        Returns:
            bool: True if a sync update was performed, False otherwise.
        """
        try:
            customer = self.razorpayClient.customer.fetch(customerId)
        except Exception as e:
            logger.warning(f"Failed to fetch Razorpay customer {customerId} for identity sync: {e}")
            return False
        updates = {}
        if canonicalIdentity["email"] and customer.get("email") != canonicalIdentity["email"]:
            updates["email"] = canonicalIdentity["email"]
        if canonicalIdentity["name"] and customer.get("name") != canonicalIdentity["name"]:
            updates["name"] = canonicalIdentity["name"]
        if canonicalIdentity["contact"] and customer.get("contact") != canonicalIdentity["contact"]:
            updates["contact"] = canonicalIdentity["contact"]
        if not updates:
            return False
        try:
            self.razorpayClient.customer.edit(customerId, updates)
            logger.info(
                f"Razorpay customer {customerId} identity synced: "
                f"fields updated = {list(updates.keys())}"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to sync Razorpay customer {customerId}: {e}")
            return False

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
                recurringFailures=0,
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

    def createSubscription(self, domains: list[str], contact: str, token: str, billingMode: str = "monthly_recurring") -> dict:
        """
        Create a Razorpay Order for tokenization-based subscription checkout.

        The Order captures the user's card details and creates a saved mandate
        (token) for future recurring charges by the billing engine.

        Args:
            domains (list[str]): Domain expert names the user is subscribing to.
            contact (str): User's phone number for Razorpay checkout and RBI compliance.
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
            customerId = self._getOrCreateRazorpayCustomer(
                userId, identity["email"], identity["name"], identity["contact"]
            )
            synced = self._syncRazorpayCustomerIdentity(customerId, identity)
            self._auditLog(
                userId, "identity.checkout_resolved",
                status="SYNCED" if synced else "CURRENT",
                metadata={
                    "flow": "createSubscription",
                    "customerId": customerId,
                    "synced": synced,
                }
            )
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
            customerState = None
            snapshot = computeInvoiceSnapshot(
                billingMode=normalizedBillingMode,
                billingReason="initial_purchase",
                domainCount=quantity,
                customerState=customerState,
            )
            invoice = self._createFrozenInvoiceFromSnapshot(
                userId=userId,
                subscriptionId=subscription.get("id") if subscription else None,
                billingReason="initial_purchase",
                paymentFlow="razorpay_order_checkout",
                requiresCustomerAuth=(normalizedBillingMode == "annual_prepaid"),
                snapshot=snapshot,
                metadata={
                    "domains": normalizedDomains,
                    "flow": "createSubscription",
                    "billingMode": normalizedBillingMode,
                },
            )
            farFutureEpoch = int((utcNow() + datetime.timedelta(days=365 * 10)).timestamp())
            orderPayload = {
                "amount": snapshot.total_amount,
                "currency": "INR",
                "customer_id": customerId,
                "notes": {
                    "userId": userId,
                    "type": "initial_subscription",
                    "domains": ", ".join(normalizedDomains),
                    "invoiceId": invoice["id"],
                    "billingMode": normalizedBillingMode,
                },
            }
            if normalizedBillingMode == "monthly_recurring":
                orderPayload["method"] = "card"
                orderPayload["token"] = {
                    "max_amount": 1500000,
                    "expire_at": farFutureEpoch,
                    "frequency": "monthly",
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
                "customerId": customerId,
                "status": order["status"],
                "quantity": quantity,
                "domains": normalizedDomains,
                "invoiceId": invoice["id"],
                "billingMode": normalizedBillingMode,
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
            invoice = loadPayableInvoice(
                self.client,
                invoiceId=invoiceId,
                userId=userId,
                expectedBillingReason="initial_purchase",
            )
            payment = self.razorpayClient.payment.fetch(paymentId)
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            customerId = subscriptionCustomerId(subscription)
            if not customerId:
                raise Exception("No Razorpay customer found — cannot validate token")
            assertInvoiceBelongsToSubscription(invoice, subscription)
            validateOrderPaymentAgainstInvoice(
                order=order,
                payment=payment,
                invoice=invoice,
                expectedType="initial_subscription",
                expectedUserId=userId,
                expectedCustomerId=customerId,
                requestOrderId=orderId,
                requireCaptured=True,
            )
            tokenId = None
            recurringStatus = ""
            if billingMode == "monthly_recurring":
                tokenId = payment.get("token_id")
                if not tokenId:
                    raise Exception("Payment did not produce a recurring token (token_id is missing)")
                tokenEntity = self.razorpayClient.token.fetch(customerId, tokenId)
                recurringDetails = tokenEntity.get("recurring_details") or {}
                recurringStatus = recurringDetails.get("status", "")
                isRecurring = tokenEntity.get("recurring", False)
                UNUSABLE_STATES = {"cancelled", "expired", "rejected"}
                if recurringStatus in UNUSABLE_STATES:
                    raise Exception(
                        f"Token {tokenId} is not usable for recurring payments "
                        f"(recurring_details.status={recurringStatus})"
                    )
                if not isRecurring:
                    raise Exception(
                        f"Token {tokenId} is not recurring-enabled (recurring={isRecurring})"
                    )
                logger.info(
                    f"Token {tokenId} validated for user {userId}: "
                    f"recurring={isRecurring}, recurring_details.status={recurringStatus}"
                )
            currentTime = utcNow()
            if billingMode == "monthly_recurring":
                anchorDay = currentTime.day
                expiry = currentTime + relativedelta(months=1)
                self._upsertCanonicalSubscription(
                    userId=userId,
                    billingMode="monthly_recurring",
                    status="active",
                    currentPeriodStart=currentTime.isoformat(),
                    currentPeriodEnd=expiry.isoformat(),
                    renewalDueAt=expiry.isoformat(),
                    autoRenewEnabled=True,
                    paymentCollectionMode="silent_token",
                    subscribedExperts=normalizedDomains,
                    domainCount=len(normalizedDomains),
                    razorpayCustomerId=customerId,
                    razorpayTokenId=tokenId,
                    subscriptionAnchorDay=anchorDay,
                    recurringFailures=0,
                    pendingRemovals=[],
                    pendingAdditions=[],
                    planType="pro",
                )
            else:
                expiry = currentTime + relativedelta(years=1)
                self._upsertCanonicalSubscription(
                    userId=userId,
                    billingMode="annual_prepaid",
                    status="active",
                    currentPeriodStart=currentTime.isoformat(),
                    currentPeriodEnd=expiry.isoformat(),
                    renewalDueAt=expiry.isoformat(),
                    autoRenewEnabled=False,
                    paymentCollectionMode="authenticated_checkout",
                    subscribedExperts=normalizedDomains,
                    domainCount=len(normalizedDomains),
                    razorpayCustomerId=customerId,
                    recurringFailures=0,
                    pendingRemovals=[],
                    pendingAdditions=[],
                    planType="annual",
                )
                annualSubscription = self._getCanonicalSubscription(userId=userId, required=True)
                self.client.table("subscriptions").update({
                    "razorpay_token_id": None,
                    "subscription_anchor_day": None,
                }).eq("id", annualSubscription["id"]).execute()
            if invoiceId:
                paidAt = payment.get("captured_at") or payment.get("created_at")
                self._markInvoicePaid(
                    invoiceId=invoiceId,
                    paymentId=paymentId,
                    paidAt=str(utcFromTimestamp(paidAt)) if paidAt else None,
                )
            self._auditLog(
                userId, "subscription.verified",
                paymentId=paymentId,
                status="ACTIVE",
                metadata={
                    "orderId": orderId,
                    "invoiceId": invoiceId,
                    "billingMode": billingMode,
                    "tokenId": tokenId,
                    "tokenRecurringStatus": recurringStatus,
                    "domains": normalizedDomains,
                    "quantity": len(normalizedDomains),
                    "anchorDay": currentTime.day,
                }
            )
            planType = "pro" if billingMode == "monthly_recurring" else "annual"
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
            billingMode = self._normalizeBillingMode(subscription.get("billing_mode") or "monthly_recurring")
            identity = self._resolveCheckoutIdentity(userId, tokenEmail)
            customerId = subscriptionCustomerId(subscription)
            if billingMode == "monthly_recurring" and not subscriptionTokenId(subscription):
                raise Exception("No active recurring token found for monthly add-domain billing")
            if not customerId:
                customerId = self._getOrCreateRazorpayCustomer(
                    userId, identity["email"], identity["name"], identity["contact"]
                )
            synced = self._syncRazorpayCustomerIdentity(customerId, identity)
            self._auditLog(
                userId, "identity.checkout_resolved",
                status="SYNCED" if synced else "CURRENT",
                metadata={
                    "flow": "addDomains",
                    "customerId": customerId,
                    "synced": synced,
                }
            )
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
                "customer_id": customerId,
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
                "customerId": customerId,
                "invoiceId": invoice["id"],
                "billingMode": billingMode,
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
            user = self.client.table("Users").select("userId").eq("userId", userId).execute().data
            if not user:
                raise Exception("User not found")
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            pendingAdditions = subscriptionPendingAdditions(subscription)
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

            expectedCustomerId = subscriptionCustomerId(subscription)
            orderCustomerId = order.get("customer_id")
            if expectedCustomerId and orderCustomerId and orderCustomerId != expectedCustomerId:
                raise Exception(
                    f"Order/customer mismatch: order.customer_id={orderCustomerId}, "
                        f"subscription.customer_id={expectedCustomerId}"
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
            paymentOrderId = payment.get("order_id")
            if paymentOrderId and paymentOrderId != orderId:
                raise Exception(
                    f"Payment/order mismatch: payment.order_id={paymentOrderId}, "
                    f"request.orderId={orderId}"
                )
            paymentCustomerId = payment.get("customer_id")
            if expectedCustomerId and paymentCustomerId and paymentCustomerId != expectedCustomerId:
                raise Exception(
                    f"Payment/customer mismatch: payment.customer_id={paymentCustomerId}, "
                        f"subscription.customer_id={expectedCustomerId}"
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
                paidAt = payment.get("captured_at") or payment.get("created_at")
                self._markInvoicePaid(
                    invoiceId=invoiceId,
                    paymentId=paymentId,
                    paidAt=str(utcFromTimestamp(paidAt)) if paidAt else None,
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
        Cancel a pending domain addition request.

        Removes the pending entry from the DB. Razorpay Orders cannot be
        programmatically cancelled; they expire naturally.

        Args:
            domain (str): The domain to cancel.
            token (str): Authorization token.

        Returns:
            dict: Confirmation of cancellation.
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
            targetIndex = None
            for i, item in enumerate(pendingAdditions):
                if item["domain"] == normalizedDomain and item.get("state") not in ("activated",):
                    targetItem = item
                    targetIndex = i
                    break
            if targetItem is None:
                raise Exception(f"No cancellable pending addition found for domain '{normalizedDomain}'")
            pendingAdditions.pop(targetIndex)
            self.client.table("subscriptions").update({
                "pending_additions": pendingAdditions
            }).eq("id", subscription["id"]).execute()
            self._auditLog(
                userId, "domain.add_cancelled",
                status="CANCELLED",
                metadata={
                    "domain": normalizedDomain,
                    "currentDomainCount": subscriptionDomainCount(subscription),
                    "effectiveAt": "immediate",
                }
            )
            logger.info(f"Pending addition cancelled for domain '{normalizedDomain}', user {userId}")
            return {"domain": normalizedDomain, "cancelled": True}
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def cancelSubscription(self, reason: str, token: str) -> dict:
        """
        Schedule cancellation at the end of the current billing cycle.

        Marks canonical subscription as cancelled and disables auto-renew.
        The current period remains usable until its end, after which billing
        lifecycle transitions to expired by scheduler logic.

        Args:
            reason (str): User-selected reason for cancellation.
            token (str): Authorization token.

        Returns:
            dict: Cancellation confirmation with effective timing.
        """
        try:
            if not reason or not isinstance(reason, str) or not reason.strip():
                raise Exception("Cancellation reason is required")
            reason = reason.strip()
            decodedToken = jwt.decode(
                token,
                os.environ["SECRET_KEY"],
                algorithms=["HS256"]
            )
            userId = decodedToken.get("userId")
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            currentStatus = (subscription.get("status") or "").lower()
            if not self._isSubscriptionActive(currentStatus):
                raise Exception("No active subscription found for this user")
            billingMode = (subscription.get("billing_mode") or "none").lower()
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
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def initiateRefund(self, token: str, paymentId: str, amount: int | None = None) -> dict:
        """
        Initiate a refund for a Razorpay payment.

        Args:
            token (str): Authorization token.
            paymentId (str): The Razorpay payment ID to refund.
            amount (int | None): Amount in paise to refund. None for full refund.

        Returns:
            dict: The Razorpay refund response.
        """
        try:
            if not paymentId:
                raise Exception("paymentId is required")
            if amount is not None and amount <= 0:
                raise Exception("amount must be greater than 0")
            decodedToken = jwt.decode(
                token,
                os.environ["SECRET_KEY"],
                algorithms = ["HS256"]
            )
            userId = decodedToken.get("userId")
            if not userId:
                raise Exception("Invalid token payload")
            userRecord = self.client.table("Users").select("userId").eq("userId", userId).execute()
            if not userRecord.data:
                raise Exception("User not found")
            subscription = self._getCanonicalSubscription(userId=userId, required=True)
            customerId = subscriptionCustomerId(subscription)
            if not customerId:
                raise Exception("No Razorpay customer found for this user")
            payment = self.razorpayClient.payment.fetch(paymentId)
            paymentCustomerId = payment.get("customer_id")
            if not paymentCustomerId or paymentCustomerId != customerId:
                logger.warning(f"Unauthorized refund attempt blocked for user {userId}, payment {paymentId}")
                raise Exception("Payment does not belong to this user")
            refundParams = {}
            if amount is not None:
                refundParams["amount"] = amount
            result = self.razorpayClient.payment.refund(paymentId, refundParams)
            self._auditLog(
                userId, "refund.initiated",
                paymentId=paymentId,
                amount=amount or payment.get("amount"),
                currency=payment.get("currency", "INR"),
                status="INITIATED"
            )
            logger.info(f"Refund initiated for payment {paymentId}, user {userId}")
            return result
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
            customerId = self._getOrCreateRazorpayCustomer(
                userId, identity["email"], identity["name"], identity["contact"]
            )

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
                "customerId": customerId,
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
            expectedCustomerId = subscriptionCustomerId(subscription)

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
            orderCustomerId = order.get("customer_id")
            if expectedCustomerId and orderCustomerId and orderCustomerId != expectedCustomerId:
                raise Exception(
                    f"Order/customer mismatch: order.customer_id={orderCustomerId}, "
                        f"subscription.customer_id={expectedCustomerId}"
                )

            payment = self.razorpayClient.payment.fetch(paymentId)
            paymentOrderId = payment.get("order_id")
            if paymentOrderId and paymentOrderId != orderId:
                raise Exception(
                    f"Payment/order mismatch: payment.order_id={paymentOrderId}, "
                    f"request.orderId={orderId}"
                )
            paymentCustomerId = payment.get("customer_id")
            if expectedCustomerId and paymentCustomerId and paymentCustomerId != expectedCustomerId:
                raise Exception(
                    f"Payment/customer mismatch: payment.customer_id={paymentCustomerId}, "
                        f"subscription.customer_id={expectedCustomerId}"
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


subscriptionService = SubscriptionService()
