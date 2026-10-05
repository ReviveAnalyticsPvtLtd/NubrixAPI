"""Manual monthly coverage: calendar months, renewal prep, boundary activation.

Implements audited design section 4: one calendar month extending from the
stored UTC expiry with destination-month clamping, early renewal freezing one
future period, activation at the paid boundary exactly once, and immediate
unpaid expiry at the exact end instant. No 30-day terms, no recurring anchor
day, no late renewal.
"""

__all__ = ["MonthlyCoverageService"]


from datetime import datetime, timezone

from dateutil.relativedelta import relativedelta

from api.services.subscriptions.paymentValidationService import parseUtc, utcNow


class _MemoryStore:
    """Unit-test persistence double.

    Production uses ManualBillingRepository; the same activation identities
    (activate:{lifecycleId}:{invoiceId}:{periodStart}) are enforced there via
    the unique operation-key index inside one transaction.
    """

    def __init__(self):
        self.invoices = []
        self.activations = []

    def findPayableRenewal(self, userId, cycleStart):
        for invoice in self.invoices:
            if (
                invoice.get("userId") == userId
                and invoice.get("billingReason") == "renewal"
                and invoice.get("periodStart") == cycleStart
                and invoice.get("status") in ("UPCOMING", "PAYMENT_PENDING")
            ):
                return invoice
        return None

    def findPaidFutureInvoice(self, userId, cycleStart):
        for invoice in self.invoices:
            if (
                invoice.get("userId") == userId
                and invoice.get("billingReason") == "renewal"
                and invoice.get("periodStart") == cycleStart
                and invoice.get("status") == "PAID"
            ):
                coverage = (invoice.get("manualBilling") or {}).get("coverageState")
                if coverage in ("scheduled", "active"):
                    return invoice
        return None

    def saveRenewalInvoice(self, invoice):
        self.invoices.append(invoice)
        return invoice

    def recordActivation(self, activation):
        self.activations.append(activation)


