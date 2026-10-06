"""Frozen dataclass contracts for the manual monthly billing system.

These types are the stable boundaries between the transaction repository,
the payment/refund services, the monthly coverage service, and the credit
engine. IDs are strings at Python boundaries; storage enforces UUID validity.
Datetimes are timezone-aware UTC; monetary values are integer minor units.
"""

__all__ = [
    "CoveragePeriod",
    "CoverageSnapshot",
    "CheckoutIntent",
    "CheckoutRequest",
    "VerifiedPaymentEvidence",
    "FinalizationResult",
    "RefundQuote",
    "RefundIntent",
    "CreditOperationContext",
]


from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class CoveragePeriod:
    userId: str
    subscriptionId: str
    lifecycleId: str
    creditPeriodId: str
    invoiceId: str
    start: datetime
    end: datetime
    domains: tuple[str, ...]
    billingMode: str
    revokedAt: datetime | None


@dataclass(frozen=True)
class CoverageSnapshot:
    userId: str
    subscriptionId: str
    lifecycleId: str
    billingMode: str
    evaluatedAt: datetime
    currentPeriod: CoveragePeriod | None
    nextPeriod: CoveragePeriod | None
    finalPaidEnd: datetime | None
    renewalOptOut: bool
    accessAllowed: bool
    denialReason: str | None


@dataclass(frozen=True)
class CheckoutRequest:
    userId: str
    purpose: str
    billingMode: str
    payload: dict
    requestKey: str | None = None


@dataclass(frozen=True)
class CheckoutIntent:
    attemptId: str
    invoiceId: str
    userId: str
    lifecycleId: str
    purpose: str
    billingMode: str
    payloadHash: str
    currency: str
    state: str
    revision: int
    amount: int
    expiresAt: datetime
    razorpayOrderId: str | None
    snapshot: dict


@dataclass(frozen=True)
class VerifiedPaymentEvidence:
    attemptId: str
    invoiceId: str
    userId: str
    providerOrderId: str
    providerPaymentId: str
    purpose: str
    currency: str
    financialStatus: str
    timingKind: str
    amount: int
    observedAt: datetime
    provenCaptureAt: datetime | None
    sourceEventId: str | None
    timingVerified: bool


@dataclass(frozen=True)
class FinalizationResult:
    invoiceId: str
    attemptId: str | None
    state: str
    creditState: str
    finalized: bool
    creditsRefilled: bool
    renewalOptOut: bool
    currentPeriod: CoveragePeriod | None
    nextPeriod: CoveragePeriod | None
    anomalyId: str | None


@dataclass(frozen=True)
class RefundQuote:
    quoteId: str
    userId: str
    caseReference: str
    currency: str
    cutoff: datetime
    expiresAt: datetime
    amount: int
    items: tuple[dict, ...]
    accessExpired: bool
    currentAccessPreserved: bool


@dataclass(frozen=True)
class RefundIntent:
    refundIntentId: str
    userId: str
    refundState: str
    cutoff: datetime
    amount: int
    items: tuple[dict, ...]
    accessExpired: bool
    currentAccessPreserved: bool
    accessRestored: bool


@dataclass(frozen=True)
class CreditOperationContext:
    userId: str
    lifecycleId: str
    creditPeriodId: str
    operationId: str
    operationType: str
    accountingReference: str
    admittedAt: datetime
    subscriptionId: str | None = None
    billingMode: str | None = None
    quotaWatermark: int | None = None
