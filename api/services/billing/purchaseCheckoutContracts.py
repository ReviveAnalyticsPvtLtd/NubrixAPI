"""Public status facts are separate from an instruction to open checkout."""
from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class PurchaseCheckoutCapabilities:
    mayEditDraft: bool = False
    maySubmitLatestDraft: bool = False


@dataclass(frozen=True)
class PurchaseCheckoutStatus:
    userId: str
    invoiceId: str
    attemptId: str
    purpose: str
    # Pay is always the action; the server waits only while an earlier payment is confirming.
    state: Literal['ready', 'confirming_earlier_payment', 'completed']
    providerState: Literal['unpaid', 'authorized', 'processing', 'captured', 'unknown', 'released']
    pendingReason: str | None = None
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
