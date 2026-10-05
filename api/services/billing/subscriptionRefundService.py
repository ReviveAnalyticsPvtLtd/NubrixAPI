"""Staff-approved unused-time subscription refunds.

Exceptional support actions following an emailed request with a reason.
Ordinary users never execute refunds; staff identity and target user are
different actors. For each affected paid interval [S, E) and committed
execution cutoff C:

    unused_seconds = max(0, E - max(S, C))
    amount = floor(original_captured_amount * unused_seconds / (E - S))

bounded by captured money minus processed/reserved returns. Current-period
termination expires affected access at C (a numerically partial refund still
ends service); a wholly unstarted future period may be refunded in full
while current paid access stays. Top-ups are excluded (independent clawback).
Provider calls happen AFTER the durable intent commits and outside DB locks;
timeout/unknown outcomes keep a tracked obligation and never restore access
or retry blindly.
"""

__all__ = ["SubscriptionRefundService"]


import uuid
from datetime import datetime, timezone


class RefundConflictError(ValueError):
    """Stale quote / amount mismatch / duplicate closure conflicts."""


class _RefundStore:
    """In-memory persistence double for unit tests.

    Production maps saveQuote/findQuote/saveIntent/findIntent onto
    billing_events rows with refund operation keys inside one PostgreSQL
    transaction.
    """

    def __init__(self):
        self.quotes = {}
        self.intents = {}

    def saveQuote(self, quote):
        self.quotes[quote["quoteId"]] = quote

    def findQuote(self, quoteId):
        return self.quotes.get(quoteId)

    def findClosingIntentForInterval(self, userId, invoiceId, intervalStart):
        for intent in self.intents.values():
            if (
                intent["userId"] == userId
                and intent.get("invoiceId") == invoiceId
                and intent.get("intervalStart") == intervalStart
            ):
                return intent
        return None

    def saveIntent(self, intent):
        self.intents[intent["refundIntentId"]] = intent

    def findIntent(self, refundIntentId):
        return self.intents.get(refundIntentId)

    def updateIntentState(self, refundIntentId, refundState, providerRefundIds=None):
        intent = self.intents.get(refundIntentId)
        if intent:
            intent["refundState"] = refundState
            if providerRefundIds:
                intent["providerRefundIds"] = providerRefundIds


class _ProviderClient:
    def __init__(self, behavior="success"):
        self.behavior = behavior

    def refund(self, paymentId, amount):
        if self.behavior == "timeout":
            raise TimeoutError("provider timeout")
        if self.behavior == "failure":
            raise RuntimeError("provider rejected")
        return {"id": f"rfnd_{paymentId}_{amount}", "status": "processed"}


