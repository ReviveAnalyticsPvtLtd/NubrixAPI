"""Durable settlement of admitted credit operations.

An operation's usage is attributed to the credit period that admitted it,
exactly once, even when the callback arrives after expiry/refund/activation
of a newer period. Admission persists the context before work starts and
rechecks eligibility; settlement floors at zero and never charges a newly
refilled period for old work.
"""

__all__ = ["CreditOperationSettlement"]


from api.services.billing.manualBillingContracts import CreditOperationContext
from api.services.credits.creditPeriodEngine import CreditPeriodEngine


class CreditOperationSettlement:
    def __init__(self, engine=None, store=None):
        self.engine = engine or CreditPeriodEngine()
        self.store = store or getattr(self.engine, "store", None)
        if self.store is None:
            self.store = CreditPeriodEngine().store

    # -- admission --------------------------------------------------------------

    def admitCreditOperation(
        self,
        userId: str,
        operationType: str,
        operationId: str,
        lifecycleId: str,
        creditPeriodId: str | None,
        accountingReference: str | None = None,
        admittedAt=None,
    ) -> CreditOperationContext:
        """Persist the admission context before counted work starts.

        A missing credit period fails closed: no valid paid period means no
        admission (queued work rechecks eligibility at execution start).
        """
        from datetime import datetime, timezone

        now = admittedAt or datetime.now(timezone.utc)
        row = self.store.getBalance(userId) if hasattr(self.store, "getBalance") else None
        resolvedPeriod = creditPeriodId or (
            (row or {}).get("credit_period_id") if row else None
        )
        if resolvedPeriod is None:
            raise ValueError(
                "ADMISSION_DENIED: no valid paid credit period exists for "
                "this operation"
            )
        lifecycle = lifecycleId or ((row or {}).get("lifecycle_id") if row else None) or "unknown"
        context = CreditOperationContext(
            userId=userId,
            lifecycleId=str(lifecycle),
            creditPeriodId=str(resolvedPeriod),
            operationId=operationId,
            operationType=operationType,
            accountingReference=accountingReference or operationId,
            admittedAt=now,
        )
        self.store.recordOperation({
            "operationId": f"creditop:admit:{operationId}",
            "operationType": "admitted",
            "userId": userId,
            "creditPeriodId": resolvedPeriod,
            "lifecycleId": lifecycle,
            "operationKind": operationType,
            "admittedAt": now.isoformat(),
        })
        return context

    # -- settlement ---------------------------------------------------------------

    def settleCreditOperation(
        self,
        context: CreditOperationContext,
        tokensUsed: int,
    ) -> dict:
        """Settle real usage once against the originally admitted period.

        Duplicate settlement of the same operation is a no-op. Overrun
        floors remaining at zero and preserves the usage/debt audit.
        """
        operationId = f"creditop:settle:{context.operationId}"
        if self.store.hasOperation(operationId):
            return {
                "settled": False,
                "duplicate": True,
                "operationId": operationId,
                "creditPeriodId": context.creditPeriodId,
            }
        row = self.store.getBalance(context.userId)
        if row is None:
            # The admitted period's durable state is missing: record the
            # settlement against the operation ledger (attribution survives)
            # without charging any newer period.
            self.store.recordOperation({
                "operationId": operationId,
                "operationType": "settled_orphaned_period",
                "userId": context.userId,
                "creditPeriodId": context.creditPeriodId,
                "tokensUsed": tokensUsed,
                "admittedAt": context.admittedAt.isoformat(),
            })
            return {
                "settled": True,
                "operationId": operationId,
                "creditPeriodId": context.creditPeriodId,
                "orphaned": True,
            }
        used = int(row.get("used_tokens", 0)) + int(tokensUsed)
        quota = int(row.get("monthly_token_quota", 0))
        remaining = max(quota - used, 0)
        # Attribution guard: settlement applies to the ORIGINAL period only.
        # If the durable row has advanced to a different credit period, the
        # usage is recorded against the operation ledger without touching
        # the new period's quota.
        if row.get("credit_period_id") not in (None, context.creditPeriodId):
            self.store.recordOperation({
                "operationId": operationId,
                "operationType": "settled_original_period_history",
                "userId": context.userId,
                "creditPeriodId": context.creditPeriodId,
                "tokensUsed": tokensUsed,
                "admittedAt": context.admittedAt.isoformat(),
            })
            return {
                "settled": True,
                "operationId": operationId,
                "creditPeriodId": context.creditPeriodId,
                "historicalOnly": True,
            }
        self.store.upsertBalance(context.userId, {
            "used_tokens": used,
            "remaining_tokens": remaining,
            "balance_version": row.get("balance_version", 0) + 1,
        })
        self.store.recordOperation({
            "operationId": operationId,
            "operationType": "settled",
            "userId": context.userId,
            "creditPeriodId": context.creditPeriodId,
            "tokensUsed": tokensUsed,
            "admittedAt": context.admittedAt.isoformat(),
        })
        return {
            "settled": True,
            "operationId": operationId,
            "creditPeriodId": context.creditPeriodId,
        }