"""Audited staff payment-case actions through the real repository transaction.

SQLite executes the production queries; PostgreSQL rollback and two-session
races live in test_gap_remediation_postgres.py. Provider fakes here prove code
paths only, never live Razorpay behaviour.
"""
import json
import uuid
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import pytest

from test.test_admin_auth_service import authFixture, passwordHasher  # noqa: F401
from test.test_admin_credit_resets import ADMIN, OTHER_ADMIN, ADMIN_TABLES, rows
from test.test_billing_notification_revisions import deliveries  # noqa: F401
from test.test_manual_billing_runtime import NOW, USER, database, read_row, seed_payment, sqlTransaction


REASON = "Customer emailed a bank statement for this payment"
CASE_REF = "SUP-1042"
CAPTURED_AT = NOW + timedelta(minutes=10)


class FakePayments:
    def __init__(self, entity):
        self.entity = entity
        self.calls = []

    def fetch(self, paymentId):
        self.calls.append(paymentId)
        if isinstance(self.entity, Exception):
            raise self.entity
        return dict(self.entity)


class FakeProvider:
    def __init__(self, entity):
        self.payment = FakePayments(entity)


@pytest.fixture
def cases(database):
    repository, path = database
    with sqlTransaction(path) as connection:
        connection.executescript(ADMIN_TABLES)
    evidence = replace(seed_payment(database), observedAt=NOW + timedelta(hours=1), provenCaptureAt=None, timingVerified=False)
    result = repository.finalizeCapturedPayment(evidence)
    assert result.state == "requires_reconciliation"
    captureId = str(result.anomalyId)
    entity = {"id": evidence.providerPaymentId, "order_id": evidence.providerOrderId, "amount": evidence.amount,
              "currency": evidence.currency, "status": "captured", "captured_at": int(CAPTURED_AT.timestamp())}
    return repository, path, captureId, evidence, entity


@pytest.fixture
def reports(deliveries):
    with sqlTransaction(deliveries[1]) as connection:
        connection.execute("ALTER TABLE notification_deliveries ADD COLUMN created_at TEXT")


CLOCK = NOW + timedelta(hours=2)


def service(cases, entity=None, clock=CLOCK):
    from api.services.adminPaymentCaseService import AdminPaymentCaseService
    repository, _, _, _, default = cases
    return AdminPaymentCaseService(repository=repository, provider=FakeProvider(default if entity is None else entity),
                                   now=lambda: clock)


def body(action="recheck", **overrides):
    return {"action": action, "caseReference": CASE_REF, "reason": REASON, **overrides}


def act(cases, action="recheck", key="case-key-1", admin=ADMIN, entity=None, svc=None, clock=CLOCK):
    """Service and database clocks are pinned so outcomes never depend on the run date."""
    from api.adminModels import AdminPaymentCaseActionRequest
    svc = svc or service(cases, entity, clock)
    with patch("api.services.billing.manualBillingRepository._now", return_value=clock):
        return svc.act(cases[2], AdminPaymentCaseActionRequest(**body(action)), key, admin)


def capture(cases):
    return rows(cases[1], "SELECT * FROM billing_events WHERE id=?", (cases[2],))[0]


def actions(path):
    return rows(path, "SELECT * FROM billing_events WHERE event_type='admin.payment_case.action'")


def audits(path):
    return rows(path, "SELECT * FROM admin_audit_log ORDER BY created_at, id")


def test_note_records_audited_investigation_and_keeps_money_open(cases):
    _, path, captureId, _, _ = cases
    svc = service(cases)
    result = act(cases, "note", svc=svc)
    assert (result["financialStatus"], result["actionOutcome"], result["reasonCode"]) == (
        "OPEN", "NOTE_RECORDED", "CAPTURE_OUTSIDE_WINDOW")
    assert result["caseId"] == captureId and result["userId"] == USER and result["finalization"] is None
    assert capture(cases)["event_status"] == "REQUIRES_RECONCILIATION"
    assert read_row(path, "Invoices")["status"] == "PAYMENT_PENDING"
    assert svc.provider.payment.calls == []
    [audit] = audits(path)
    assert (audit["action"], audit["target_type"], audit["target_id"], audit["outcome"]) == (
        "billing.payment_case.note", "payment_capture", captureId, "NOTE_RECORDED")
    assert audit["admin_id"] == ADMIN.adminId and audit["session_id"] == ADMIN.sessionId
    details = json.loads(audit["details"])
    assert details["caseReference"] == CASE_REF and details["reason"] == REASON
    assert details["actionId"] == result["actionId"] and json.loads(audit["changed_fields"]) == []
    assert len(actions(path)) == 1


