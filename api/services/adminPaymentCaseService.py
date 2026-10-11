"""Audited administrator actions on received-money payment cases.

A note records investigation progress only: the capture stays unresolved, its
money stays visible and renewal solicitations stay held. A recheck re-reads the
provider payment before the transaction and asks the original-purchase rules to
reconsider the recorded capture once. Any finalization, the idempotent action
result and the strict admin audit row commit in one owner-locked transaction,
so an audit failure rolls back the grant. Refunds and overrides are not actions.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone

from loguru import logger
from psycopg2.extras import Json, RealDictCursor

from api.adminErrors import AdminApiError
from api.adminModels import AdminPaymentCaseActionRequest
from api.services.adminAuthService import AdminContext


ACTION_EVENT = "admin.payment_case.action"
_UNAVAILABLE = "Payment case action is temporarily unavailable"


class PaymentCaseNotFound(Exception):
    pass


class PaymentCaseConflict(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _actionKey(adminId: str, keyHash: str) -> str:
    return f"admin-payment-case:{adminId}:{keyHash}"


def _validIdempotencyKey(value) -> str:
    key = value.strip() if isinstance(value, str) else ""
    if not key or len(key) > 128:
        raise AdminApiError(422, "Validation failed",
                            {"Idempotency-Key": "Must be 1-128 non-blank characters"})
    return key


class AdminPaymentCaseRepository:
    def __init__(self, billing):
        self.billing = billing

    def _read(self, query, parameters):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(query, parameters)
                return cursor.fetchone()
        return self.billing._run(operation)

    def storedAction(self, adminId: str, keyHash: str) -> dict | None:
        row = self._read("select metadata_json from public.billing_events where idempotency_key=%s",
                         (_actionKey(adminId, keyHash),))
        return self.billing._json(row["metadata_json"]) if row else None

    def caseForAction(self, captureId: str) -> dict | None:
        return self._read("""select id, user_id, invoice_id, provider_order_id, provider_payment_id,
                amount, currency, event_status from public.billing_events
            where id=%s and event_type='payment.capture'""", (captureId,))

    def act(self, captureId, request, keyHash, fingerprint, admin, evidence, providerReason) -> dict:
        from api.services.billing.manualBillingPresentation import serializeFinalizationResult
        billing = self.billing

        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("select user_id from public.billing_events where id=%s and event_type='payment.capture'",
                               (captureId,))
                identity = cursor.fetchone()
                if not identity:
                    raise PaymentCaseNotFound()
                owner = identity["user_id"]
                billing._lockUser(cursor, owner or "payment-case:" + captureId)
                cursor.execute("select metadata_json from public.billing_events where idempotency_key=%s",
                               (_actionKey(admin.adminId, keyHash),))
                prior = cursor.fetchone()
                if prior:
                    stored = billing._json(prior["metadata_json"])
                    if stored.get("fingerprint") != fingerprint:
                        raise PaymentCaseConflict("IDEMPOTENCY_KEY_REUSED")
                    return stored["result"]
                cursor.execute("select * from public.billing_events where id=%s for update", (captureId,))
                capture = cursor.fetchone()
                cursor.execute('select "userId" from public."Invoices" where id=%s', (capture["invoice_id"],))
                invoice = cursor.fetchone()
                if owner is not None and (not invoice or invoice["userId"] != owner):
                    raise PaymentCaseConflict("PAYMENT_CASE_MISBOUND")
                finalization, reasonCode, changed = None, None, []
                if request.action == "recheck":
                    if str(capture.get("event_status") or "").upper() == "FINALIZED":
                        raise PaymentCaseConflict("PAYMENT_CASE_ALREADY_FINALIZED")
                    cursor.execute('select "isBanned" as banned from public."Users" where "userId"=%s', (owner,))
                    user = cursor.fetchone()
                    if owner is None:
                        reasonCode = "ERASED_ACCOUNT"
                    elif not user or user["banned"]:
                        reasonCode = "ACCOUNT_RESTRICTED"
                    elif providerReason or evidence is None:
                        reasonCode = providerReason or "PROVIDER_EVIDENCE_MISMATCH"
                    else:
                        result, reasonCode = billing.reconsiderCaptureLocked(cursor, capture, evidence)
                        if result is not None and result.finalized:
                            finalization = serializeFinalizationResult(result)
                            changed = ["event_status", "invoice.status", "invoice.period", "coverage"]
                cursor.execute("select event_status, failure_reason from public.billing_events where id=%s", (captureId,))
                current = cursor.fetchone()
                financialStatus = "FINALIZED" if str(current["event_status"] or "").upper() == "FINALIZED" else "OPEN"
                if request.action == "note":
                    outcome, reasonCode = "NOTE_RECORDED", current["failure_reason"]
                else:
                    outcome = "FINALIZED_ORIGINAL" if financialStatus == "FINALIZED" else "STILL_UNRESOLVED"
                    reasonCode = None if outcome == "FINALIZED_ORIGINAL" else (reasonCode or current["failure_reason"])
                cursor.execute("select clock_timestamp() as now")
                from api.services.billing.manualBillingRepository import _utc
                now = _utc(cursor.fetchone()["now"])
                actionId = str(uuid.uuid4())
                result = {
                    "actionId": actionId, "caseId": captureId, "action": request.action,
                    "userId": owner, "invoiceId": str(capture["invoice_id"]) if capture.get("invoice_id") else None,
                    "financialStatus": financialStatus, "actionOutcome": outcome, "reasonCode": reasonCode,
                    "caseReference": request.caseReference, "finalization": finalization,
                    "recordedAt": now.isoformat(),
                }
                cursor.execute("""insert into public.billing_events(id,user_id,subscription_id,invoice_id,
                        event_category,event_type,event_status,idempotency_key,metadata_json,occurred_at)
                    values(%s,%s,%s,%s,'audit',%s,'processed',%s,%s,%s)""",
                    (actionId, owner, capture.get("subscription_id"), capture.get("invoice_id"), ACTION_EVENT,
                     _actionKey(admin.adminId, keyHash),
                     Json({"fingerprint": fingerprint, "adminId": admin.adminId, "reason": request.reason,
                           "result": result}), now))
                cursor.execute("""insert into public.admin_audit_log
                        (id, admin_id, admin_email, session_id, actor_type, action,
                         target_type, target_id, changed_fields, details, outcome, created_at)
                    values (%s, %s, %s, %s, 'admin', %s, 'payment_capture', %s, %s, %s, %s, %s)""",
                    (str(uuid.uuid4()), admin.adminId, admin.email, admin.sessionId,
                     "billing.payment_case." + request.action, captureId, Json(changed),
                     Json({"actionId": actionId, "caseReference": request.caseReference,
                           "reason": request.reason, "userId": owner, "invoiceId": result["invoiceId"],
                           "financialStatus": financialStatus, "reasonCode": reasonCode}),
                     outcome, now))
                return result

        return billing._run(operation)


class AdminPaymentCaseService:
    def __init__(self, repository=None, provider=None, now=None):
        self._repository = repository
        self._provider = provider
        self._cases = None
        self.now = now or (lambda: datetime.now(timezone.utc))

    @property
    def cases(self) -> AdminPaymentCaseRepository:
        if self._cases is None:
            repository = self._repository
            if repository is None:
                from api.services.billing.manualBillingRepository import getManualBillingRepository
                repository = getManualBillingRepository()
            self._cases = AdminPaymentCaseRepository(repository)
        return self._cases

    @property
    def provider(self):
        if self._provider is None:
            from api.services.subscriptions.subscriptionService import subscriptionService
            self._provider = subscriptionService.razorpayClient
        return self._provider

    def act(self, captureId: str, request: AdminPaymentCaseActionRequest,
            idempotencyKey: str, admin: AdminContext) -> dict:
        key = _validIdempotencyKey(idempotencyKey)
        try:
            captureId = str(uuid.UUID(str(captureId)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AdminApiError(404, "Payment case not found") from exc
        keyHash = _sha256(key)
        fingerprint = _sha256(json.dumps({"caseId": captureId, "action": request.action,
            "caseReference": request.caseReference, "reason": request.reason}, sort_keys=True))
        try:
            stored = self.cases.storedAction(admin.adminId, keyHash)
        except Exception as exc:
            logger.error("Payment case action lookup failed: {}", type(exc).__name__)
            raise AdminApiError(503, _UNAVAILABLE) from exc
        if stored is not None:
            if stored.get("fingerprint") != fingerprint:
                raise AdminApiError(409, "Idempotency key is already in use")
            return stored["result"]
        evidence, providerReason = None, None
        if request.action == "recheck":
            evidence, providerReason = self._providerEvidence(captureId)
        try:
            return self.cases.act(captureId, request, keyHash, fingerprint, admin, evidence, providerReason)
        except PaymentCaseNotFound as exc:
            raise AdminApiError(404, "Payment case not found") from exc
        except PaymentCaseConflict as exc:
            if exc.code == "IDEMPOTENCY_KEY_REUSED":
                raise AdminApiError(409, "Idempotency key is already in use") from exc
            raise AdminApiError(409, "Payment case action was not applied", {"reasonCode": exc.code}) from exc
        except Exception as exc:
            logger.error("Payment case action failed for case={}: {}", captureId, type(exc).__name__)
            raise AdminApiError(503, _UNAVAILABLE) from exc

    def _providerEvidence(self, captureId: str):
        """Fresh provider evidence read before the owner-locked transaction."""
        try:
            case = self.cases.caseForAction(captureId)
        except Exception as exc:
            logger.error("Payment case lookup failed: {}", type(exc).__name__)
            raise AdminApiError(503, _UNAVAILABLE) from exc
        if case is None:
            raise AdminApiError(404, "Payment case not found")
        if case["user_id"] is None or str(case.get("event_status") or "").upper() == "FINALIZED":
            return None, None
        try:
            attempt = self.cases.billing.attemptForOrder(case["provider_order_id"])
        except ValueError:
            return None, "PROVIDER_EVIDENCE_MISMATCH"
        except Exception as exc:
            logger.error("Payment case attempt lookup failed: {}", type(exc).__name__)
            raise AdminApiError(503, _UNAVAILABLE) from exc
        try:
            payment = self.provider.payment.fetch(case["provider_payment_id"])
        except Exception as exc:
            logger.error("Payment case provider read failed: {}", type(exc).__name__)
            raise AdminApiError(503, _UNAVAILABLE) from exc
        if (not isinstance(payment, dict) or payment.get("id") != case["provider_payment_id"]
                or payment.get("order_id") != case["provider_order_id"]
                or int(payment.get("amount") or -1) != int(case["amount"] or 0)
                or str(payment.get("currency") or "").upper() != str(case["currency"] or "").upper()
                or str(payment.get("status") or "").lower() != "captured"):
            return None, "PROVIDER_EVIDENCE_MISMATCH"
        from api.services.billing.manualPaymentService import ManualPaymentService
        frozen = self.cases.billing._json(attempt.get("metadata_json")).get("manualBilling", {})
        # The recheck's own observation time proves nothing about the original deadline.
        evidence = ManualPaymentService.buildPaymentEvidence(
            payment, attemptId=str(attempt["id"]), invoiceId=str(case["invoice_id"]), userId=case["user_id"],
            purpose=frozen.get("purpose") or "", observedAt=self.now(), serverObservedCapture=False)
        return evidence, None


_adminPaymentCaseService: AdminPaymentCaseService | None = None


def getAdminPaymentCaseService() -> AdminPaymentCaseService:
    global _adminPaymentCaseService
    if _adminPaymentCaseService is None:
        _adminPaymentCaseService = AdminPaymentCaseService()
    return _adminPaymentCaseService
