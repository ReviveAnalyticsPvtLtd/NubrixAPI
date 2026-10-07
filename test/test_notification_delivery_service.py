from datetime import datetime, timezone

from api.services.notifications.edgeEmailClient import EdgeSendResult
from api.services.notifications.notificationDeliveryService import (
    NotificationDeliveryService,
)


NOW = datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc)


def _delivery(deliveryId="11111111-1111-4111-8111-111111111111", **overrides):
    row = {
        "id": deliveryId,
        "notification_type": "trial_expiry_warning",
        "template_version": "1",
        "user_id": "user-1",
        "subscription_id": "22222222-2222-4222-8222-222222222222",
        "period_end": "2026-09-19T01:00:00+00:00",
        "attempt_count": 1,
        "payload_version": 1,
        "metadata_json": {"trialStartDate": "2026-09-07T01:00:00+00:00"},
    }
    row.update(overrides)
    return row


def _subscription(**overrides):
    row = {
        "id": "22222222-2222-4222-8222-222222222222",
        "user_id": "user-1",
        "status": "trial",
        "billing_mode": "none",
        "erasure_pending": False,
        "current_period_start": "2026-09-07T01:00:00+00:00",
        "current_period_end": "2026-09-19T01:00:00+00:00",
    }
    row.update(overrides)
    return row


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeQuery:
    def __init__(self, rows):
        self.rows = list(rows)
        self.filters = []

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def limit(self, _value):
        return self

    def execute(self):
        rows = self.rows
        for field, value in self.filters:
            rows = [row for row in rows if row.get(field) == value]
        return FakeResult(rows)


class FakeSupabase:
    def __init__(self, *, subscriptions=None, users=None):
        self.rows = {
            "subscriptions": subscriptions or [],
            "Users": users or [],
        }

    def table(self, name):
        return FakeQuery(self.rows[name])


class FakeRepository:
    def __init__(self, rows):
        self.rows = list(rows)
        self.claimCalls = 0
        self.claimArguments = []
        self.accepted = []
        self.retries = []
        self.terminals = []
        self.reconciliation = []
        self.ambiguous = []
        self.advanced = []
        self.recovered = []
        self.recoveredClaims = []

    def claimDue(self, workerId, limit=50, leaseSeconds=300):
        self.claimCalls += 1
        self.claimArguments.append((workerId, limit, leaseSeconds))
        if not self.rows:
            return []
        claimed = self.rows[:limit]
        self.rows = self.rows[limit:]
        return claimed

    authorization = "AUTHORIZED"

    def authorizeBillingSubmissionResult(self, *args):
        return self.authorization

    def markAccepted(self, *args, **kwargs):
        self.accepted.append(args)
        return True

    def scheduleRetry(self, *args, **kwargs):
        self.retries.append(args)
        return True

    def markTerminal(self, *args, **kwargs):
        self.terminals.append((args, kwargs))
        return True

    def listForReconciliation(self, limit=100):
        return self.reconciliation[:limit]

    def listAmbiguous(self, limit=100):
        return self.ambiguous[:limit]

    def advanceReconciliation(self, *args):
        self.advanced.append(args)
        return True

    def attachRecoveredMessageId(self, *args):
        self.recovered.append(args)
        return True

    def recoverClaimedAmbiguous(self, *args):
        self.recoveredClaims.append(args)
        return True


class FakeEdgeClient:
    def __init__(self, results, validationError=None):
        self.results = list(results)
        self.validationError = validationError
        self.validateCalls = 0
        self.payloads = []

    def validate(self):
        self.validateCalls += 1
        if self.validationError is not None:
            raise self.validationError

    def sendTrialExpiry(self, payload):
        self.payloads.append(payload)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def sendBilling(self, payload):
        return self.sendTrialExpiry(payload)


class FakeLedger:
    def __init__(self):
        self.events = []

    def log_event(self, **kwargs):
        self.events.append(kwargs)


