"""
topupService.py

Credit top-up purchases: pack listing, Razorpay order creation, and payment
verification.

Mirrors the mid-cycle domain-addition flow — a frozen invoice, a Razorpay
Order, a signature-verified callback, and a webhook backup path. The thin
seams over subscriptionService keep that reuse in one place and make the
purchase flow testable without a live Razorpay or Supabase.

Purchased tokens never expire and are spent only once the monthly quota is
exhausted; the bucket mechanics live in creditService.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = ["TopupService", "topupService"]


from api.services.credits.creditConfig import (
    TOKEN_TO_CREDIT_RATIO,
    getTopupPack,
    getTopupPacks,
)
from api.services.billing.billingEngine import computeTopupSnapshot
from api.services.credits import creditMath
from utils.exceptionHandler import CustomException
from utils.logger import logger
from jose import jwt
import hashlib
import hmac
import os


_ELIGIBLE_PLAN_TYPES = {"pro", "annual"}


class TopupService:
    """Purchase flow for credit top-up packs."""

    def __init__(self):
        self._client = None
        self._razorpayClient = None

    # ---- lazily bound collaborators -----------------------------------------

    @property
    def client(self):
        """Lazily acquire the shared Supabase client (avoids import-time coupling)."""
        if self._client is None:
            from api.commons import client
            self._client = client
        return self._client

    @client.setter
    def client(self, value):
        self._client = value

    @property
    def razorpayClient(self):
        """Reuse the subscription service's authenticated Razorpay client."""
        if self._razorpayClient is None:
            from api.services.subscriptions.subscriptionService import subscriptionService
            self._razorpayClient = subscriptionService.razorpayClient
        return self._razorpayClient

    @razorpayClient.setter
    def razorpayClient(self, value):
        self._razorpayClient = value

    # ---- thin seams over subscriptionService ---------------------------------

    @staticmethod
    def _decodeToken(token: str) -> tuple[str, str]:
        """Return (userId, email) from the authorization token."""
        decoded = jwt.decode(token, os.environ["SECRET_KEY"], algorithms=["HS256"])
        return decoded.get("userId"), decoded.get("email")

    @staticmethod
    def _subscription(userId: str) -> dict | None:
        """Fetch the canonical subscription row, or None when absent."""
        from api.services.subscriptions.subscriptionService import subscriptionService
        return subscriptionService._getCanonicalSubscription(userId=userId, required=False)

    @staticmethod
    def _identity(userId: str, tokenEmail: str) -> dict:
        """Checkout identity without any Razorpay Customer dependency.

        Contact details are prefill only — the same contact across separate
        accounts is never a billing identity. No Customer create/fetch/edit.
        """
        from api.services.subscriptions.subscriptionService import subscriptionService

        identity = subscriptionService._resolveCheckoutIdentity(userId, tokenEmail)
        return dict(identity)

    @staticmethod
    def _createInvoice(userId: str, subscriptionId: str | None, snapshot,
                       packId: str, tokens: int) -> dict:
        """Create the frozen add_on invoice that backs this purchase."""
        from api.services.subscriptions.subscriptionService import subscriptionService

        return subscriptionService._createFrozenInvoiceFromSnapshot(
            userId=userId,
            subscriptionId=subscriptionId,
            billingReason="add_on",
            paymentFlow="razorpay_order_checkout",
            requiresCustomerAuth=False,
            snapshot=snapshot,
            metadata={"flow": "creditTopup", "packId": packId, "tokens": tokens},
        )

    @staticmethod
    def _attachOrder(invoiceId: str, orderId: str) -> None:
        """Attach the created Razorpay order to the frozen invoice."""
        from api.services.subscriptions.subscriptionService import subscriptionService
        subscriptionService._attachOrderToInvoice(invoiceId=invoiceId, orderId=orderId)

    @staticmethod
    def _audit(userId: str, eventType: str, **kwargs) -> None:
        """Insert an audit row into the unified billing ledger."""
        from api.services.subscriptions.subscriptionService import subscriptionService
        subscriptionService._auditLog(userId, eventType, **kwargs)

    # ---- eligibility ---------------------------------------------------------

    @staticmethod
    def _isTopupEligible(subscription: dict | None) -> bool:
        """
        Determine whether a subscription may purchase top-ups.

        Requires a timestamp-valid paid Pro or Annual plan — including a
        cancelled monthly subscription whose already-paid coverage remains.
        Top-ups are an overflow valve for paying customers, not a substitute
        for one: free, trial, and expired users are pushed to upgrade, and
        stored top-ups never grant paid access.

        Args:
            subscription (dict | None): Canonical subscription row.

        Returns:
            bool: True when top-ups may be purchased.
        """
        if not subscription:
            return False
        from api.services.subscriptions.subscriptionService import subscriptionService
        from api.services.subscriptions.paymentValidationService import isAccessActive

        return (
            isAccessActive(subscription)
            and (subscription.get("plan_type") or "") in _ELIGIBLE_PLAN_TYPES
        )

    # ---- public API ----------------------------------------------------------

    def listPacks(self, token: str) -> dict:
        """
        Return the purchasable packs with tax-inclusive totals.

        Never raises on ineligibility — the client still needs the prices in
        order to show what a paid plan would unlock.

        Args:
            token (str): Authorization token.

        Returns:
            dict: {"packs": [...], "eligible": bool}.
        """
        try:
            userId, _ = self._decodeToken(token)
            subscription = self._subscription(userId)
            eligible = self._isTopupEligible(subscription)
            billingMode = (subscription or {}).get("billing_mode") or "monthly_prepaid"

            packs = []
            for packId in getTopupPacks():
                snapshot = computeTopupSnapshot(packId, billingMode)
                tokens = snapshot.pricing_reference_snapshot_json["tokens"]
                packs.append({
                    "packId": packId,
                    "tokens": tokens,
                    "credits": creditMath.tokensToCredits(tokens, TOKEN_TO_CREDIT_RATIO),
                    "amountBeforeTax": snapshot.amount_before_tax,
                    "taxAmount": snapshot.tax.tax_amount,
                    "totalAmount": snapshot.total_amount,
                    "currency": snapshot.currency,
                })
            packs.sort(key=lambda pack: pack["tokens"])

            return {"packs": packs, "eligible": eligible}
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception

    def createTopupOrder(self, packId: str, token: str) -> dict:
        """
        Create a frozen invoice and a Razorpay Order for a top-up pack.

        Tokens are granted only after payment, via verifyTopupPayment() or the
        payment.captured webhook.

        Args:
            packId (str): Pack key from config, e.g. 'medium'.
            token (str): Authorization token.

        Returns:
            dict: Checkout payload for Razorpay embedded checkout.
        """
        try:
            userId, tokenEmail = self._decodeToken(token)
            subscription = self._subscription(userId)
            if not self._isTopupEligible(subscription):
                raise Exception(
                    "TOPUP_NOT_ELIGIBLE: credit top-ups require an active Pro or "
                    f"Annual plan (status={(subscription or {}).get('status')}, "
                    f"plan={(subscription or {}).get('plan_type')})"
                )

            pack = getTopupPack(packId)
            if pack is None:
                raise Exception(f"TOPUP_PACK_UNKNOWN: no active top-up pack '{packId}'")

            billingMode = subscription.get("billing_mode") or "monthly_prepaid"
            snapshot = computeTopupSnapshot(packId, billingMode)
            tokens = pack["tokens"]

            identity = self._identity(userId, tokenEmail)
            invoice = self._createInvoice(
                userId, subscription.get("id"), snapshot, packId, tokens
            )

            order = self.razorpayClient.order.create({
                "amount": snapshot.total_amount,
                "currency": snapshot.currency,
                "notes": {
                    "userId": userId,
                    "type": "credit_topup",
                    "packId": packId,
                    "tokens": str(tokens),
                    "invoiceId": invoice["id"],
                },
            })
            self._attachOrder(invoice["id"], order["id"])

            self._audit(
                userId, "credit.topup_requested",
                amount=snapshot.total_amount,
                status="AWAITING_PAYMENT",
                metadata={
                    "packId": packId,
                    "tokens": tokens,
                    "orderId": order["id"],
                    "invoiceId": invoice["id"],
                },
            )
            logger.info(
                f"Top-up order created — userId={userId}, pack={packId}, "
                f"tokens={tokens}, order={order['id']}"
            )

            return {
                "razorpayKey": os.environ["RAZORPAY_KEY_ID"],
                "orderId": order["id"],
                "currency": snapshot.currency,
                "amount": snapshot.total_amount,
                "packId": packId,
                "tokens": tokens,
                "credits": creditMath.tokensToCredits(tokens, TOKEN_TO_CREDIT_RATIO),
                "invoiceId": invoice["id"],
                "userEmail": identity["email"],
                "userName": identity["name"],
                "userContact": identity["contact"],
                "pricingSnapshot": {
                    "pricingVersion": snapshot.pricing_version,
                    "priceSource": snapshot.pricing_reference_snapshot_json["source"],
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

    def verifyTopupPayment(self, payload: dict, token: str) -> dict:
        """
        Verify the Razorpay checkout signature and grant the purchased tokens.

        The grant is idempotent, so racing the payment.captured webhook is
        expected and safe — whichever arrives first credits the tokens and the
        other reports granted=False.

        Ownership is established from the order's notes as fetched from
        Razorpay, never from the client payload: a valid signature proves the
        payment is genuine, not that it belongs to the caller.

        Args:
            payload (dict): Razorpay checkout response.
            token (str): Authorization token.

        Returns:
            dict: {"granted": bool, "tokens": int, "credits": float}.
        """
        try:
            from api.services.credits.creditService import creditService

            userId, _ = self._decodeToken(token)
            paymentId = payload.get("razorpayPaymentId")
            orderId = payload.get("razorpayOrderId")
            signature = payload.get("razorpaySignature")
            if not all([paymentId, orderId, signature, userId]):
                raise Exception("Missing Razorpay verification fields")

            expectedSignature = hmac.new(
                os.environ["RAZORPAY_KEY_SECRET"].encode(),
                f"{orderId}|{paymentId}".encode(),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expectedSignature, signature):
                raise Exception("Invalid Razorpay signature")

            order = self.razorpayClient.order.fetch(orderId)
            notesRaw = order.get("notes") or {}
            notes = notesRaw if isinstance(notesRaw, dict) else {}
            if notes.get("type") != "credit_topup":
                raise Exception(
                    f"Order {orderId} is not a credit top-up order "
                    f"(type={notes.get('type')})"
                )
            noteUserId = notes.get("userId")
            if noteUserId and noteUserId != userId:
                raise Exception(
                    f"Order/user mismatch during top-up verification: "
                    f"order.userId={noteUserId}, token.userId={userId}"
                )

            result = creditService.grantTopupTokens(userId, orderId, paymentId)
            tokens = result["tokens"]

            if result["granted"]:
                self._audit(
                    userId, "credit.topup_granted",
                    paymentId=paymentId,
                    status="GRANTED",
                    metadata={
                        "packId": notes.get("packId"),
                        "tokens": tokens,
                        "orderId": orderId,
                        "flow": "verify",
                    },
                )

            return {
                "granted": result["granted"],
                "tokens": tokens,
                "credits": creditMath.tokensToCredits(tokens, TOKEN_TO_CREDIT_RATIO),
            }
        except Exception as e:
            exception = CustomException(e)
            logger.error(exception)
            raise exception


topupService = TopupService()
