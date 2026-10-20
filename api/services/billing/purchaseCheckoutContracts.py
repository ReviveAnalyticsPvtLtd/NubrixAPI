"""Public status facts are separate from an instruction to open checkout."""
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class PurchaseCheckoutCapabilities:
    mayEditDraft: bool = False
    mayResumeOriginal: bool = False
    maySubmitLatestDraft: bool = False
    mayRequestCancellation: bool = False


@dataclass(frozen=True)
class PurchaseCheckoutStatus:
    userId: str
    invoiceId: str
    attemptId: str
    purpose: str
    state: Literal['unpaid', 'awaiting_capture', 'processing', 'status_unknown',
                   'cancellation_pending', 'cancelled', 'refund_pending', 'refunded',
                   'completed', 'needs_attention']
    providerState: Literal['unpaid', 'authorized', 'processing', 'captured', 'unknown', 'released']
    pendingReason: str | None = None
    operationRevision: int = 0
    cancellationAcceptedAt: str | None = None
    refundState: str = 'none'
    capabilities: PurchaseCheckoutCapabilities = field(default_factory=PurchaseCheckoutCapabilities)


@dataclass(frozen=True)
class PurchaseDraft:
    userId: str
    revision: int = 0
    domains: tuple[str, ...] = ()
    billingInterval: Literal['month', 'year'] = 'month'
    updatedAt: str | None = None


@dataclass(frozen=True)
class PurchaseEditorState:
    userId: str
    draft: PurchaseDraft
    enabled: bool = False
    checkout: PurchaseCheckoutStatus | None = None
    capabilities: PurchaseCheckoutCapabilities = field(default_factory=PurchaseCheckoutCapabilities)