class FakeBrevoClient:
    def __init__(self, *, byMessage=None, byTag=None):
        self.byMessage = byMessage or {}
        self.byTag = byTag or {}
        self.messageCalls = []
        self.tagCalls = []

    def findByMessageId(self, messageId):
        self.messageCalls.append(messageId)
        return self.byMessage.get(messageId)

    def findByTag(self, tag):
        self.tagCalls.append(tag)
        return self.byTag.get(tag)


def _service(
    repository,
    edgeClient,
    *,
    users=None,
    subscriptions=None,
    ledger=None,
    brevoClient=None,
):
    return NotificationDeliveryService(
        repository=repository,
        edgeClient=edgeClient,
        supabaseClient=FakeSupabase(
            subscriptions=(
                [_subscription()] if subscriptions is None else subscriptions
            ),
            users=([{
                "userId": "user-1",
                "email": "recipient@example.test",
                "fullName": "Recipient",
            }] if users is None else users),
        ),
        eventService=ledger or FakeLedger(),
        brevoClient=brevoClient,
        now=lambda: NOW,
    )


def test_globalValidationOccursBeforeClaimingRows():
    repository = FakeRepository([_delivery()])
    edge = FakeEdgeClient([], validationError=RuntimeError("EDGE_VALIDATION_FAILED"))

    try:
        _service(repository, edge).dispatchBatch("worker-a")
        raise AssertionError("expected validation failure")
    except RuntimeError as error:
        assert str(error) == "EDGE_VALIDATION_FAILED"

    assert repository.claimCalls == 0


def test_heldBillingSolicitationIsRescheduledNotCancelled():
    delivery = _delivery(notification_type="monthly_subscription_expired",
        period_end="2026-09-16T01:00:00+00:00", metadata_json={})
    repository = FakeRepository([delivery])
    repository.authorization = "HELD"
    edge = FakeEdgeClient([])
    service = _service(repository, edge, subscriptions=[_subscription(status="expired",
        billing_mode="monthly_prepaid", current_period_end="2026-09-16T01:00:00+00:00")])
    result = service.dispatchBatch("worker-a")
    assert result["cancelled"] == 0 and repository.terminals == []
    assert edge.payloads == []
    assert len(repository.retries) == 1 and repository.retries[0][2] == "PAYMENT_HOLD"


def test_committedBillingReceiptDispatchesEvenAfterSubscriptionExpires():
    delivery = _delivery(notification_type="payment_receipt", metadata_json={"paymentId":"pay_123","amount":3000,"currency":"INR"})
    repository = FakeRepository([delivery])
    edge = FakeEdgeClient([EdgeSendResult(outcome="ACCEPTED", messageId="receipt-message")])
    service = _service(repository, edge, subscriptions=[_subscription(status="expired",billing_mode="monthly_prepaid")])
    result = service.dispatchBatch("worker-a")
    assert result["accepted"] == 1 and result["cancelled"] == 0
    assert edge.payloads[0]["notificationType"] == "payment_receipt"
    assert edge.payloads[0]["metadata"]["paymentId"] == "pay_123"


def test_acceptedResponsePersistsMessageIdAndSafeAudit():
    repository = FakeRepository([_delivery()])
    edge = FakeEdgeClient([
        EdgeSendResult(outcome="ACCEPTED", messageId="message-1")
    ])
    ledger = FakeLedger()

    summary = _service(repository, edge, ledger=ledger).dispatchBatch("worker-a")

    assert summary["accepted"] == 1
    assert repository.accepted == [(
        "11111111-1111-4111-8111-111111111111",
        "worker-a",
        "message-1",
        NOW.isoformat(),
    )]
    assert edge.payloads[0]["trackingTag"].startswith("nubrix_delivery:")
    assert all("email" not in event["metadata"] for event in ledger.events)
    assert [event["event_type"] for event in ledger.events] == [
        "email.expiry_warning.sending",
        "email.expiry_warning.accepted",
    ]


