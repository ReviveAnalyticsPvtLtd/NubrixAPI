"""Hand-authored current-shaped records, never a live row export."""
from copy import deepcopy
from datetime import datetime, timezone

NOW = datetime(2026, 10, 18, 10, tzinfo=timezone.utc)
SUB = '10000000-0000-4000-8000-000000000001'
LIFE = '10000000-0000-4000-8000-000000000002'
INVOICE = '10000000-0000-4000-8000-000000000003'
EVENT = '10000000-0000-4000-8000-000000000004'
OWNER = {'userId': 'model-backfill-user', 'isBanned': False}


def source(condition='none'):
    row = dict(id=SUB, user_id=OWNER['userId'], billing_mode='none',
               current_period_start=None, current_period_end=None, renewal_due_at=None,
               auto_renew_enabled=False, payment_collection_mode='authenticated_checkout',
               status='none', default_currency='INR', subscribed_experts=[], domain_count=0,
               pending_removals=[], pending_additions=[], billing_state={'unknown': {'keep': [1, 2]}},
               cancellation_reason=None, version=1, created_at=NOW, updated_at=NOW,
               plan_type='none', erasure_pending=False, admin_credit_generation=0,
               is_canonical=True, renewal_opt_out=False)
    invoices, events = [], []
    if condition == 'trial':
        start = datetime(2026, 10, 10, 10, tzinfo=timezone.utc)
        end = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)
        row.update(status='trial', plan_type='free', current_period_start=start,
                   current_period_end=end, subscribed_experts=['banking', 'manufacturing', 'supplychain', 'telecom'],
                   domain_count=4)
        row['billing_state'].update(trialConsumed=True, trialConsumedAt=start.isoformat())
    if condition == 'paid':
        end = datetime(2026, 11, 18, 10, tzinfo=timezone.utc)
        row.update(status='active', plan_type='pro', billing_mode='monthly_prepaid',
                   current_period_start=NOW, current_period_end=end,
                   subscribed_experts=['banking'], domain_count=1)
        row['billing_state']['manualBilling'] = {'lifecycleId': LIFE}
        invoices = [dict(id=INVOICE, userId=OWNER['userId'], subscription_id=SUB, status='PAID',
                         billing_reason='initial_purchase', period_start=NOW, period_end=end,
                         total_amount=10000, currency='INR', razorpay_order_id='order-synthetic',
                         razorpayPaymentId='payment-synthetic', paidAt=NOW,
                         metadata_json={'manualBilling': {'purpose': 'initial_purchase',
                            'billingMode': 'monthly_prepaid', 'lifecycleId': LIFE,
                            'creditPeriodId': None, 'domains': ['banking'], 'coverageState': 'active'}})]
        events = [dict(id=EVENT, user_id=OWNER['userId'], subscription_id=SUB, invoice_id=INVOICE,
                       event_category='audit', event_type='payment.capture', event_status='FINALIZED',
                       provider_payment_id='payment-synthetic', provider_order_id='order-synthetic',
                       amount=10000, currency='INR', metadata_json={})]
    return deepcopy(row), deepcopy(OWNER), deepcopy(invoices), deepcopy(events)
