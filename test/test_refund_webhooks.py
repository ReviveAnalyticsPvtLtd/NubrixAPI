"""Refund event routing with real credit SQL and a local provider boundary."""
import json
import sys
from types import SimpleNamespace

import pytest

from test.test_manual_billing_runtime import database, USER, SUB, sqlTransaction, read_row
from test.test_manual_auth_coverage import SqlRestClient, SqlRestQuery
from api.services.credits.manualCreditRepository import ManualCreditRepository


class WebhookRestQuery(SqlRestQuery):
    def execute(self):
        if self.payload is None:
            return super().execute()
        # Supabase returns the rows changed by the conditional claim, even
        # though their new status no longer matches the original WHERE clause.
        import sqlite3
        with sqlTransaction(self.path) as db:
            db.row_factory = sqlite3.Row
            assignments = ','.join(f'"{key}"=?' for key in self.payload)
            where = ' AND '.join(f'"{key}"=?' for key, _ in self.filters)
            values = [json.dumps(value) if isinstance(value, (dict, list)) else value
                      for value in self.payload.values()]
            rows = db.execute(f'UPDATE "{self.table}" SET {assignments} WHERE {where} RETURNING *',
                              values + [value for _, value in self.filters]).fetchall()
        return SimpleNamespace(data=[dict(row) for row in rows])


class WebhookRestClient(SqlRestClient):
    def table(self, name):
        return WebhookRestQuery(self.path, name)


def refundEvent(kind='processed', refundId='refund-one', amount=150, notes=None):
    return {'event': 'refund.' + kind, 'payload': {'refund': {'entity': {
        'id': refundId, 'payment_id': 'topup-payment', 'amount': amount,
        'currency': 'INR', 'status': 'pending' if kind == 'created' else kind,
        'notes': notes or {}}}}}


@pytest.fixture
def webhook(database, monkeypatch):
    from unittest.mock import MagicMock
    # The audit/provider boundaries are local; importing the production logger
    # would otherwise start its network delivery worker in standalone runs.
    monkeypatch.setitem(sys.modules, 'utils.logger', SimpleNamespace(logger=MagicMock()))
    from api.services.webhookService import WebhookService
    repo, path = database
    with sqlTransaction(path) as db:
        db.execute('''CREATE TABLE "WebhookEvents" ("razorpayEventId" TEXT UNIQUE,
            "eventType" TEXT,payload TEXT,status TEXT,attempts INTEGER,"lastAttemptAt" TEXT,
            "errorMessage" TEXT,"completedAt" TEXT,user_id TEXT)''')
        db.execute('''INSERT INTO credit_balances(user_id,subscription_id,topup_tokens)
            VALUES(?,?,500)''', (USER, SUB))
        db.execute('''INSERT INTO "Invoices"(id,"userId",subscription_id,status,billing_reason,
            total_amount,currency,"razorpayPaymentId",metadata_json)
            VALUES('topup-invoice',?,?,'PAID','add_on',150,'INR','topup-payment',?)''',
            (USER, SUB, json.dumps({'tokens': 500})))
    credits = ManualCreditRepository(repo)
    provider = SimpleNamespace(payment=SimpleNamespace(fetch=lambda paymentId: {
        'id': paymentId, 'notes': {'type': 'credit_topup', 'userId': USER}}))
    monkeypatch.setitem(sys.modules, 'api.services.subscriptions.subscriptionService',
        SimpleNamespace(subscriptionService=SimpleNamespace(razorpayClient=provider)))
    monkeypatch.setitem(sys.modules, 'api.services.credits.creditService',
        SimpleNamespace(creditService=SimpleNamespace(clawbackTopupTokens=credits.clawbackTopup)))
    service = WebhookService.__new__(WebhookService)
    service.client = WebhookRestClient(path)
    audits = []
    service._auditLog = lambda *args, **kwargs: audits.append((args, kwargs))
    return service, credits, path, audits


def test_refund_created_does_not_change_balances(webhook):
    service, _, path, audits = webhook
    service.processEvent(refundEvent('created'), 'event-created')
    assert read_row(path, 'credit_balances')['topup_tokens'] == 500
    assert read_row(path, 'WebhookEvents')['status'] == 'completed'
    assert audits[-1][0][1] == 'refund.created'
    assert audits[-1][1]['status'] == 'pending'


def test_refund_failed_restores_nothing(webhook):
    service, _, path, audits = webhook
    service.processEvent(refundEvent('failed'), 'event-failed')
    assert read_row(path, 'credit_balances')['topup_tokens'] == 500
    assert read_row(path, 'WebhookEvents')['status'] == 'completed'
    assert audits[-1][0][1] == 'refund.failed'
    assert audits[-1][1]['status'] == 'failed'


