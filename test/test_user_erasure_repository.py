import pytest

from api.adminErrors import AdminApiError


class FakeCursor:
    def __init__(self, state):
        self.state = state
        self.current = None
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        normalized = " ".join(str(query).split()).lower()
        self.state["executed"].append((normalized, params))
        self.current = None
        self.rowcount = 0
        if self.state.get("failOn") and self.state["failOn"] in normalized:
            raise RuntimeError("database unavailable")
        if 'from public."users"' in normalized:
            self.current = (
                dict(self.state["userRow"])
                if self.state["userExists"]
                else None
            )
        elif 'update public."users"' in normalized:
            wasBanned = bool(self.state["userRow"].get("isBanned"))
            self.state["userRow"].update({
                "isBanned": True,
                "bannedAt": self.state["userRow"].get("bannedAt") or "now",
                "bannedBy": self.state["userRow"].get("bannedBy") or params[0],
                "banReason": (
                    self.state["userRow"].get("banReason")
                    if wasBanned and params[2] is None
                    else params[1]
                ),
            })
            self.rowcount = 1
        elif 'delete from public."sessions"' in normalized:
            self.rowcount = self.state["sessionCount"]
        elif "where idempotency_key = %s" in normalized:
            self.current = self.state.get("idempotencyRow")
        elif "status <> 'completed'" in normalized and "target_user_id" in normalized:
            self.current = self.state.get("activeRow")
        elif "insert into public.user_erasure_requests" in normalized:
            if self.state.get("insertError"):
                raise self.state["insertError"]
            self.current = {
                "id": "request-1",
                "target_user_id": params[0],
                "subject_fingerprint": params[1],
                "idempotency_key": params[3],
                "status": "PENDING",
                "created_at": "2026-08-24T10:00:00+00:00",
            }
        elif "from public.user_erasure_requests" in normalized:
            self.current = self.state.get("requestRow")
        else:
            self.current = None

    def executemany(self, query, params):
        normalized = " ".join(str(query).split()).lower()
        values = list(params)
        self.state["executemany"].append((normalized, values))

    def fetchone(self):
        return self.current

    def fetchall(self):
        return list(self.state.get("stepRows", []))


class FakeConnection:
    def __init__(self, **overrides):
        self.state = {
            "executed": [],
            "executemany": [],
            "userExists": True,
            "userRow": {
                "userId": "user-1",
                "isBanned": False,
                "bannedAt": None,
                "bannedBy": None,
                "banReason": None,
            },
            "sessionCount": 2,
            **overrides,
        }
        self.commits = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self, **_kwargs):
        return FakeCursor(self.state)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed += 1


def _repository(connection):
    from api.services.userErasureRepository import UserErasureRepository

    return UserErasureRepository(connectionFactory=lambda: connection)


def test_create_request_persists_all_steps_and_freezes_billing_transactionally():
    connection = FakeConnection()
    repository = _repository(connection)

    row = repository.createRequest(
        userId="user-1",
        subjectFingerprint="a" * 64,
        adminId="admin-1",
        idempotencyKey="8cfdb150-417d-47ab-acd1-fef39d2bc14e",
        reason=None,
    )

    assert row["id"] == "request-1"
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed == 1
    assert len(connection.state["executemany"][0][1]) == 8
    assert [params[1] for params in connection.state["executemany"][0][1]] == [
        "revoke_access",
        "inventory",
        "stop_billing",
        "delete_storage",
        "delete_transient_state",
        "delete_auth_identity",
        "delete_database_data",
        "verify_and_finalize",
    ]
    assert any(
        "set erasure_pending = true" in query
        and "auto_renew_enabled = false" in query
        for query, _params in connection.state["executed"]
    )
    assert any(
        "update public.admin_free_trial_extensions" in query
        and "credit_sync_status = 'cancelled'" in query
        for query, _params in connection.state["executed"]
    )
    assert not any(
        "admin_free_trial_extension_items" in query
        or "admin_free_trial_extension_batches" in query
        for query, _params in connection.state["executed"]
    )


