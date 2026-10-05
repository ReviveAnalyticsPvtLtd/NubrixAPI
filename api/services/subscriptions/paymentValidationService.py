"""
Shared subscription package payment and lifecycle validation helpers.

The functions in this module are intentionally small and dependency-light so
endpoint services, webhook handlers, and schedulers can use the same safety
rules without duplicating high-risk payment checks.
"""

__version__ = "1.0.0"
__author__ = "Rohit Mishra"
__all__ = [
    "PaymentValidationError",
    "utcNow",
    "utcFromTimestamp",
    "parseUtc",
    "calculateSubscriptionDaysLeft",
    "mergeSubscriptionLifecycleSnapshot",
    "isPeriodExpired",
    "isAccessActive",
    "blocksNewCheckout",
    "loadPayableInvoice",
    "assertInvoiceBelongsToSubscription",
    "validateOrderPaymentAgainstInvoice",
    "normalizeChurnedSubscription",
]


import datetime
from dateutil import parser


class PaymentValidationError(ValueError):
    """Raised when a provider payment object does not match internal billing state."""


def utcNow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def utcFromTimestamp(timestamp: int | float | str | None) -> datetime.datetime | None:
    if timestamp in (None, ""):
        return None
    return datetime.datetime.fromtimestamp(int(timestamp), datetime.timezone.utc)


def parseUtc(value) -> datetime.datetime | None:
    try:
        if value in (None, "", "None", "null"):
            return None
        parsed = parser.isoparse(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _coerceUtc(value: datetime.datetime | None = None) -> datetime.datetime:
    current = value or utcNow()
    if current.tzinfo is None:
        return current.replace(tzinfo=datetime.timezone.utc)
    return current.astimezone(datetime.timezone.utc)


def calculateSubscriptionDaysLeft(
    currentPeriodEnd,
    now: datetime.datetime | None = None,
) -> int:
    """
    Calculate calendar days remaining from a subscription period end.

    ``current_period_end`` remains authoritative; this helper only derives a
    safe runtime/display value from it.
    """
    periodEnd = parseUtc(currentPeriodEnd)
    if not periodEnd:
        return 0
    current = _coerceUtc(now)
    return max(0, (periodEnd.date() - current.date()).days)


def mergeSubscriptionLifecycleSnapshot(
    billingState: dict | None,
    *,
    currentPeriodEnd,
    status: str | None,
    now: datetime.datetime | None = None,
) -> dict:
    """
    Merge a dynamic lifecycle snapshot into existing subscription billing_state.
    """
    current = _coerceUtc(now)
    merged = dict(billingState or {})
    merged["lifecycle_snapshot"] = {
        "subscription_days_left": calculateSubscriptionDaysLeft(
            currentPeriodEnd,
            now=current,
        ),
        "calculated_at": current.isoformat(),
        "current_period_end": currentPeriodEnd,
        "status": (status or "").lower() or None,
    }
    return merged


def isPeriodExpired(subscription: dict | None, now: datetime.datetime | None = None) -> bool:
    periodEnd = parseUtc((subscription or {}).get("current_period_end"))
    if not periodEnd:
        return False
    now = now or utcNow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)
    else:
        now = now.astimezone(datetime.timezone.utc)
    return periodEnd <= now


def isAccessActive(subscription: dict | None, now: datetime.datetime | None = None) -> bool:
    """Paid access requires timestamp-valid coverage of `now`.

    Status alone is never an access grant: `payment_pending` means an unpaid
    next-period invoice exists and grants nothing once the current window
    has elapsed; `cancelled` keeps paid access only while its period end is
    in the future. At the exact end instant the old period no longer grants
    access, regardless of scheduler or JWT staleness.
    """
    row = subscription or {}
    status = (row.get("status") or "").lower()
    periodEnd = parseUtc(row.get("current_period_end"))
    if periodEnd is None:
        # No stored coverage window: fail closed.
        return False
    if status in {"active", "renewal_upcoming", "payment_pending", "cancelled"}:
        return not isPeriodExpired(row, now)
    return False


def blocksNewCheckout(subscription: dict | None, now: datetime.datetime | None = None) -> bool:
    return isAccessActive(subscription, now)


def _notes(entity: dict | None) -> dict:
    raw = (entity or {}).get("notes", {}) or {}
    return raw if isinstance(raw, dict) else {}


def _raise(message: str) -> None:
    raise PaymentValidationError(message)


def loadPayableInvoice(client, invoiceId: str, userId: str, expectedBillingReason: str) -> dict:
    invoiceRows = (
        client.table("Invoices")
        .select(
            "id, userId, subscription_id, billing_reason, status, total_amount, "
            "amount, currency, razorpay_order_id"
        )
        .eq("id", invoiceId)
        .limit(1)
        .execute()
        .data
    )
    if not invoiceRows:
        _raise(f"Invoice {invoiceId} not found")
    invoice = invoiceRows[0]
    if invoice.get("userId") != userId:
        _raise("Invoice does not belong to the authenticated user")
    if invoice.get("billing_reason") != expectedBillingReason:
        _raise(
            f"Invoice {invoiceId} has billing_reason={invoice.get('billing_reason')}, "
            f"expected={expectedBillingReason}"
        )
    status = (invoice.get("status") or "").lower()
    if status in {"paid", "void"}:
        _raise(f"Invoice {invoiceId} is already resolved (status={status})")
    if status not in {"upcoming", "payment_pending"}:
        _raise(f"Invoice {invoiceId} is not payable (status={status})")
    return invoice