@pytest.mark.parametrize('invoiceStatus', ['PAYMENT_PENDING', 'FAILED', 'SUPERSEDED', None])
def test_refund_webhook_for_ungranted_topup_is_recorded_once(webhook, invoiceStatus):
    service, credits, path, _ = webhook
    with sqlTransaction(path) as db:
        if invoiceStatus is None:
            db.execute('DELETE FROM "Invoices"')
        else:
            db.execute('UPDATE "Invoices" SET status=?', (invoiceStatus,))
    service.processEvent(refundEvent(), 'ungranted-one')
    service.processEvent(refundEvent(), 'ungranted-replay')
    assert read_row(path, 'credit_balances')['topup_tokens'] == 500
    with sqlTransaction(path) as db:
        assert db.execute('SELECT status FROM "WebhookEvents"').fetchall() == [('completed',), ('completed',)]
        rows = db.execute('SELECT event_status,metadata_json FROM billing_events').fetchall()
    assert len(rows) == 1 and rows[0][0] == 'NOT_GRANTED'
    assert json.loads(rows[0][1])['paymentId'] == 'topup-payment'
    assert credits.clawbackTopup(USER, 'refund-one', 'topup-payment', 150) == {
        'clawed': False, 'tokens': 0, 'status': 'already_applied'}
    with pytest.raises(ValueError, match='TOPUP_REFUND_IDENTITY_CONFLICT'):
        credits.clawbackTopup(USER, 'refund-one', 'topup-payment', 149)


def test_processed_refund_of_granted_topup_claws_back_once(webhook):
    service, credits, path, _ = webhook
    service.processEvent(refundEvent(amount=50), 'processed-first')
    service.processEvent(refundEvent(amount=50), 'processed-replay')
    assert read_row(path, 'credit_balances')['topup_tokens'] == 334
    assert credits.clawbackTopup(USER, 'refund-two', 'topup-payment', 100) == {
        'clawed': True, 'tokens': 334, 'status': 'clawed_back'}
    assert read_row(path, 'credit_balances')['topup_tokens'] == 0


def test_not_granted_outcome_is_explicit(webhook):
    _, credits, path, _ = webhook
    with sqlTransaction(path) as db:
        db.execute("UPDATE \"Invoices\" SET status='PAYMENT_PENDING'")
    assert credits.clawbackTopup(USER, 'refund-one', 'topup-payment', 150) == {
        'clawed': False, 'tokens': 0, 'status': 'not_granted'}


def test_processed_refund_fetch_failure_retries_and_replay_claws_back_once(webhook, monkeypatch):
    from utils.webhookExceptions import RetryableWebhookError
    service, _, path, _ = webhook
    payment = sys.modules['api.services.subscriptions.subscriptionService'].subscriptionService.razorpayClient.payment
    availableFetch = payment.fetch
    def unavailableFetch(paymentId):
        raise TimeoutError('synthetic provider outage')
    monkeypatch.setattr(payment, 'fetch', unavailableFetch)
    event = refundEvent(amount=50)
    with pytest.raises(RetryableWebhookError):
        service.processEvent(event, 'retryable-refund')
    failed = read_row(path, 'WebhookEvents')
    assert failed['status'] == 'failed' and failed['attempts'] == 1
    assert json.loads(failed['payload']) == event
    assert read_row(path, 'credit_balances')['topup_tokens'] == 500
    assert read_row(path, 'billing_events') is None
    monkeypatch.setattr(payment, 'fetch', availableFetch)
    service.processEvent(event, 'retryable-refund')
    completed = read_row(path, 'WebhookEvents')
    assert completed['status'] == 'completed' and completed['attempts'] == 2
    assert completed['errorMessage'] is None
    service.processEvent(event, 'retryable-refund')
    service.processEvent(event, 'same-refund-new-delivery')
    assert read_row(path, 'credit_balances')['topup_tokens'] == 334
    with sqlTransaction(path) as db:
        assert db.execute('SELECT count(*) FROM billing_events').fetchone()[0] == 1


@pytest.mark.parametrize('delayedKind', ['created', 'failed'])
def test_delayed_refund_event_preserves_processed_settlement(webhook, delayedKind):
    service, _, path, _ = webhook
    service.processEvent(refundEvent(amount=50), 'processed-first')
    assert read_row(path, 'credit_balances')['topup_tokens'] == 334
    service.processEvent(refundEvent(delayedKind, amount=50), 'delayed-event')
    assert read_row(path, 'credit_balances')['topup_tokens'] == 334
    with sqlTransaction(path) as db:
        assert db.execute('SELECT event_type,event_status FROM billing_events').fetchall() == [
            ('credit.topup_refunded', 'SETTLED')]
        assert db.execute('SELECT status FROM "WebhookEvents"').fetchall() == [('completed',), ('completed',)]
