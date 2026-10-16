"""Validate real HTTP JSON encoding of dates returned by the SQL repository."""
from datetime import date, datetime, timezone
import json

import pytest
import requests

from nubrix.triggers.tasks.annualRenewalTask import AnnualRenewalTask
from test.test_renewal_email_flow import _FakeClient, _FakeRedis


@pytest.mark.parametrize('due_date,expected', [
    (datetime(2026, 12, 1, tzinfo=timezone.utc), '2026-12-01T00:00:00+00:00'),
    (date(2026, 12, 1), '2026-12-01'),
    ('2026-12-01T00:00:00+00:00', '2026-12-01T00:00:00+00:00'),
    (None, ''),
])
def test_t7_email_encodes_sql_due_date_without_consuming_failed_retries(monkeypatch, due_date, expected):
    monkeypatch.setenv('RENEWAL_REMINDER_EMAIL_URL', 'https://email.example.test')
    task = AnnualRenewalTask.__new__(AnnualRenewalTask)
    task.client, task.redisClient = _FakeClient(), _FakeRedis()
    bodies = []
    def prepare_only(*args, **kwargs):
        # Use requests' real JSON encoder, but never send a network request.
        request = requests.Request('POST', kwargs['url'], json=kwargs['json']).prepare()
        bodies.append(json.loads(request.body))
        response = requests.Response()
        response.status_code = 200
        return response
    monkeypatch.setattr('nubrix.triggers.tasks.annualRenewalTask.requests.post', prepare_only)
    task._sendT7Email({'userId': 'u1', 'email': 'user@example.test'},
        {'id': 'invoice', 'total_amount': 23600, 'currency': 'USD', 'due_date': due_date},
        {'subscribed_experts': ['banking']})
    assert bodies and bodies[0]['dueDate'] == expected
    assert task.client.state['inserts'][0]['metadata_json']['deliveryStatus'] == 'SENT'