def test_transientFailureSchedulesExactFirstBackoff():
    repository = FakeRepository([_delivery(attempt_count=1)])
    edge = FakeEdgeClient([
        EdgeSendResult(outcome="RETRYABLE", errorCode="EDGE_HTTP_503")
    ])

    summary = _service(repository, edge).dispatchBatch("worker-a")

    assert summary["retryScheduled"] == 1
    assert repository.retries[0] == (
        "11111111-1111-4111-8111-111111111111",
        "worker-a",
        "EDGE_HTTP_503",
        "2026-09-17T01:05:00+00:00",
        None,
    )


def test_ambiguousFailurePollsBeforeThirtyMinuteResend():
    repository = FakeRepository([_delivery(attempt_count=1)])
    edge = FakeEdgeClient([
        EdgeSendResult(outcome="AMBIGUOUS", errorCode="AMBIGUOUS_SEND")
    ])

    summary = _service(repository, edge).dispatchBatch("worker-a")

    assert summary["ambiguous"] == 1
    assert repository.retries[0] == (
        "11111111-1111-4111-8111-111111111111",
        "worker-a",
        "AMBIGUOUS_SEND",
        "2026-09-17T01:30:00+00:00",
        "2026-09-17T01:05:00+00:00",
    )


def test_missingUserCancelsWithoutCallingEdgeFunction():
    repository = FakeRepository([_delivery()])
    edge = FakeEdgeClient([])

    summary = _service(repository, edge, users=[]).dispatchBatch("worker-a")

    assert summary["cancelled"] == 1
    assert edge.payloads == []
    args, kwargs = repository.terminals[0]
    assert args[:2] == (
        "11111111-1111-4111-8111-111111111111",
        "CANCELLED",
    )
    assert kwargs["errorCode"] == "USER_NOT_FOUND"


def test_seventhClaimIsTerminalizedWithoutAnotherPhysicalSend():
    repository = FakeRepository([_delivery(attempt_count=7)])
    edge = FakeEdgeClient([])

    summary = _service(repository, edge).dispatchBatch("worker-a")

    assert edge.payloads == []
    assert summary["failed"] == 1
    args, kwargs = repository.terminals[0]
    assert args[:2] == (
        "11111111-1111-4111-8111-111111111111",
        "FAILED",
    )
    assert kwargs["errorCode"] == "RETRY_ATTEMPTS_EXHAUSTED"


def test_oneUnexpectedRowFailureDoesNotStopLaterRows():
    repository = FakeRepository([
        _delivery("11111111-1111-4111-8111-111111111111"),
        _delivery("33333333-3333-4333-8333-333333333333"),
    ])
    edge = FakeEdgeClient([
        RuntimeError("private@example.test"),
        EdgeSendResult(outcome="ACCEPTED", messageId="message-2"),
    ])

    summary = _service(repository, edge).dispatchBatch("worker-a")

    assert summary["errors"] == 1
    assert summary["accepted"] == 1
    assert len(repository.retries) == 1


def test_dispatch_claims_one_row_immediately_before_processing():
    repository = FakeRepository([
        _delivery("11111111-1111-4111-8111-111111111111"),
        _delivery("33333333-3333-4333-8333-333333333333"),
    ])
    edge = FakeEdgeClient([
        EdgeSendResult(outcome="ACCEPTED", messageId="message-1"),
        EdgeSendResult(outcome="ACCEPTED", messageId="message-2"),
    ])

    summary = _service(repository, edge).dispatchBatch("worker-a", limit=2)

    assert summary["claimed"] == 2
    assert repository.claimArguments == [
        ("worker-a", 1, 300),
        ("worker-a", 1, 300),
    ]


