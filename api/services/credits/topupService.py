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
        if not subscription or subscription.get('status') == 'trial' or subscription.get('billing_mode') not in ('monthly_prepaid', 'annual_prepaid'):
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

    def createTopupOrder(self, packId: str, token: str, requestKey: str | None = None) -> dict:
        from api.services.subscriptions.subscriptionService import SubscriptionService
        userId, email = self._decodeToken(token)
        service = SubscriptionService.__new__(SubscriptionService)
        service.client = self.client
        service.razorpayClient = self.razorpayClient
        subscription = self._subscription(userId)
        if not subscription or subscription.get('status') == 'trial' or subscription.get('billing_mode') not in ('monthly_prepaid', 'annual_prepaid'):
            raise CustomException(ValueError('TOPUP_NOT_ELIGIBLE'), statusCode=403,
                uiMessage='Credit top-ups require an active paid subscription.', errorCode='TOPUP_NOT_ELIGIBLE')
        if getTopupPack(packId) is None:
            raise CustomException(ValueError('TOPUP_PACK_UNKNOWN'), statusCode=422,
                uiMessage='Unknown credit top-up pack.', errorCode='TOPUP_PACK_UNKNOWN')
        result = service._reservedCheckout({'userId': userId, 'email': email}, 'topup',
            subscription['billing_mode'], {'packId': packId}, requestKey)
        result['credits'] = creditMath.tokensToCredits(result['tokens'], TOKEN_TO_CREDIT_RATIO)
        return result

    def verifyTopupPayment(self, payload: dict, token: str) -> dict:
        from api.services.subscriptions.subscriptionService import SubscriptionService
        from api.services.billing.manualBillingRepository import getManualBillingRepository
        service = SubscriptionService.__new__(SubscriptionService)
        service.client, service.razorpayClient = self.client, self.razorpayClient
        result = service._verifyDurableCheckout(payload, token, 'topup')
        attempt = getManualBillingRepository().attemptForOrder(payload['razorpayOrderId'])
        tokens = int(getManualBillingRepository()._json(attempt['metadata_json'])['manualBilling']['tokens'])
        return {**result, 'granted': result['finalized'] and not result['alreadyFinalized'],
            'tokens': tokens, 'credits': creditMath.tokensToCredits(tokens, TOKEN_TO_CREDIT_RATIO)}


topupService = TopupService()
