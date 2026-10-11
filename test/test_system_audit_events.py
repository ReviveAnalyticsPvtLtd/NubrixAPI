"""System-level billing events carry no user: billing_events.user_id references Users."""
from unittest.mock import MagicMock, patch


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, client):
        return self

    def log_event(self, **kwargs):
        self.calls.append(kwargs)


def test_token_webhooks_audit_without_a_fake_user():
    from api.services.webhookService import WebhookService
    service = WebhookService.__new__(WebhookService)
    service.client = MagicMock()
    recorder = _Recorder()
    event = {'payload': {'token': {'entity': {'id': 'token_1'}}}}
    with patch('api.services.webhookService.BillingEventService', recorder):
        service._handleTokenConfirmed(event)
        service._handleTokenCancelled(event)
    assert [call['user_id'] for call in recorder.calls] == [None, None]
    assert [call['event_type'] for call in recorder.calls] == ['token.confirmed', 'token.cancelled']


def test_metric_alerts_audit_without_a_fake_user():
    from api.services.billing.billingMetricsService import BillingMetricsService
    service = BillingMetricsService.__new__(BillingMetricsService)
    service.client = MagicMock()
    recorder = _Recorder()
    with patch('api.services.billing.billingMetricsService.BillingEventService', recorder):
        service._persistAlerts([{'alertType': 'failure_rate', 'severity': 'WARNING'}])
    assert [call['user_id'] for call in recorder.calls] == [None]
