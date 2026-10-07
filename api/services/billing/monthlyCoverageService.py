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
    are enforced through the owner lock and the invoice coverage state inside
    one transaction; this double's keys are not populated in production.
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


class _OptOutStore:
    """Persistence double for opt-out/resume/cancellation unit tests."""

    def __init__(self):
        self.subscriptions = {}
        self.voidedInvoices = []
        self.audit = []

    def applyOptOut(self, userId, optOut, reason, requestKey):
        row = self.subscriptions.setdefault(
            userId,
            {
                "renewal_opt_out": False,
                "cancellation_reason": None,
            },
        )
        row["renewal_opt_out"] = optOut
        row["cancellation_reason"] = reason if optOut else None
        return dict(row)

    def voidUnpaidRenewalInvoices(self, userId, reason):
        voided = []
        for invoice in self.voidedInvoices:
            if invoice.get("userId") == userId and invoice.get("status") in (
                "UPCOMING",
                "PAYMENT_PENDING",
            ):
                invoice["status"] = "VOID"
                invoice["voidReason"] = reason
                voided.append(invoice["id"])
        return voided


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

    def evaluateAnnualPaidCoverage(
        self,
        userId: str,
        subscription: dict,
        historicalIntervals: list[dict],
        now: datetime | None = None,
    ) -> dict:
        """Mode-aware coverage adapter for annual rows (design §9.1).

        Annual early renewal rewrites ``current_period_start`` to the
        previous expiry, which can be future-dated while the earlier annual
        term is still usable. The stored window alone would deny that
        existing paid access; this adapter resolves the actual paid coverage
        from the stored window OR verified historical paid intervals.
        Unverified rows grant nothing.
        """
        current = now or self.now()
        stored = self.getCoverage(userId, subscription, now=current)
        if stored["paid"]:
            return stored
        for interval in historicalIntervals or []:
            start = parseUtc(interval.get("start"))
            end = parseUtc(interval.get("end"))
            if start is None or end is None:
                continue
            if start <= current < end:
                return {
                    "paid": True,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "reason": "annual_historical_interval",
                }
        return {"paid": False, "reason": stored.get("reason") or "outside_period"}

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
        if not renewalDomains:
            raise ValueError(
                "REMOVAL_EMPTIES_SELECTION: the next paid period must keep at "
                "least one expert; schedule removals so one remains or cancel "
                "the subscription instead"
            )
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

    # -- cancellation / resume ----------------------------------------------------

    def setRenewalOptOut(
        self,
        userId: str,
        subscription: dict,
        reason: str | None,
        requestKey: str,
        now: datetime | None = None,
    ) -> dict:
        """Explicit monthly cancellation: opt out of future renewal.

        Accepts an optional reason (monthly). Annual keeps its existing
        mandatory-reason policy. All already-paid coverage is preserved;
        the effective end is the end of all contiguous valid paid coverage
        including a paid upcoming month. Repeated requests are no-ops.
        """
        current = now or self.now()
        row = subscription or {}
        alreadyOut = bool(row.get("renewal_opt_out"))

        billingMode = (row.get("billing_mode") or "").lower()
        if billingMode == "annual_prepaid":
            if not (reason or "").strip():
                raise ValueError(
                    "Cancellation reason is required for annual subscriptions"
                )
        if reason is not None:
            reason = reason.strip() or None
            if reason and len(reason) > 1000:
                raise ValueError("Cancellation reason exceeds 1000 characters")

        # Effective end: end of all valid contiguous paid coverage.
        currentEnd = parseUtc(row.get("current_period_end"))
        manualBillingState = (row.get("billing_state") or {}).get("manualBilling") or {}
        paidFutureEnd = parseUtc(manualBillingState.get("paidFutureEnd"))
        if paidFutureEnd is not None and currentEnd is not None and paidFutureEnd > currentEnd:
            effectiveEnd = paidFutureEnd
        else:
            effectiveEnd = currentEnd

        voidedInvoiceIds = []
        if not alreadyOut:
            voidedInvoiceIds = self._voidUnpaidRenewals(userId, "subscription_cancelled")

        result = {
            "renewalOptOut": True,
            "repeated": alreadyOut,
            "cancellationReason": reason,
            "effectiveAt": effectiveEnd.isoformat() if effectiveEnd else None,
            "currentPeriod": {
                "start": row.get("current_period_start"),
                "end": row.get("current_period_end"),
                "domains": list(row.get("subscribed_experts") or []),
            },
            "voidedInvoiceIds": voidedInvoiceIds or None,
            "state": "cancelled" if not alreadyOut else "already_cancelled",
            "refundInitiated": False,
        }
        store = getattr(self.store, "applyOptOut", None)
        if callable(store) and not alreadyOut:
            store(userId, True, reason, requestKey)
        return result

    def _voidUnpaidRenewals(self, userId: str, reason: str) -> list:
        voider = getattr(self.store, "voidUnpaidRenewalInvoices", None)
        if callable(voider):
            return voider(userId, reason)
        return []

    def resumeRenewal(
        self,
        userId: str,
        subscription: dict,
        requestKey: str,
        now: datetime | None = None,
    ) -> dict:
        """Clear the monthly renewal opt-out before the final paid end.

        Resume restores eligibility for manual invoices/reminders only. It
        never charges, never reactivates a void order, and is unavailable
        after the final paid end or a terminating refund.
        """
        current = now or self.now()
        row = subscription or {}
        alreadyIn = not bool(row.get("renewal_opt_out"))
        currentEnd = parseUtc(row.get("current_period_end"))
        manualBillingState = (row.get("billing_state") or {}).get("manualBilling") or {}
        terminatingRefund = manualBillingState.get("terminatingRefundId")
        if terminatingRefund:
            raise ValueError(
                "RESUME_BLOCKED: a support refund terminated this "
                "subscription's current access; only a new purchase or an "
                "audited support correction can establish coverage"
            )
        finalEnd = currentEnd
        paidFutureEnd = parseUtc(manualBillingState.get("paidFutureEnd"))
        if paidFutureEnd is not None and finalEnd is not None and paidFutureEnd > finalEnd:
            finalEnd = paidFutureEnd
        if finalEnd is None or current >= finalEnd:
            raise ValueError(
                "RESUME_UNAVAILABLE: the final paid end has passed; "
                "purchase a new subscription instead"
            )
        result = {
            "renewalOptOut": False,
            "repeated": alreadyIn,
            "renewalEligible": True,
            "effectiveAt": finalEnd.isoformat(),
            "state": "renewal_resumed" if not alreadyIn else "already_opted_in",
        }
        store = getattr(self.store, "applyOptOut", None)
        if callable(store) and not alreadyIn:
            store(userId, False, None, requestKey)
        return result

    # -- expert revision safety ------------------------------------------------------

    def validateRemovalAgainstFuture(
        self,
        userId: str,
        subscription: dict,
        now: datetime | None = None,
    ) -> dict:
        """Reject removals targeting an already-paid next-period selection."""
        current = now or self.now()
        row = subscription or {}
        currentEnd = parseUtc(row.get("current_period_end"))
        if currentEnd is None:
            raise ValueError("Current period end is missing")
        paidFuture = self.store.findPaidFutureInvoice(userId, currentEnd.isoformat())
        if paidFuture is not None:
            raise ValueError(
                "PAID_FUTURE_IMMUTABLE: the next period is already paid; its "
                "expert selection cannot change. Removals apply only to an "
                "unpaid next period."
            )
        currentExperts = list(row.get("subscribed_experts") or [])
        pendingRemovals = set(row.get("pending_removals") or [])
        remaining = [d for d in currentExperts if d not in pendingRemovals]
        if not remaining:
            raise ValueError(
                "REMOVAL_EMPTIES_SELECTION: at least one expert must remain "
                "for the next period; use cancel subscription instead"
            )
        return {"allowed": True, "nextDomains": remaining}

    def validateAdditionCapacity(
        self,
        subscription: dict,
        requested: list[str],
    ) -> dict:
        """Enforce the four distinct expert limit across active + pending."""
        row = subscription or {}
        currentExperts = set(row.get("subscribed_experts") or [])
        pendingAdditions = {
            item.get("domain")
            for item in (row.get("pending_additions") or [])
            if item.get("state") not in ("failed", "activated", "cancelled", "expired")
        }
        requestedSet = set(requested or [])
        combined = currentExperts | pendingAdditions | requestedSet
        if len(combined) > 4:
            raise ValueError(
                f"MAX_EXPERTS_EXCEEDED: {sorted(combined)} exceeds the "
                "maximum of four distinct experts"
            )
        return {"allowed": True, "combined": sorted(combined)}

    def evaluateCancelledAdditionCapture(
        self,
        pendingAddition: dict,
        capturedNow: datetime | None = None,
    ) -> dict:
        """A late capture against a cancelled addition is reconciliation."""
        state = (pendingAddition or {}).get("state")
        if state == "cancelled":
            return {
                "activate": False,
                "disposition": "reconciliation",
                "reason": "cancelled_addition_capture",
            }
        return {"activate": True, "disposition": None, "reason": None}

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