def test_create_request_locks_then_bans_and_revokes_sessions_before_workflow_mutations():
    connection = FakeConnection()

    _repository(connection).createRequest(
        userId="user-1",
        subjectFingerprint="a" * 64,
        adminId="admin-1",
        idempotencyKey="8cfdb150-417d-47ab-acd1-fef39d2bc14e",
        reason="support request",
    )

    calls = connection.state["executed"]
    sql = [query for query, _params in calls]
    lockIndex = next(i for i, query in enumerate(sql) if "pg_advisory_xact_lock" in query)
    userReadIndex = next(i for i, query in enumerate(sql) if 'from public."users"' in query)
    banIndex = next(i for i, query in enumerate(sql) if 'update public."users"' in query)
    sessionIndex = next(i for i, query in enumerate(sql) if 'delete from public."sessions"' in query)
    requestIndex = next(i for i, query in enumerate(sql) if "insert into public.user_erasure_requests" in query)
    subscriptionIndex = next(i for i, query in enumerate(sql) if "update public.subscriptions" in query)

    assert lockIndex < userReadIndex < banIndex < sessionIndex
    assert sessionIndex < requestIndex
    assert sessionIndex < subscriptionIndex
    assert calls[lockIndex][1] == ("user-1",)
    assert calls[userReadIndex][1] == ("user-1",)
    assert "for update" in sql[userReadIndex]
    assert calls[banIndex][1] == (
        "admin-1", "support request", "support request", "user-1"
    )
    assert calls[sessionIndex][1] == ("user-1",)
    assert connection.state["userRow"]["isBanned"] is True
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_create_request_rolls_back_if_atomic_session_revocation_fails():
    connection = FakeConnection(failOn='delete from public."sessions"')

    with pytest.raises(RuntimeError, match="database unavailable"):
        _repository(connection).createRequest(
            "user-1",
            "a" * 64,
            "admin-1",
            "8cfdb150-417d-47ab-acd1-fef39d2bc14e",
            None,
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closed == 1
    assert not any(
        "insert into public.user_erasure_requests" in query
        for query, _params in connection.state["executed"]
    )


def test_atomic_ban_clears_stale_reason_on_transition_but_preserves_existing_ban_reason():
    transitioning = FakeConnection(userRow={
        "userId": "user-1",
        "isBanned": False,
        "bannedAt": None,
        "bannedBy": None,
        "banReason": "stale-reason",
    })
    alreadyBanned = FakeConnection(userRow={
        "userId": "user-1",
        "isBanned": True,
        "bannedAt": "2026-08-20T00:00:00+00:00",
        "bannedBy": "admin-original",
        "banReason": "existing-reason",
    })

    for connection in (transitioning, alreadyBanned):
        _repository(connection).createRequest(
            "user-1",
            "a" * 64,
            "admin-new",
            "8cfdb150-417d-47ab-acd1-fef39d2bc14e",
            None,
        )

    transitionSql, transitionParams = next(
        call for call in transitioning.state["executed"]
        if 'update public."users"' in call[0]
    )
    assert 'case when not "isbanned"' in transitionSql
    assert transitionParams == ("admin-new", None, None, "user-1")
    assert transitioning.state["userRow"]["banReason"] is None
    assert alreadyBanned.state["userRow"]["banReason"] == "existing-reason"


def test_create_request_rejects_missing_user_and_active_workflow():
    missing = FakeConnection(userExists=False)
    with pytest.raises(AdminApiError) as captured:
        _repository(missing).createRequest(
            "missing", "a" * 64, "admin-1",
            "8cfdb150-417d-47ab-acd1-fef39d2bc14e", None,
        )
    assert captured.value.statusCode == 404
    assert missing.rollbacks == 1
    assert missing.commits == 0

    active = FakeConnection(activeRow={"id": "existing", "status": "IN_PROGRESS"})
    with pytest.raises(AdminApiError) as captured:
        _repository(active).createRequest(
            "user-1", "a" * 64, "admin-1",
            "8cfdb150-417d-47ab-acd1-fef39d2bc14e", None,
        )
    assert captured.value.statusCode == 409
    assert captured.value.message == "User already has an active erasure request"
    assert active.rollbacks == 1


def test_find_by_idempotency_and_get_request_include_ordered_steps():
    requestRow = {
        "id": "request-1",
        "subject_fingerprint": "a" * 64,
        "status": "PENDING",
        "created_at": "2026-08-24T10:00:00+00:00",
    }
    steps = [
        {"step_name": "revoke_access", "status": "COMPLETED"},
        {"step_name": "inventory", "status": "PENDING"},
    ]
    connection = FakeConnection(
        idempotencyRow=requestRow,
        requestRow=requestRow,
        stepRows=steps,
    )
    repository = _repository(connection)

    found = repository.findByIdempotency(
        "8cfdb150-417d-47ab-acd1-fef39d2bc14e"
    )
    loaded = repository.getRequest("request-1")

    assert found == requestRow
    assert loaded == {**requestRow, "steps": steps}
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert connection.closed == 2


def test_create_request_rolls_back_and_closes_on_database_error():
    connection = FakeConnection(insertError=RuntimeError("database unavailable"))

    with pytest.raises(RuntimeError, match="database unavailable"):
        _repository(connection).createRequest(
            "user-1", "a" * 64, "admin-1",
            "8cfdb150-417d-47ab-acd1-fef39d2bc14e", None,
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closed == 1


def test_database_erasure_deletes_trial_reduction_ledger_before_subscription():
    from api.services.userErasureRepository import UserErasureRepository

    connection = FakeConnection()

    class RecordingRepository(UserErasureRepository):
        def __init__(self):
            super().__init__(connectionFactory=lambda: connection)
            self.deletedTables = []

        def _tableExists(self, _cursor, _tableName):
            return False

        def _deleteByUser(
            self, _cursor, tableName, _columnName, _userId
        ):
            self.deletedTables.append(tableName)

    repository = RecordingRepository()

    repository.deleteDatabaseData("request-1", "user-1", [])

    assert connection.state["executed"][0] == (
        "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
        ("user-1",),
    )
    assert "admin_free_trial_reductions" in repository.deletedTables
    assert repository.deletedTables.index(
        "admin_free_trial_reductions"
    ) < repository.deletedTables.index("subscriptions")
