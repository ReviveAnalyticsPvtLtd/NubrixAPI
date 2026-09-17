from test.test_user_erasure_repository import FakeConnection, _repository


def _create_request(connection):
    return _repository(connection).createRequest(
        userId="user-1",
        subjectFingerprint="a" * 64,
        adminId="admin-1",
        idempotencyKey="8cfdb150-417d-47ab-acd1-fef39d2bc14e",
        reason=None,
    )


def test_erasure_start_cancels_unsent_notifications_transactionally():
    connection = FakeConnection()

    _create_request(connection)

    query, parameters = next(
        call
        for call in connection.state["executed"]
        if "update public.notification_deliveries" in call[0]
    )
    assert "when status in ('pending', 'retry_pending', 'sending')" in query
    assert "then 'cancelled'" in query
    assert "user_id = null" in query
    assert "subscription_id = null" in query
    assert "lease_owner = null" in query
    assert parameters == ("user-1",)
    assert connection.commits == 1


def test_erasure_preserves_accepted_provider_state_for_reconciliation():
    connection = FakeConnection()

    _create_request(connection)

    query = next(
        query
        for query, _parameters in connection.state["executed"]
        if "update public.notification_deliveries" in query
    )
    assert "provider_message_id" not in query
    assert "provider_status" not in query
    assert "next_reconcile_at" not in query
    assert "when status in ('pending', 'retry_pending', 'sending')" in query


def test_erasure_tolerates_notification_table_missing_during_rollout():
    connection = FakeConnection(tables=set())

    _create_request(connection)
    _repository(connection).deleteDatabaseData("request-1", "user-1", [])

    assert not any(
        "update public.notification_deliveries" in query
        for query, _parameters in connection.state["executed"]
    )
    assert connection.rollbacks == 0