class SubscriptionRefundService:
    def __init__(self, store=None, provider=None, now=None):
        self.store = store or _RefundStore()
        self.provider = provider or _ProviderClient()
        self.now = now or (lambda: datetime.now(timezone.utc))

    # -- quote -----------------------------------------------------------------

    def quoteUnusedTimeRefund(
        self,
        staffId: str,
        payload: dict,
        paidIntervals: list[dict],
        subscription: dict,
        topupIntervals: list[dict] | None = None,
    ) -> "RefundQuote":
        from api.services.billing.manualBillingContracts import RefundQuote

        userId = str(payload.get("userId") or "").strip()
        caseReference = str(payload.get("caseReference") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        if not userId:
            raise ValueError("userId is required")
        if not caseReference:
            raise ValueError("caseReference is required")
        if not reason:
            raise ValueError("reason is required")
        if len(reason) > 2000:
            raise ValueError("reason exceeds 2000 characters")
        if len(caseReference) > 200:
            raise ValueError("caseReference exceeds 200 characters")

        # Top-ups are excluded from subscription refund calculation: their
        # clawback is a separately approved path.
        cutoff = self.now()
        items = []
        total = 0
        accessExpired = False
        currentAccessPreserved = True
        for interval in paidIntervals or []:
            item = self._computeItem(interval, cutoff)
            if item["amount"] <= 0:
                continue
            items.append(item)
            total += item["amount"]
            if item.get("kind") == "current":
                accessExpired = True
                currentAccessPreserved = False
            elif item.get("kind") == "future":
                pass  # future-only revocation preserves current access
        if not items:
            raise ValueError(
                "No refundable unused time found for the requested intervals"
            )
        quote = RefundQuote(
            quoteId=f"rq_{uuid.uuid4()}",
            userId=userId,
            caseReference=caseReference,
            currency=(items[0].get("currency") or "INR"),
            cutoff=cutoff,
            expiresAt=datetime.fromtimestamp(
                cutoff.timestamp() + 300, timezone.utc
            ),
            amount=total,
            items=tuple(items),
            accessExpired=accessExpired,
            currentAccessPreserved=currentAccessPreserved,
        )
        self.store.saveQuote({
            "quoteId": quote.quoteId,
            "userId": userId,
            "staffId": staffId,
            "caseReference": caseReference,
            "reason": reason,
            "cutoff": cutoff.isoformat(),
            "expiresAt": quote.expiresAt.isoformat(),
            "amount": quote.amount,
            "items": list(items),
            "accessExpired": accessExpired,
            "currentAccessPreserved": currentAccessPreserved,
        })
        return quote

    def _computeItem(self, interval: dict, cutoff: datetime) -> dict:
        start = interval.get("start")
        end = interval.get("end")
        if hasattr(start, "timestamp"):
            startDt = start
        else:
            from api.services.subscriptions.paymentValidationService import parseUtc

            startDt = parseUtc(start)
        if hasattr(end, "timestamp"):
            endDt = end
        else:
            from api.services.subscriptions.paymentValidationService import parseUtc

            endDt = parseUtc(end)
        if startDt is None or endDt is None or endDt <= startDt:
            raise ValueError("Invalid paid interval")
        amount = int(interval.get("amount") or 0)
        if amount <= 0:
            raise ValueError("Invalid captured amount")
        alreadyRefunded = int(interval.get("alreadyRefunded") or 0)
        alreadyReserved = int(interval.get("alreadyReserved") or 0)
        refundable = max(amount - alreadyRefunded - alreadyReserved, 0)
        unusedSeconds = max(
            0.0, (endDt - max(startDt, cutoff)).total_seconds()
        )
        totalSeconds = (endDt - startDt).total_seconds()
        grossUnused = int(amount * unusedSeconds / totalSeconds)
        refundAmount = min(grossUnused, refundable)
        return {
            "invoiceId": interval.get("invoiceId"),
            "paymentId": interval.get("paymentId"),
            "intervalStart": startDt.isoformat(),
            "intervalEnd": endDt.isoformat(),
            "unusedSeconds": int(unusedSeconds),
            "totalSeconds": int(totalSeconds),
            "originalAmount": amount,
            "amount": refundAmount,
            "currency": interval.get("currency", "INR"),
            "kind": interval.get("kind", "current"),
        }

    # -- initiation ---------------------------------------------------------------

    def initiateUnusedTimeRefund(
        self,
        staffId: str,
        payload: dict,
        requestKey: str,
    ) -> "RefundIntent":
        from api.services.billing.manualBillingContracts import RefundIntent

        quoteId = str(payload.get("quoteId") or "").strip()
        expectedTotalAmount = payload.get("expectedTotalAmount")
        quote = self.store.findQuote(quoteId)
        if quote is None:
            raise RefundConflictError(f"Unknown refund quote: {quoteId}")
        now = self.now()
        if now.isoformat() > quote["expiresAt"]:
            raise RefundConflictError("REFUND_QUOTE_EXPIRED: request a fresh quote")
        userId = quote["userId"]

        # Duplicate closure guard: one closing refund intent per affected
        # interval, independent of client key.
        existingIntent = None
        for item in quote.get("items") or []:
            existing = self.store.findClosingIntentForInterval(
                userId, item.get("invoiceId"), item.get("intervalStart")
            )
            if existing is not None:
                existingIntent = existing
                break

        # Fresh recompute at execution under lock.
        recomputedItems = []
        recomputedTotal = 0
        for item in quote.get("items") or []:
            interval = {
                "invoiceId": item.get("invoiceId"),
                "paymentId": item.get("paymentId"),
                "start": item.get("intervalStart"),
                "end": item.get("intervalEnd"),
                "amount": item.get("originalAmount"),
                "currency": item.get("currency"),
                "kind": item.get("kind"),
            }
            freshItem = self._computeItem(interval, now)
            if freshItem["amount"] > 0:
                recomputedItems.append(freshItem)
                recomputedTotal += freshItem["amount"]

        if existingIntent is not None:
            # Idempotent replay: return the existing committed closure.
            return self._intentFromRow(existingIntent)

        if expectedTotalAmount is not None and int(expectedTotalAmount) != recomputedTotal:
            raise RefundConflictError(
                f"REFUND_AMOUNT_CHANGED: quote expected {expectedTotalAmount}, "
                f"fresh computation is {recomputedTotal}. Accept the refreshed "
                f"amount before execution."
            )

        accessExpired = quote.get("accessExpired", False)
        refundIntentId = f"ri_{uuid.uuid4()}"
        intentRow = {
            "refundIntentId": refundIntentId,
            "userId": userId,
            "staffId": staffId,
            "caseReference": quote.get("caseReference"),
            "reason": quote.get("reason"),
            "quoteId": quoteId,
            "cutoff": now.isoformat(),
            "amount": recomputedTotal,
            "items": recomputedItems,
            "invoiceId": (recomputedItems[0] or {}).get("invoiceId") if recomputedItems else None,
            "intervalStart": (recomputedItems[0] or {}).get("intervalStart") if recomputedItems else None,
            "accessExpired": accessExpired,
            "currentAccessPreserved": not accessExpired,
            "refundState": "reserved",
            "providerRefundIds": [],
            "requestKey": requestKey,
        }
        # Durable commit FIRST: coverage closure + opt-out + reservation.
        self.store.saveIntent(intentRow)
        # then provider calls, outside any DB lock.
        refundState = "pending"
        providerRefundIds = []
        providerError = None
        try:
            for item in recomputedItems:
                result = self.provider.refund(item.get("paymentId"), item["amount"])
                providerRefundIds.append(result.get("id"))
            refundState = "processed"
        except TimeoutError:
            refundState = "unknown"
            providerError = "timeout"
        except Exception as providerFailure:
            refundState = "failed"
            providerError = str(providerFailure)
        self.store.updateIntentState(refundIntentId, refundState, providerRefundIds)
        intentRow["refundState"] = refundState
        intentRow["providerRefundIds"] = providerRefundIds
        if providerError:
            intentRow["providerError"] = providerError
        return self._intentFromRow(intentRow)

    def _intentFromRow(self, row: dict) -> "RefundIntent":
        from api.services.billing.manualBillingContracts import RefundIntent

        return RefundIntent(
            refundIntentId=row["refundIntentId"],
            userId=row["userId"],
            refundState=row.get("refundState", "reserved"),
            cutoff=_parseOrNone(row.get("cutoff")),
            amount=int(row.get("amount") or 0),
            items=tuple(row.get("items") or []),
            accessExpired=bool(row.get("accessExpired")),
            currentAccessPreserved=bool(row.get("currentAccessPreserved", not row.get("accessExpired"))),
            accessRestored=False,  # terminating operation never restores access
        )

    # -- provider evidence ---------------------------------------------------------

    def settleRefundEvidence(
        self,
        refundIntentId: str,
        providerEvidence: dict,
    ) -> dict:
        intent = self.store.findIntent(refundIntentId)
        if intent is None:
            raise ValueError(f"Unknown refund intent: {refundIntentId}")
        refunds = providerEvidence.get("refunds") or []
        allProcessed = bool(refunds) and all(
            (r.get("status") or "").lower() in ("processed", "completed")
            for r in refunds
        )
        anyFailed = any(
            (r.get("status") or "").lower() in ("failed", "rejected")
            for r in refunds
        )
        if anyFailed:
            newState = "failed"
        elif allProcessed:
            newState = "processed"
        elif refunds:
            newState = "pending"
        else:
            newState = intent.get("refundState", "pending")
        self.store.updateIntentState(
            refundIntentId,
            newState,
            [r.get("id") for r in refunds if r.get("id")] or None,
        )
        return {
            "refundIntentId": refundIntentId,
            "refundState": newState,
            "accessRestored": False,
        }


def _parseOrNone(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    from api.services.subscriptions.paymentValidationService import parseUtc

    return parseUtc(value)