def test_recheck_with_attested_capture_time_finalizes_the_original_purchase(cases):
    _, path, captureId, evidence, _ = cases
    result = act(cases)
    assert (result["financialStatus"], result["actionOutcome"], result["reasonCode"]) == (
        "FINALIZED", "FINALIZED_ORIGINAL", None)
    assert result["finalization"]["finalized"] and result["finalization"]["state"] == "activated"
    invoice = read_row(path, "Invoices")
    assert invoice["status"] == "PAID" and invoice["razorpayPaymentId"] == evidence.providerPaymentId
    assert invoice["period_start"] == CAPTURED_AT.isoformat()
    assert capture(cases)["event_status"] == "FINALIZED"
    receipts = rows(path, "SELECT metadata_json FROM billing_events WHERE event_type='email.billing_intent.committed'")
    assert [json.loads(row["metadata_json"])["notificationType"] for row in receipts] == ["payment_receipt"]
    [audit] = audits(path)
    assert audit["action"] == "billing.payment_case.recheck" and audit["outcome"] == "FINALIZED_ORIGINAL"
    assert "event_status" in json.loads(audit["changed_fields"])


def test_recheck_never_finalizes_an_original_interval_that_has_already_elapsed(cases):
    _, path, _, _, _ = cases
    before = read_row(path, "subscriptions")
    result = act(cases, clock=NOW + timedelta(days=40))
    assert (result["financialStatus"], result["actionOutcome"], result["reasonCode"]) == (
        "OPEN", "STILL_UNRESOLVED", "ORIGINAL_INTERVAL_ELAPSED")
    assert read_row(path, "Invoices")["status"] == "PAYMENT_PENDING"
    assert capture(cases)["event_status"] == "REQUIRES_RECONCILIATION"
    after = read_row(path, "subscriptions")
    assert (after["current_period_start"], after["current_period_end"], after["billing_state"]) == (
        before["current_period_start"], before["current_period_end"], before["billing_state"])
    receipts = rows(path, "SELECT id FROM billing_events WHERE event_type='email.billing_intent.committed'")
    assert receipts == []


def test_finalized_case_leaves_reports_and_later_channels_stay_idempotent(cases, reports):
    from api.services.billing.manualObligationReport import listObligations
    repository, path, captureId, evidence, _ = cases
    assert captureId in {item["id"] for item in listObligations(repository)["items"]}
    act(cases)
    assert captureId not in {item["id"] for item in listObligations(repository)["items"]}
    assert repository.finalizeCapturedPayment(evidence).state == "already_finalized"
    assert len(rows(path, "SELECT id FROM billing_events WHERE event_type='payment.capture'")) == 1


def test_noted_case_stays_in_operator_reports(cases, reports):
    from api.services.billing.manualObligationReport import listObligations
    repository, _, captureId, _, _ = cases
    act(cases, "note")
    assert captureId in {item["id"] for item in listObligations(repository)["items"]}


def test_recheck_without_attested_capture_time_stays_unresolved(cases):
    _, path, _, _, entity = cases
    entity = {key: value for key, value in entity.items() if key != "captured_at"}
    result = act(cases, entity=entity)
    assert (result["financialStatus"], result["actionOutcome"], result["reasonCode"]) == (
        "OPEN", "STILL_UNRESOLVED", "CAPTURE_TIMING_UNVERIFIED")
    assert read_row(path, "Invoices")["status"] == "PAYMENT_PENDING"
    assert capture(cases)["event_status"] == "REQUIRES_RECONCILIATION"
    assert audits(path)[0]["outcome"] == "STILL_UNRESOLVED"


@pytest.mark.parametrize("field,value", [
    ("amount", 2999), ("currency", "USD"), ("order_id", "order-other"), ("id", "pay-other"), ("status", "refunded"),
])
def test_recheck_rejects_mismatched_provider_evidence(cases, field, value):
    _, path, _, _, entity = cases
    result = act(cases, entity={**entity, field: value})
    assert (result["actionOutcome"], result["reasonCode"]) == ("STILL_UNRESOLVED", "PROVIDER_EVIDENCE_MISMATCH")
    assert read_row(path, "Invoices")["status"] == "PAYMENT_PENDING"
    assert capture(cases)["event_status"] == "REQUIRES_RECONCILIATION"


