"""Independent top-up settlement.

Top-ups have their own attempt TTL, not the monthly renewal deadline. A
valid captured top-up created while eligible may settle after subscription
expiry: the purchased tokens are stored exactly once, but NO paid time or
access is granted. Superseded/cancelled/overdue/unknown attempts are
investigated, never silently granted from a stale callback. Erasure guards
apply: grants to erased users are blocked upstream.
"""

__all__ = ["TopupSettlementService"]


import uuid


class TopupSettlementService:
    def __init__(self, store=None, now=None):
        from datetime import datetime, timezone

        self.store = store or _DefaultTopupStore()
        self.now = now or (lambda: datetime.now(timezone.utc))

    def settleCapturedTopup(
        self,
        userId: str,
        attemptKey: str,
        providerPaymentId: str,
    ) -> dict:
        """Settle a captured top-up purchase exactly once.

        Outcomes:
        - granted: tokens stored once (idempotent per provider payment).
        - reconciliation: superseded/cancelled/unknown attempts are
          investigated money, never a grant.
        Top-ups never extend access: `paidAccessGranted` is always False.
        """
        attempt = self.store.findAttempt(attemptKey)
        if attempt is None:
            return {
                "granted": False,
                "disposition": "reconciliation",
                "reason": "unknown_attempt",
                "paidAccessGranted": False,
            }
        state = (attempt.get("state") or "").lower()
        if state in ("superseded", "cancelled", "expired", "closed"):
            return {
                "granted": False,
                "disposition": "reconciliation",
                "reason": f"attempt_{state}",
                "paidAccessGranted": False,
            }
        if state == "granted":
            return {
                "granted": False,
                "duplicate": True,
                "tokens": attempt.get("tokens", 0),
                "paidAccessGranted": False,
            }
        grantOperationId = f"creditop:topup-grant:{providerPaymentId}"
        if getattr(self.store, "hasOperation", None) and self.store.hasOperation(
            grantOperationId
        ):
            return {
                "granted": False,
                "duplicate": True,
                "tokens": attempt.get("tokens", 0),
                "paidAccessGranted": False,
            }
        tokens = int(attempt.get("tokens", 0))
        if tokens <= 0:
            return {
                "granted": False,
                "disposition": "reconciliation",
                "reason": "invalid_token_amount",
                "paidAccessGranted": False,
            }
        self.store.recordGrant(userId, tokens, grantOperationId)
        self.store.addTopupTokens(userId, tokens)
        if hasattr(self.store, "recordOperation"):
            self.store.recordOperation({
                "operationId": grantOperationId,
                "operationType": "topup_granted",
                "userId": userId,
                "tokens": tokens,
                "providerPaymentId": providerPaymentId,
            })
        attempt["state"] = "granted"
        attempt["grantedAt"] = self.now().isoformat()
        return {
            "granted": True,
            "tokens": tokens,
            "operationId": grantOperationId,
            "paidAccessGranted": False,  # top-ups never buy time
        }


class _DefaultTopupStore:
    """Minimal default store (production uses the transaction repository)."""

    def __init__(self):
        self.attempts = {}
        self.balances = {}
        self.operations = []
        self._grants = []

    def findAttempt(self, attemptKey):
        return self.attempts.get(attemptKey)

    def recordGrant(self, userId, tokens, operationId):
        self._grants.append((userId, tokens, operationId))

    def addTopupTokens(self, userId, tokens):
        row = self.balances.setdefault(userId, {"topup_tokens": 0})
        row["topup_tokens"] += tokens
        return dict(row)

    def recordOperation(self, operation):
        self.operations.append(operation)

    def hasOperation(self, operationId):
        return any(op.get("operationId") == operationId for op in self.operations)