def assertInvoiceBelongsToSubscription(invoice: dict, subscription: dict) -> None:
    invoiceSubscriptionId = invoice.get("subscription_id")
    subscriptionId = (subscription or {}).get("id")
    if subscriptionId and not invoiceSubscriptionId:
        _raise(f"Invoice {invoice.get('id')} is missing subscription_id")
    if invoiceSubscriptionId and subscriptionId and invoiceSubscriptionId != subscriptionId:
        _raise(
            f"Invoice/subscription mismatch: invoice.subscription_id={invoiceSubscriptionId}, "
            f"subscription.id={subscriptionId}"
        )


def validateOrderPaymentAgainstInvoice(
    *,
    order: dict,
    payment: dict,
    invoice: dict,
    expectedType: str,
    expectedUserId: str,
    expectedCustomerId: str | None = None,
    requestOrderId: str | None = None,
    requireCaptured: bool = False,
) -> None:
    orderId = requestOrderId or order.get("id")
    orderNotes = _notes(order)
    paymentNotes = _notes(payment)

    if orderNotes.get("type") != expectedType:
        _raise(
            f"Order {orderId} has type={orderNotes.get('type')}, expected={expectedType}"
        )
    if orderNotes.get("userId") and orderNotes.get("userId") != expectedUserId:
        _raise(
            f"Order/user mismatch: order.userId={orderNotes.get('userId')}, "
            f"expected={expectedUserId}"
        )
    if orderNotes.get("invoiceId") and orderNotes.get("invoiceId") != invoice.get("id"):
        _raise(
            f"Order/invoice mismatch: order.invoiceId={orderNotes.get('invoiceId')}, "
            f"invoice.id={invoice.get('id')}"
        )

    invoiceOrderId = invoice.get("razorpay_order_id")
    if invoiceOrderId and orderId and invoiceOrderId != orderId:
        _raise(
            f"Invoice/order mismatch: invoice.order_id={invoiceOrderId}, "
            f"order.id={orderId}"
        )

    paymentOrderId = payment.get("order_id")
    if paymentOrderId and orderId and paymentOrderId != orderId:
        _raise(
            f"Payment/order mismatch: payment.order_id={paymentOrderId}, "
            f"order.id={orderId}"
        )

    if requireCaptured:
        paymentStatus = (payment.get("status") or "").lower()
        if paymentStatus != "captured":
            _raise(f"Payment {payment.get('id')} is not captured (status={paymentStatus})")

    orderCustomerId = order.get("customer_id")
    if expectedCustomerId and orderCustomerId and orderCustomerId != expectedCustomerId:
        _raise(
            f"Order/customer mismatch: order.customer_id={orderCustomerId}, "
            f"expected={expectedCustomerId}"
        )

    paymentCustomerId = payment.get("customer_id")
    if expectedCustomerId and paymentCustomerId and paymentCustomerId != expectedCustomerId:
        _raise(
            f"Payment/customer mismatch: payment.customer_id={paymentCustomerId}, "
            f"expected={expectedCustomerId}"
        )

    expectedAmount = invoice.get("total_amount")
    if expectedAmount is None:
        expectedAmount = invoice.get("amount")
    actualAmount = payment.get("amount")
    if expectedAmount is not None and int(actualAmount or 0) != int(expectedAmount):
        _raise(f"Amount mismatch: expected={expectedAmount}, actual={actualAmount}")

    expectedCurrency = str(invoice.get("currency") or "INR").upper()
    actualCurrency = str(payment.get("currency") or "INR").upper()
    if actualCurrency != expectedCurrency:
        _raise(f"Currency mismatch: expected={expectedCurrency}, actual={actualCurrency}")

    if paymentNotes.get("type") and paymentNotes.get("type") != expectedType:
        _raise(f"Payment note type mismatch: {paymentNotes.get('type')}")
    if paymentNotes.get("invoiceId") and paymentNotes.get("invoiceId") != invoice.get("id"):
        _raise(
            f"Payment/invoice mismatch: payment.invoiceId={paymentNotes.get('invoiceId')}, "
            f"invoice.id={invoice.get('id')}"
        )


def normalizeChurnedSubscription(
    client,
    subscription: dict,
    churn_reason: str,
    now: datetime.datetime | None = None,
    override_status: str | None = None,
) -> bool:
    """
    Apply a partial churn reset to a terminal subscription row.

    Clears period dates, entitlements, pending changes, and stale payment
    tokens while preserving ``billing_mode`` and ``razorpay_customer_id``.
    Archives previous values in ``billing_state.churn_snapshot``.

    Returns ``True`` if the DB was updated, ``False`` if already reset (no-op).
    """
    from api.services.subscriptions.subscriptionFieldUtils import buildChurnResetPayload
    from api.services.billing.billingEventService import BillingEventService

    payload = buildChurnResetPayload(
        subscription,
        churn_reason,
        now=now,
        override_status=override_status,
    )
    if payload is None:
        return False

    subscriptionId = subscription.get("id")
    client.table("subscriptions").update(payload).eq("id", subscriptionId).execute()

    try:
        BillingEventService(client).log_event(
            user_id=subscription.get("user_id"),
            subscription_id=subscriptionId,
            event_type="subscription.churn_normalized",
            event_status="NORMALIZED",
            category="lifecycle",
            metadata={
                "churn_reason": churn_reason,
                "override_status": override_status,
                "billing_mode": subscription.get("billing_mode"),
            },
        )
    except Exception:
        pass

    return True