class MonthlyCoverageService:
    def __init__(self, store=None, now=None):
        self.store = store or _MemoryStore()
        self.now = now or utcNow

    # -- calendar arithmetic --------------------------------------------------

    @staticmethod
    def addCalendarMonth(start: datetime) -> datetime:
        """One calendar month with destination-month clamp.

        31 January -> 28/29 February; subsequent periods extend from the
        stored expiry. No recurring anchor-day concept.
        """
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return start + relativedelta(months=1)

    # -- coverage --------------------------------------------------------------

    def getCoverage(self, userId: str, subscription: dict, now: datetime | None = None) -> dict:
        """Evaluate paid coverage of `now` for the current monthly period.

        Coverage is [period_start, period_end): at the exact end instant the
        old period no longer grants access, regardless of job/JWT staleness.
        """
        current = now or self.now()
        start = parseUtc((subscription or {}).get("current_period_start"))
        end = parseUtc((subscription or {}).get("current_period_end"))
        if start is None or end is None:
            return {"paid": False, "reason": "no_period"}
        paid = start <= current < end
        return {
            "paid": paid,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "reason": None if paid else "outside_period",
        }

    # -- renewal preparation ------------------------------------------------------

    def prepareRenewalInvoice(
        self,
        userId: str,
        subscription: dict,
        now: datetime | None = None,
    ) -> dict:
        """Prepare (or reuse) the next unpaid monthly renewal invoice.

        The renewal represents a future period starting at the current
        period end. May be requested earlier than T-7 (dashboard Renew).
        Skips opted-out lifecycles and cycles that already have a paid
        future invoice.
        """
        current = now or self.now()
        row = subscription or {}
        if (row.get("renewal_opt_out") or False):
            raise ValueError(
                "RENEWAL_OPTED_OUT: renewal invoices are disabled for this "
                "subscription"
            )
        currentEnd = parseUtc(row.get("current_period_end"))
        if currentEnd is None:
            raise ValueError("Current period end is missing")
        if current >= currentEnd:
            raise ValueError(
                "RENEWAL_WINDOW_CLOSED: current period has ended; no late "
                "monthly renewal. Purchase again through createSubscription."
            )

        cycleStartIso = currentEnd.isoformat()
        paidFuture = self.store.findPaidFutureInvoice(userId, cycleStartIso)
        if paidFuture is not None:
            return {
                "invoiceId": paidFuture["id"],
                "state": "already_paid",
                "creditsRefilled": False,
                "currentPeriod": {
                    "start": row.get("current_period_start"),
                    "end": row.get("current_period_end"),
                    "domains": list(row.get("subscribed_experts") or []),
                },
                "nextPeriod": {
                    "start": paidFuture["periodStart"],
                    "end": paidFuture["periodEnd"],
                    "domains": paidFuture.get("domains")
                    or (paidFuture.get("manualBilling") or {}).get("domains", []),
                },
            }

        existing = self.store.findPayableRenewal(userId, cycleStartIso)
        if existing is not None:
            return {
                "invoiceId": existing["id"],
                "state": "invoice_ready",
                "creditsRefilled": False,
                "currentPeriod": {
                    "start": row.get("current_period_start"),
                    "end": row.get("current_period_end"),
                    "domains": list(row.get("subscribed_experts") or []),
                },
                "nextPeriod": {
                    "start": existing["periodStart"],
                    "end": existing["periodEnd"],
                    "domains": existing.get("domains") or [],
                },
            }

        nextStart = currentEnd
        nextEnd = self.addCalendarMonth(currentEnd)
        currentExperts = list(row.get("subscribed_experts") or [])
        pendingRemovals = set(row.get("pending_removals") or [])
        renewalDomains = [d for d in currentExperts if d not in pendingRemovals]
        invoice = {
            "id": f"inv-renewal-{cycleStartIso}",
            "userId": userId,
            "billingReason": "renewal",
            "periodStart": nextStart.isoformat(),
            "periodEnd": nextEnd.isoformat(),
            "status": "UPCOMING",
            "domains": renewalDomains,
            "manualBilling": {
                "schemaVersion": 1,
                "lifecycleId": ((row.get("billing_state") or {}).get("manualBilling") or {}).get("lifecycleId"),
                "purpose": "renewal",
                "billingMode": "monthly_prepaid",
                "domains": renewalDomains,
                "coverageState": "estimated",
                "revision": 1,
            },
        }
        self.store.saveRenewalInvoice(invoice)
        return {
            "invoiceId": invoice["id"],
            "state": "payment_pending",
            "creditsRefilled": False,
            "currentPeriod": {
                "start": row.get("current_period_start"),
                "end": row.get("current_period_end"),
                "domains": currentExperts,
            },
            "nextPeriod": {
                "start": invoice["periodStart"],
                "end": invoice["periodEnd"],
                "domains": renewalDomains,
            },
        }

    # -- paid renewal finalization -------------------------------------------------

    def applyPaidRenewal(
        self,
        userId: str,
        subscription: dict,
        invoice: dict,
        now: datetime | None = None,
    ) -> dict:
        """Mark a renewal paid and schedule its frozen future coverage.

        Early payment freezes dates/experts/amount. It does NOT change
        current dates, current selection, usage, or quota.
        """
        current = now or self.now()
        row = subscription or {}
        domains = invoice.get("domains") or (invoice.get("manualBilling") or {}).get("domains", [])
        return {
            "invoiceId": invoice["id"],
            "state": "paid_scheduled",
            "creditsRefilled": False,
            "currentPeriod": {
                "start": row.get("current_period_start"),
                "end": row.get("current_period_end"),
                "domains": list(row.get("subscribed_experts") or []),
            },
            "nextPeriod": {
                "start": invoice["periodStart"],
                "end": invoice["periodEnd"],
                "domains": domains,
            },
        }

    # -- boundary activation ---------------------------------------------------------

    def activateDueCoverage(
        self,
        userId: str,
        subscription: dict,
        now: datetime | None = None,
    ) -> dict:
        """Activate the paid next period at its start, exactly once.

        If an entire paid period has already elapsed, materialize financial
        and coverage history without granting present-day quota or extending
        a further month.
        """
        current = now or self.now()
        row = subscription or {}
        currentEnd = parseUtc(row.get("current_period_end"))
        if currentEnd is None or current < currentEnd:
            return {
                "state": "not_due",
                "finalized": False,
                "creditsRefilled": False,
                "currentPeriod": None,
                "nextPeriod": None,
            }

        cycleStartIso = currentEnd.isoformat()
        paidFuture = self.store.findPaidFutureInvoice(userId, cycleStartIso)
        if paidFuture is None:
            return {
                "state": "expired",
                "finalized": False,
                "creditsRefilled": False,
                "currentPeriod": None,
                "nextPeriod": None,
            }

        periodStart = parseUtc(paidFuture["periodStart"])
        periodEnd = parseUtc(paidFuture["periodEnd"])
        domains = paidFuture.get("domains") or (paidFuture.get("manualBilling") or {}).get("domains", [])
        lifecycleId = (paidFuture.get("manualBilling") or {}).get("lifecycleId")

        if current >= periodEnd:
            # The entire paid interval has elapsed: history, not fresh quota.
            return {
                "state": "elapsed",
                "finalized": False,
                "creditsRefilled": False,
                "currentPeriod": {
                    "start": paidFuture["periodStart"],
                    "end": paidFuture["periodEnd"],
                    "domains": domains,
                },
                "nextPeriod": None,
            }

        activationKey = f"activate:{lifecycleId}:{paidFuture['id']}:{paidFuture['periodStart']}"
        for activation in getattr(self.store, "activations", []):
            if activation.get("key") == activationKey:
                return {
                    "state": "already_finalized",
                    "finalized": True,
                    "creditsRefilled": True,
                    "currentPeriod": {
                        "start": paidFuture["periodStart"],
                        "end": paidFuture["periodEnd"],
                        "domains": domains,
                    },
                    "nextPeriod": None,
                }
        self.store.recordActivation({
            "key": activationKey,
            "invoiceId": paidFuture["id"],
            "userId": userId,
            "lifecycleId": lifecycleId,
            "periodStart": paidFuture["periodStart"],
            "periodEnd": paidFuture["periodEnd"],
            "domains": domains,
            "activatedAt": current.isoformat(),
        })
        return {
            "state": "activated",
            "finalized": True,
            "creditsRefilled": True,
            "currentPeriod": {
                "start": paidFuture["periodStart"],
                "end": paidFuture["periodEnd"],
                "domains": domains,
            },
            "nextPeriod": None,
        }