def test_attested_time_still_outside_window_stays_unresolved_and_is_reconsidered_once(cases):
    _, path, _, _, entity = cases
    late = {**entity, "captured_at": int((NOW + timedelta(hours=2)).timestamp())}
    first = act(cases, entity=late)
    assert (first["actionOutcome"], first["reasonCode"]) == ("STILL_UNRESOLVED", "CAPTURE_OUTSIDE_WINDOW")
    second = act(cases, key="case-key-2", entity=entity)
    assert (second["actionOutcome"], second["reasonCode"]) == ("STILL_UNRESOLVED", "RECHECK_ALREADY_APPLIED")
    assert read_row(path, "Invoices")["status"] == "PAYMENT_PENDING"


def test_banned_account_recheck_stays_unresolved(cases):
    _, path, _, _, _ = cases
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE "Users" SET "isBanned"=1')
    result = act(cases)
    assert (result["actionOutcome"], result["reasonCode"]) == ("STILL_UNRESOLVED", "ACCOUNT_RESTRICTED")
    assert read_row(path, "Invoices")["status"] == "PAYMENT_PENDING"


def test_same_key_replay_returns_stored_result_without_provider_call(cases):
    _, path, _, _, _ = cases
    svc = service(cases)
    first = act(cases, svc=svc)
    again = act(cases, svc=svc)
    assert again == first and first["actionOutcome"] == "FINALIZED_ORIGINAL"
    assert len(svc.provider.payment.calls) == 1
    assert len(audits(path)) == 1 and len(actions(path)) == 1


def test_key_payload_conflict_is_409(cases):
    from api.adminErrors import AdminApiError
    act(cases, "note")
    with pytest.raises(AdminApiError) as error:
        act(cases, "recheck")
    assert error.value.statusCode == 409
    assert len(actions(cases[1])) == 1


def test_idempotency_key_is_scoped_to_the_admin(cases):
    first = act(cases, "note")
    other = act(cases, "note", admin=OTHER_ADMIN)
    assert first["actionId"] != other["actionId"]


def test_recheck_of_finalized_case_is_409(cases):
    from api.adminErrors import AdminApiError
    act(cases)
    with pytest.raises(AdminApiError) as error:
        act(cases, key="case-key-2")
    assert error.value.statusCode == 409


def test_provider_outage_is_503_without_action(cases):
    from api.adminErrors import AdminApiError
    with pytest.raises(AdminApiError) as error:
        act(cases, entity=RuntimeError("provider down"))
    assert error.value.statusCode == 503
    assert actions(cases[1]) == [] and audits(cases[1]) == []


def test_audit_failure_rolls_back_the_original_finalization(cases):
    from api.adminErrors import AdminApiError
    _, path, _, _, _ = cases
    with sqlTransaction(path) as connection:
        connection.execute("DROP TABLE admin_audit_log")
    with pytest.raises(AdminApiError) as error:
        act(cases)
    assert error.value.statusCode == 503
    assert read_row(path, "Invoices")["status"] == "PAYMENT_PENDING"
    assert capture(cases)["event_status"] == "REQUIRES_RECONCILIATION"
    assert actions(path) == []


def test_unknown_and_non_capture_cases_are_404(cases):
    from api.adminErrors import AdminApiError
    from api.adminModels import AdminPaymentCaseActionRequest
    _, path, _, evidence, _ = cases
    svc = service(cases)
    for caseId in (str(uuid.uuid4()), "not-a-uuid", evidence.attemptId):
        with pytest.raises(AdminApiError) as error:
            svc.act(caseId, AdminPaymentCaseActionRequest(**body("note")), "k", ADMIN)
        assert error.value.statusCode == 404
    assert actions(path) == []


def test_misbound_case_is_409(cases):
    from api.adminErrors import AdminApiError
    _, path, _, _, _ = cases
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE "Invoices" SET "userId"=?', ("someone-else",))
    with pytest.raises(AdminApiError) as error:
        act(cases, "note")
    assert error.value.statusCode == 409
    assert actions(path) == []


