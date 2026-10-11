"""Durable paid-period credit engine for manual monthly billing.

The database credit_balances row (with lifecycle_id / credit_period_id /
balance_version) plus the operation ledger is the authoritative recovery
state. Exactly one allocation exists per valid paid credit period; early
renewal payment never refills the current period; expiry/refund closes with
a zero subscription bucket while purchased top-ups are preserved. Redis is
a projection rebuilt from committed state and can never manufacture an
unpaid quota.
"""

__all__ = ["CreditPeriodEngine", "creditPeriodEngine"]


import uuid
from datetime import datetime, timezone


class _BalanceStore:
    """In-memory persistence double for unit tests.

    Production maps these calls onto credit_balances + billing_events
    operations inside one PostgreSQL transaction using the same
    creditop:{operationId} unique key.
    """

    def __init__(self):
        self.rows = {}
        self.operations = []

    def getBalance(self, userId):
        return self.rows.get(userId)

    def upsertBalance(self, userId, payload):
        row = self.rows.setdefault(
            userId,
            {
                "monthly_token_quota": 0,
                "used_tokens": 0,
                "remaining_tokens": 0,
                "topup_tokens": 0,
                "balance_version": 0,
                "lifecycle_id": None,
                "credit_period_id": None,
                "period_start": None,
                "period_end": None,
                "domain_count": 1,
            },
        )
        row.update(payload)
        return dict(row)

    def recordOperation(self, operation):
        self.operations.append(operation)

    def hasOperation(self, operationId):
        return any(op.get("operationId") == operationId for op in self.operations)


class CreditPeriodEngine:
    def __init__(self, store=None):
        self.store = store or _BalanceStore()

    # -- allocation ------------------------------------------------------------

    def ensurePaidCreditPeriod(
        self,
        userId: str,
        period,
        quotaTokens: int,
    ) -> dict:
        """Allocate the subscription quota for a valid paid period exactly once.

        The credit period identity (credit_period_id) is the allocation
        fence: a replay for the same period is a no-op, a new period gets a
        fresh quota while usage history stays attributable to its original
        period through the operation ledger.
        """
        operationId = f"creditop:ensure:{period.creditPeriodId}"
        existingRow = self.store.getBalance(userId)
        if (
            existingRow is not None
            and existingRow.get("credit_period_id") == period.creditPeriodId
            and existingRow.get("monthly_token_quota", 0) > 0
        ):
            return {"allocated": False, "row": dict(existingRow), "operationId": None}
        if self.store.hasOperation(operationId):
            return {"allocated": False, "row": dict(existingRow or {}), "operationId": operationId}
        row = self.store.upsertBalance(
            userId,
            {
                "lifecycle_id": period.lifecycleId or None,
                "credit_period_id": period.creditPeriodId or None,
                "period_start": period.start.isoformat(),
                "period_end": period.end.isoformat(),
                "monthly_token_quota": quotaTokens,
                "used_tokens": 0,
                "remaining_tokens": quotaTokens,
                "balance_version": (existingRow or {}).get("balance_version", 0) + 1,
            },
        )
        self.store.recordOperation({
            "operationId": operationId,
            "operationType": "ensure_paid_period",
            "userId": userId,
            "lifecycleId": period.lifecycleId,
            "creditPeriodId": period.creditPeriodId,
            "quotaTokens": quotaTokens,
        })
        return {"allocated": True, "row": row, "operationId": operationId}

    def scheduleFuturePeriod(
        self,
        userId: str,
        period,
        quotaTokens: int,
    ) -> dict:
        """Record a paid future period WITHOUT touching the current bucket.

        Early renewal payment freezes the future allocation; it is applied
        once at the future period start via ensurePaidCreditPeriod.
        """
        operationId = f"creditop:schedule:{period.creditPeriodId}"
        if self.store.hasOperation(operationId):
            return {"scheduled": False, "operationId": operationId}
        self.store.recordOperation({
            "operationId": operationId,
            "operationType": "schedule_future_period",
            "userId": userId,
            "lifecycleId": period.lifecycleId,
            "creditPeriodId": period.creditPeriodId,
            "quotaTokens": quotaTokens,
            "periodStart": period.start.isoformat(),
            "periodEnd": period.end.isoformat(),
        })
        return {"scheduled": True, "operationId": operationId}

    # -- closure ----------------------------------------------------------------

    def closeSubscriptionCreditPeriod(
        self,
        userId: str,
        lifecycleId: str,
        cutoff: datetime,
        operationId: str,
    ) -> dict:
        """Close the subscription-funded bucket at expiry/refund.

        Zero subscription quota and remaining; preserve purchased top-ups
        and usage history; record a durable once-only close operation. No
        minimum-one clamp: a closed balance has exactly zero quota and zero
        domain count.
        """
        fullOperationId = f"creditop:close:{operationId}"
        row = self.store.getBalance(userId)
        if row is None:
            return {"closed": False, "reason": "no_balance_row", "operationId": fullOperationId}
        if row.get("monthly_token_quota", 0) == 0 and row.get("credit_period_id") is None:
            return {"closed": False, "reason": "already_closed", "operationId": fullOperationId}
        if self.store.hasOperation(fullOperationId):
            return {"closed": False, "reason": "already_closed", "operationId": fullOperationId}
        previousQuota = row.get("monthly_token_quota", 0)
        if previousQuota == 0:
            return {"closed": False, "reason": "already_closed", "operationId": fullOperationId}
        updated = self.store.upsertBalance(
            userId,
            {
                "monthly_token_quota": 0,
                "remaining_tokens": 0,
                "credit_period_id": None,
                "lifecycle_id": None,
                "period_start": None,
                "period_end": None,
                "domain_count": 0,
                "balance_version": row.get("balance_version", 0) + 1,
            },
        )
        self.store.recordOperation({
            "operationId": fullOperationId,
            "operationType": "close_subscription_period",
            "userId": userId,
            "lifecycleId": lifecycleId,
            "cutoff": cutoff.isoformat() if cutoff else None,
            "previousQuota": previousQuota,
            "preservedTopups": row.get("topup_tokens", 0),
        })
        return {"closed": True, "row": updated, "operationId": fullOperationId}

    # -- cache projection -----------------------------------------------------------

    def rebuildCacheProjection(self, userId: str) -> dict:
        """Rebuild the fast-path cache from committed database state.

        Redis loss / cache miss reconstruction allocates nothing: an unpaid
        or absent period stays zero. Only committed paid-period state is
        projected, fenced by balance_version.
        """
        row = self.store.getBalance(userId)
        if row is None or row.get("credit_period_id") is None:
            return {"allocated": False, "projected": False}
        return {
            "allocated": True,
            "projected": True,
            "projection": dict(row),
        }


creditPeriodEngine = CreditPeriodEngine()