def test_ambiguous_retry_performs_final_tag_lookup_before_resend():
    from api.services.notifications.brevoEventClient import ProviderEvent

    delivery = _delivery(last_error_code="AMBIGUOUS_SEND", attempt_count=2)
    repository = FakeRepository([delivery])
    edge = FakeEdgeClient([])
    tag = f"nubrix_delivery:{delivery['id']}"
    brevo = FakeBrevoClient(byTag={
        tag: ProviderEvent(
            messageId="recovered-message",
            providerStatus="delivered",
            occurredAt="2026-09-17T01:01:00+00:00",
            terminalStatus="DELIVERED",
            errorCode=None,
        )
    })

    summary = _service(
        repository,
        edge,
        brevoClient=brevo,
    ).dispatchBatch("worker-a")

    assert edge.payloads == []
    assert repository.recoveredClaims == [(
        delivery["id"],
        "worker-a",
        "recovered-message",
        NOW.isoformat(),
    )]
    assert summary["accepted"] == 1
    assert summary["recovered"] == 1


def test_reconciliationMarksAcceptedDeliveryDelivered():
    from api.services.notifications.brevoEventClient import ProviderEvent

    delivery = _delivery(
        status="ACCEPTED",
        provider_message_id="message-1",
        accepted_at="2026-09-17T00:55:00+00:00",
    )
    repository = FakeRepository([])
    repository.reconciliation = [delivery]
    brevo = FakeBrevoClient(byMessage={
        "message-1": ProviderEvent(
            messageId="message-1",
            providerStatus="delivered",
            occurredAt="2026-09-17T01:02:00+00:00",
            terminalStatus="DELIVERED",
            errorCode=None,
        )
    })

    summary = _service(
        repository,
        FakeEdgeClient([]),
        brevoClient=brevo,
    ).reconcileBatch()

    assert summary["delivered"] == 1
    args, kwargs = repository.terminals[0]
    assert args[:2] == (delivery["id"], "DELIVERED")
    assert kwargs["providerStatus"] == "delivered"
    assert kwargs["deliveredAt"] == "2026-09-17T01:02:00+00:00"


def test_unresolvedAcceptedDeliveryAdvancesPollingWithoutDispatching():
    delivery = _delivery(
        status="ACCEPTED",
        provider_message_id="message-1",
        accepted_at="2026-09-17T00:55:00+00:00",
    )
    repository = FakeRepository([])
    repository.reconciliation = [delivery]

    summary = _service(
        repository,
        FakeEdgeClient([]),
        brevoClient=FakeBrevoClient(),
    ).reconcileBatch()

    assert summary["pending"] == 1
    assert repository.advanced == [(
        delivery["id"],
        "request",
        "2026-09-17T01:05:00+00:00",
        None,
    )]


def test_ambiguousSendRecoversMessageIdBeforeApplyingProviderState():
    from api.services.notifications.brevoEventClient import ProviderEvent

    delivery = _delivery(
        status="RETRY_PENDING",
        last_error_code="AMBIGUOUS_SEND",
        next_attempt_at="2026-09-17T01:30:00+00:00",
    )
    repository = FakeRepository([])
    repository.ambiguous = [delivery]
    tag = f"nubrix_delivery:{delivery['id']}"
    brevo = FakeBrevoClient(byTag={
        tag: ProviderEvent(
            messageId="recovered-message",
            providerStatus="delivered",
            occurredAt="2026-09-17T01:03:00+00:00",
            terminalStatus="DELIVERED",
            errorCode=None,
        )
    })

    summary = _service(
        repository,
        FakeEdgeClient([]),
        brevoClient=brevo,
    ).reconcileBatch()

    assert repository.recovered == [(delivery["id"], "recovered-message")]
    assert summary["recovered"] == 1
    assert summary["delivered"] == 1


def test_acceptedDeliveryTimesOutAfterSeventyTwoHours():
    delivery = _delivery(
        status="ACCEPTED",
        provider_message_id="message-1",
        accepted_at="2026-09-14T00:59:59+00:00",
    )
    repository = FakeRepository([])
    repository.reconciliation = [delivery]

    summary = _service(
        repository,
        FakeEdgeClient([]),
        brevoClient=FakeBrevoClient(),
    ).reconcileBatch()

    assert summary["failed"] == 1
    _args, kwargs = repository.terminals[0]
    assert kwargs["errorCode"] == "DELIVERY_STATUS_TIMEOUT"