def test_mark_investigated_cannot_clear_received_money():
    from api.services.billing.reconciliationService import ReconciliationService
    from utils.exceptionHandler import CustomException

    class Query:
        def __init__(self, data):
            self.data, self.updates = data, []
        def __getattr__(self, name):
            return lambda *args, **kwargs: self
        def update(self, values):
            self.updates.append(values)
            return self
        def execute(self):
            return type("Result", (), {"data": self.data})()

    for status in ("authorized", "captured"):
        query = Query([{"id": "attempt", "payment_status": status, "user_id": USER}])
        reconciliation = ReconciliationService(repository=object())
        reconciliation.client = type("Client", (), {"table": lambda self, name: query})()
        with pytest.raises(CustomException) as error:
            reconciliation.markInvestigated("payment_attempt", "attempt", "billing-admin", "looked at it")
        assert (error.value.statusCode, error.value.errorCode) == (409, "PAYMENT_CASE_ACTION_REQUIRED")
        assert query.updates == []


# -- HTTP contract through the production app -----------------------------------

@pytest.fixture
def api(cases, authFixture):
    from fastapi.testclient import TestClient
    from api.services.adminAuthService import getAdminAuthService
    from api.services.adminPaymentCaseService import getAdminPaymentCaseService
    from main import app as productionApp
    from test.test_admin_auth_service import VALID_PASSWORD
    svc = service(cases)
    productionApp.dependency_overrides[getAdminAuthService] = lambda: authFixture.service
    productionApp.dependency_overrides[getAdminPaymentCaseService] = lambda: svc
    login = authFixture.service.login("admin@example.com", VALID_PASSWORD, "203.0.113.10")
    client = TestClient(productionApp, raise_server_exceptions=False)
    clock = patch("api.services.billing.manualBillingRepository._now", return_value=CLOCK)
    clock.start()
    try:
        yield {"client": client, "token": login["token"], "adminId": login["admin"]["id"], "service": svc}
    finally:
        clock.stop()
        client.close()
        productionApp.dependency_overrides.clear()


def post(api, cases, payload=None, key="http-key-1", token=None, caseId=None):
    headers = {"Authorization": "Bearer " + (token or api["token"])}
    if key is not None:
        headers["Idempotency-Key"] = key
    return api["client"].post(f"/admin/billing/payment-cases/{caseId or cases[2]}/actions",
                              json=body() if payload is None else payload, headers=headers)


def test_genuine_admin_rechecks_over_http(api, cases):
    response = post(api, cases)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["actionOutcome"] == "FINALIZED_ORIGINAL" and data["caseId"] == cases[2]
    assert audits(cases[1])[0]["admin_id"] == api["adminId"]
    assert api["token"] not in response.text


def test_user_and_billing_admin_tokens_are_rejected(api, cases, monkeypatch):
    import os
    from jose import jwt
    monkeypatch.setenv("BILLING_ADMIN_USER_IDS", USER)
    userToken = jwt.encode({"userId": USER, "email": "user@example.com"}, os.environ["SECRET_KEY"], algorithm="HS256")
    for token in (userToken, "not-a-token"):
        assert post(api, cases, token=token).status_code == 401
    assert actions(cases[1]) == []


@pytest.mark.parametrize("payload,key", [
    (body(reason="   "), "k-1"), (body(reason="x" * 2001), "k-1"), (body(caseReference=" "), "k-1"),
    (body(caseReference="c" * 129), "k-1"), (body(action="refund"), "k-1"), (body(action="override"), "k-1"),
    (body(amount=3000), "k-1"), (body(adminId="someone"), "k-1"), (body(credits=10), "k-1"),
    (body(capturedAt="2026-10-06T12:00:00+00:00"), "k-1"), ({"action": "note"}, "k-1"),
    (body(), None), (body(), "   "), (body(), "k" * 129),
])
def test_invalid_requests_are_422_without_action(api, cases, payload, key):
    assert post(api, cases, payload=payload, key=key).status_code == 422
    assert actions(cases[1]) == [] and api["service"].provider.payment.calls == []


def test_http_status_mapping(api, cases):
    assert post(api, cases, caseId=str(uuid.uuid4())).status_code == 404
    assert post(api, cases, payload=body("note"), key="k-2").status_code == 200
    assert post(api, cases, payload=body("recheck"), key="k-2").status_code == 409
