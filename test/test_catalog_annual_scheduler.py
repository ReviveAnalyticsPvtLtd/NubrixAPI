"""Scheduled annual renewals keep the same financial promise after publication."""
from unittest.mock import Mock

from test.test_manual_billing_runtime import database, USER, sqlTransaction, read_row
from test.test_manual_checkout_http import checkout_database, request
from test.test_manual_payment_entrypoints import paid_then_request, evidence
from test.test_final_review_regressions import annual_task_database


def test_scheduler_never_reprices_an_issued_annual_invoice_or_order(annual_task_database, monkeypatch):
    import api.services.billing.invoiceService as invoices
    repo, path, client = annual_task_database
    monkeypatch.setattr(invoices, 'client', client)
    monkeypatch.setattr(invoices, 'getBillingRedisClient', lambda: Mock(set=lambda *a, **kw: True))
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository', lambda: repo)
    manual, initial = paid_then_request((repo, path), 'annual_prepaid', 'initial_purchase')
    manual.finalizeCapturedPayment(evidence(manual.createCheckout(initial)))
    subscription = read_row(path, 'subscriptions')
    import json
    for key in ('subscribed_experts', 'pending_removals', 'billing_state'):
        subscription[key] = json.loads(subscription[key])
    issued = invoices.createUpcomingRenewalInvoice(subscription, {'userId': USER})
    prepared = invoices.prepareDashboardRenewalInvoice(issued)
    checkout = request('issued-annual-renewal', mode='annual_prepaid', purpose='renewal', invoiceId=issued['id'])
    original = manual.createCheckout(checkout)
    monkeypatch.setattr('api.services.billing.billingEngine._getAnnualBasePrice', lambda **kw: {
        'amount': 99000, 'currency': 'INR', 'plan_id': 'changed-after-issuance'})
    again = invoices.prepareDashboardRenewalInvoice(prepared)
    assert again['total_amount'] == prepared['total_amount']
    replay = manual.createCheckout(checkout)
    assert replay.amount == original.amount and replay.razorpayOrderId == original.razorpayOrderId
    with sqlTransaction(path) as connection:
        row = connection.execute('SELECT total_amount FROM "Invoices" WHERE id=?', (issued['id'],)).fetchone()
    assert row[0] == original.amount
