"""Production authentication/coverage behavior with local SQL and REST transports.

Replacing exact-time coverage by display days, selecting history or writing all
owner rows must fail these assertions. SQLite is deterministic evidence only;
PostgreSQL owner-lock races are in the integration suite.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from test.test_manual_billing_runtime import (
    database, NOW, USER, SUB, LIFE, seed_payment, sqlTransaction, read_row,
)


class SqlRestQuery:
    def __init__(self, path, table):
        self.path, self.table = path, table
        self.filters, self.sort, self.count, self.payload = [], None, None, None

    def select(self, *args):
        return self

    def eq(self, key, value):
        self.filters.append((key, value))
        return self

    def order(self, key, desc=False):
        self.sort = (key, desc)
        return self

    def limit(self, count):
        self.count = count
        return self

    def update(self, payload):
        self.payload = payload
        return self

    def insert(self, payload):
        with sqlTransaction(self.path) as connection:
            keys = ','.join(f'"{key}"' for key in payload)
            values = [json.dumps(value) if isinstance(value, (dict, list)) else value for value in payload.values()]
            connection.execute(f'INSERT INTO "{self.table}"({keys}) VALUES({",".join("?" for _ in values)})', values)
        return self

    def execute(self):
        import sqlite3
        with sqlTransaction(self.path) as connection:
            connection.row_factory = sqlite3.Row
            where = " AND ".join(f'"{key}"=?' for key, _ in self.filters) or "1=1"
            params = [value for _, value in self.filters]
            if self.payload is not None:
                values = [json.dumps(v) if isinstance(v, (dict, list)) else v
                          for v in self.payload.values()]
                assignments = ",".join(f'"{key}"=?' for key in self.payload)
                connection.execute(f'UPDATE "{self.table}" SET {assignments} WHERE {where}', values + params)
            query = f'SELECT * FROM "{self.table}" WHERE {where}'
            if self.sort:
                query += f' ORDER BY "{self.sort[0]}" ' + ("DESC" if self.sort[1] else "ASC")
            if self.count is not None:
                query += " LIMIT " + str(int(self.count))
            rows = [dict(row) for row in connection.execute(query, params)]
        for row in rows:
            for key in ("billing_state", "subscribed_experts", "pending_removals", "pending_additions"):
                if isinstance(row.get(key), str):
                    row[key] = json.loads(row[key])
        return SimpleNamespace(data=rows)


class SqlRestClient:
    def __init__(self, path):
        self.path = path

    def table(self, name):
        return SqlRestQuery(self.path, name)


def auth_service(database):
    from api.services.authenticationService import AuthenticationService
    service = AuthenticationService.__new__(AuthenticationService)
    service.client = SqlRestClient(database[1])
    return service


def activate_initial(database):
    repository, _ = database
    return repository.finalizeCapturedPayment(seed_payment(database))


def test_login_same_day_preserves_paid_hours(database):
    repository, path = database
    activate_initial(database)
    end = NOW + timedelta(hours=5)
    with sqlTransaction(path) as connection:
        connection.execute("UPDATE subscriptions SET current_period_end=? WHERE id=?", (end.isoformat(), SUB))
        connection.execute('UPDATE "Invoices" SET period_end=?', (end.isoformat(),))
    service = auth_service(database)
    row = service._getSubscriptionSnapshot(USER)
    with patch('api.services.billing.manualBillingRepository.getManualBillingRepository', return_value=repository), \
         patch('api.services.subscriptions.paymentValidationService.utcNow', return_value=NOW):
        service._refreshLifecycleSnapshot(USER, row)
    assert row['status'] == 'active'
    assert read_row(path, 'subscriptions')['current_period_end'] == end.isoformat()
    assert read_row(path, 'credit_balances')['monthly_token_quota'] > 0


def test_newer_historical_row_does_not_replace_canonical(database):
    activate_initial(database)
    _, path = database
    with sqlTransaction(path) as connection:
        connection.execute("INSERT INTO subscriptions(id,user_id,is_canonical,billing_mode,status,updated_at) VALUES('history',?,0,'annual_prepaid','expired','2099-01-01')", (USER,))
    assert auth_service(database)._getSubscriptionSnapshot(USER)['id'] == SUB


def test_auth_refresh_does_not_mutate_history(database):
    repository, path = database
    activate_initial(database)
    with sqlTransaction(path) as connection:
        connection.execute("INSERT INTO subscriptions(id,user_id,is_canonical,billing_mode,status,billing_state) VALUES('history',?,0,'none','none','{}')", (USER,))
    service = auth_service(database)
    row = service._getSubscriptionSnapshot(USER)
    with patch('api.services.billing.manualBillingRepository.getManualBillingRepository', return_value=repository):
        service._refreshLifecycleSnapshot(USER, row)
    with sqlTransaction(path) as connection:
        historical = connection.execute("SELECT billing_state,status FROM subscriptions WHERE id='history'").fetchone()
    assert historical == ('{}', 'none')


def test_login_materializes_paid_continuation(database):
    repository, path = database
    activate_initial(database)
    current_end = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    with sqlTransaction(path) as connection:
        connection.execute("UPDATE subscriptions SET current_period_start='2026-09-20T12:00:00+00:00',current_period_end=? WHERE id=?", (current_end.isoformat(), SUB))
        connection.execute('UPDATE "Invoices" SET period_start=\'2026-09-20T12:00:00+00:00\',period_end=?', (current_end.isoformat(),))
    renewal = seed_payment(database, purpose='renewal', invoice='future', order='future-order')
    from dataclasses import replace
    repository.finalizeCapturedPayment(replace(renewal, providerPaymentId='future-payment'))
    service = auth_service(database)
    row = service._getSubscriptionSnapshot(USER)
    with patch('api.services.billing.manualBillingRepository.getManualBillingRepository', return_value=repository), \
         patch('test.test_manual_billing_runtime.NOW', current_end), \
         patch('api.services.billing.manualBillingRepository._now', return_value=current_end), \
         patch('api.services.subscriptions.paymentValidationService.utcNow', return_value=current_end):
        service._refreshLifecycleSnapshot(USER, row)
    assert row['status'] == 'active'
    assert row['current_period_start'] == '2026-10-20T12:00:00+00:00'
    assert row['current_period_end'] == '2026-11-20T12:00:00+00:00'


def test_coverage_snapshot_uses_exact_boundary(database):
    repository, _ = database
    period = activate_initial(database).currentPeriod
    assert repository.getCoverageSnapshot(USER, period.end-timedelta(microseconds=1)).accessAllowed
    assert not repository.getCoverageSnapshot(USER, period.end).accessAllowed


def test_webhook_targets_canonical_row(database):
    from api.services.webhookService import WebhookService
    _, path = database
    with sqlTransaction(path) as connection:
        connection.execute("INSERT INTO subscriptions(id,user_id,is_canonical,status,updated_at) VALUES('history',?,0,'expired','2099-01-01')", (USER,))
    service = WebhookService.__new__(WebhookService)
    service.client = SqlRestClient(path)
    assert service._findSubscriptionByUserId(USER)['id'] == SUB


def test_missing_subscription_creates_non_entitled_canonical_shell(database):
    repository, path = database
    with sqlTransaction(path) as connection:
        connection.execute('DELETE FROM subscriptions')
    row = repository.ensureCanonicalSubscription(USER)
    assert row['id'] is not None
    snapshot = repository.getCoverageSnapshot(USER, NOW)
    assert not snapshot.accessAllowed and snapshot.currentPeriod is None
    with sqlTransaction(path) as connection:
        assert connection.execute('SELECT count(*) FROM subscriptions WHERE is_canonical=1').fetchone()[0] == 1


def test_auth_does_not_guess_canonical_from_history(database):
    _, path = database
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE subscriptions SET is_canonical=0')
    with pytest.raises(Exception) as error:
        auth_service(database)._ensureSubscriptionSnapshot(USER)
    assert error.value.statusCode == 409
    with sqlTransaction(path) as connection:
        assert connection.execute('SELECT count(*) FROM subscriptions').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM subscriptions WHERE is_canonical=1').fetchone()[0] == 0


def test_ambiguous_canonical_fails_closed(database):
    repository, path = database
    with sqlTransaction(path) as connection:
        connection.execute("INSERT INTO subscriptions(id,user_id,is_canonical,status) VALUES('duplicate',?,1,'active')", (USER,))
    with pytest.raises(ValueError, match='AMBIGUOUS_CANONICAL'):
        repository.getCoverageSnapshot(USER, NOW)


def test_entitlement_uses_paid_evidence_not_active_label(database):
    from api.services.subscriptions.entitlementService import SubscriptionEntitlementService
    repository, path = database
    activate_initial(database)
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE "Invoices" SET status=\'VOID\'')
    service = SubscriptionEntitlementService(SqlRestClient(path))
    with patch('api.services.billing.manualBillingRepository.getManualBillingRepository', return_value=repository):
        assert not service.get(USER).activeSubscription


def test_snapshot_does_not_use_annual_addition_as_coverage(database):
    repository, path = database
    activate_initial(database)
    with sqlTransaction(path) as connection:
        connection.execute("UPDATE subscriptions SET billing_mode='annual_prepaid',plan_type='annual'")
        metadata = json.loads(connection.execute('SELECT metadata_json FROM "Invoices"').fetchone()[0])
        metadata['manualBilling']['purpose'] = 'current_expert_addition'
        connection.execute('UPDATE "Invoices" SET metadata_json=?', (json.dumps(metadata),))
    assert not repository.getCoverageSnapshot(USER, NOW).accessAllowed


def test_paid_snapshot_respects_erasure(database):
    repository, path = database
    activate_initial(database)
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE subscriptions SET erasure_pending=1')
    snapshot = repository.getCoverageSnapshot(USER, NOW)
    assert not snapshot.accessAllowed
    assert snapshot.denialReason == 'erasure_pending'


def test_expiry_preserves_trial_consumption(database):
    repository, path = database
    period = activate_initial(database).currentPeriod
    with sqlTransaction(path) as connection:
        state = json.loads(read_row(path, 'subscriptions')['billing_state'])
        state['trialConsumed'] = True
        connection.execute('UPDATE subscriptions SET billing_state=?', (json.dumps(state),))
    repository.getCoverageSnapshot(USER, period.end)
    assert json.loads(read_row(path, 'subscriptions')['billing_state'])['trialConsumed']
    with pytest.raises(ValueError, match='TRIAL_NOT_ELIGIBLE'):
        repository.activateTrial(USER, ('banking',))


def test_trial_consumption_is_durable_and_cannot_repeat(database):
    repository, path = database
    repository.activateTrial(USER, ('banking',))
    with pytest.raises(ValueError, match='TRIAL_NOT_ELIGIBLE'):
        repository.activateTrial(USER, ('banking',))
    assert json.loads(read_row(path, 'subscriptions')['billing_state'])['trialConsumed']


def test_paid_snapshot_respects_ban(database):
    repository, path = database
    activate_initial(database)
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE "Users" SET "isBanned"=1 WHERE "userId"=?', (USER,))
    snapshot = repository.getCoverageSnapshot(USER, NOW)
    assert not snapshot.accessAllowed
    assert snapshot.denialReason == 'account_banned'


def test_annual_early_renewal_preserves_verified_earlier_interval(database):
    repository, path = database
    activate_initial(database)
    with sqlTransaction(path) as connection:
        connection.execute("UPDATE subscriptions SET billing_mode='annual_prepaid',plan_type='annual',current_period_start='2027-10-06T12:00:00+00:00',current_period_end='2028-10-06T12:00:00+00:00'")
        connection.execute('UPDATE "Invoices" SET period_start=\'2026-10-06T12:00:00+00:00\',period_end=\'2027-10-06T12:00:00+00:00\'')
    assert repository.getCoverageSnapshot(USER, NOW).accessAllowed


def test_profile_refresh_preserves_final_paid_hours(database):
    from api.services.managementService import ManagementService
    repository, path = database
    activate_initial(database)
    end = NOW + timedelta(hours=5)
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE subscriptions SET current_period_end=?', (end.isoformat(),))
        connection.execute('UPDATE "Invoices" SET period_end=?', (end.isoformat(),))
    service = ManagementService.__new__(ManagementService)
    service.client = SqlRestClient(path)
    row = service._getCanonicalSubscription(USER)
    with patch('api.services.billing.manualBillingRepository.getManualBillingRepository', return_value=repository), \
         patch('api.services.subscriptions.paymentValidationService.utcNow', return_value=NOW):
        service._refreshLifecycleSnapshot(USER, row)
    assert row['status'] == 'active' and row['current_period_end'] == end.isoformat()


@pytest.mark.parametrize('kind', ['password', 'google', 'azure-ad'])
@pytest.mark.parametrize('offset,expected', [(timedelta(hours=-5), 'ACTIVE'), (timedelta(0), 'EXPIRED')])
def test_all_login_variants_share_exact_boundary(database, kind, offset, expected):
    import hashlib
    import os
    from api.models import Login, LoginWithProvider
    repository, path = database
    period = activate_initial(database).currentPeriod
    evaluated = period.end + offset
    password = hashlib.md5(('secret' + os.environ['SECRET_KEY']).encode()).hexdigest()
    with sqlTransaction(path) as connection:
        connection.execute('UPDATE "Users" SET email=?,password=?,onboarded=1,"currentWorkspaceId"=\'workspace\',"profileImage"=\'https://example.test/avatar\' WHERE "userId"=?', ('user@example.test', password, USER))
        connection.execute('CREATE TABLE "Sessions"("userId" TEXT,email TEXT,"accessToken" TEXT,"sessionStartTime" TEXT,"lastActivity" TEXT,"createdAt" TEXT,"expiresAt" TEXT)')
    service = auth_service(database)
    auth_user = SimpleNamespace(email='user@example.test', id=USER, email_confirmed_at='2026-01-01')
    service.client.auth = SimpleNamespace(admin=SimpleNamespace(list_users=lambda page, per_page: [auth_user] if page == 1 else []))
    with patch('api.services.billing.manualBillingRepository.getManualBillingRepository', return_value=repository), \
         patch('test.test_manual_billing_runtime.NOW', evaluated), \
         patch.object(service, '_getCreditSnapshot', return_value={}), \
         patch('api.services.subscriptions.paymentValidationService.utcNow', return_value=evaluated):
        if kind == 'password':
            result = service.login(Login(email='user@example.test', password='secret'))
        else:
            result = service.loginWithProvider(LoginWithProvider(email='user@example.test', sub='provider-sub', provider=kind))
    assert result['subscriptionStatus'] == expected
    assert read_row(path, 'subscriptions')['current_period_end'] == period.end.isoformat()
