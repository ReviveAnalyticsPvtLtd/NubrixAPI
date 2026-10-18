"""Synthetic runtime authority fixtures; only the guarded localhost fixture runs DDL."""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

from api.services.billing.manualBillingContracts import CheckoutRequest, VerifiedPaymentEvidence
from api.services.billing.manualBillingRepository import ManualBillingRepository
from api.services.subscriptions.subscriptionModelRepository import SubscriptionModelRepository
from test.test_manual_billing_postgres_integration import postgres  # noqa: F401

NOW = datetime.now(timezone.utc)


@pytest.fixture(scope='module')
def runtimeDb(postgres):
    root = Path(__file__).resolve().parents[1]
    with psycopg2.connect(postgres) as db, db.cursor() as cursor:
        cursor.execute((root / 'supabase/migrations/20261017211222_expand_subscription_model_v2.sql').read_text())
        for path in sorted(list((root / 'supabase/migrations').glob('*_protect_subscription_purchase_authority.sql'))
                + list((root / 'supabase/migrations').glob('*_protect_subscription_trial_authority.sql'))
                + list((root / 'supabase/migrations').glob('*_extend_annual_subscription_notifications.sql'))):
            if path.read_text().strip():
                cursor.execute(path.read_text())
    return postgres


def query(url, statement, args=(), *, one=False):
    with psycopg2.connect(url) as db, db.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(statement, args)
        if cursor.description:
            return cursor.fetchone() if one else list(cursor.fetchall())


def capture(repo, intent, now=NOW):
    order = 'order-synthetic-' + str(uuid4())
    repo.bindProviderOrder(intent.attemptId, {'id': order})
    evidence = VerifiedPaymentEvidence(intent.attemptId, intent.invoiceId, intent.userId, order,
        'pay-synthetic-' + str(uuid4()), intent.purpose, intent.currency, 'captured',
        'server_observation', intent.amount, now, None, None, False)
    return repo.finalizeCapturedPayment(evidence), evidence


@pytest.fixture
def account(runtimeDb, monkeypatch, request):
    monkeypatch.setattr('api.services.billing.manualBillingRepository._now', lambda: NOW)
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE', 'legacy')
    user = 'runtime-synthetic-' + str(uuid4())
    query(runtimeDb, 'INSERT INTO public."Users"("userId") VALUES(%s)', (user,))
    repo = ManualBillingRepository(lambda: psycopg2.connect(runtimeDb))
    repo.ensureCanonicalSubscription(user)
    mode = getattr(request, 'param', 'monthly_prepaid')
    intent = repo.reserveCheckout(CheckoutRequest(user, 'initial_purchase', mode,
        {'domains': ['banking', 'manufacturing']}, expectedCatalogRevision=1))
    capture(repo, intent)
    # Explicit promoted identity, as a reviewed migration would supply it.
    query(runtimeDb, "UPDATE subscriptions SET plan_code='pro',lifecycle_id=%s WHERE user_id=%s",
          (intent.lifecycleId, user))
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE', 'v2')
    model = SubscriptionModelRepository(lambda: psycopg2.connect(runtimeDb))
    return repo, model, intent, runtimeDb


def selection(account, experts):
    repo, model, intent, url = account
    row = query(url, 'SELECT * FROM subscriptions WHERE user_id=%s', (intent.userId,), one=True)
    with psycopg2.connect(url) as db, db.cursor(cursor_factory=RealDictCursor) as cursor:
        return model.saveNextSelection(cursor, str(row['id']), intent.lifecycleId,
            row['current_period_end'], row['version'], experts, now=NOW)


def renewal(account):
    repo, model, intent, url = account
    context = repo.getRenewalContext(intent.userId, NOW)
    from dateutil.relativedelta import relativedelta
    start = context['current_period_end']
    payload = dict(userId=intent.userId, subscription_id=context['id'], billing_reason='renewal',
        payment_flow='razorpay_order_checkout', requires_customer_auth=True,
        period_start=start, period_end=start + relativedelta(months=1), status='UPCOMING',
        metadata_json={'manualBilling': dict(lifecycleId=intent.lifecycleId, purpose='renewal',
            billingMode='monthly_prepaid', domains=context['_verifiedCoverage']['nextExperts'],
            coverageState='scheduled', revision=1)})
    return repo.createFrozenRenewalInvoice(payload, context['version'], expectedCatalogRevision=1)


def expirePurchasedCoverage(account, *, staleCache=True):
    """Synthetic historical paid term with deliberately untrustworthy old caches."""
    from dateutil.relativedelta import relativedelta
    repo, model, intent, url = account
    start, end = NOW - relativedelta(years=2), NOW - relativedelta(years=1)
    query(url, '''UPDATE "Invoices" SET period_start=%s,period_end=%s
        WHERE id=%s''', (start, end, intent.invoiceId))
    query(url, '''UPDATE subscriptions SET billing_mode='annual_prepaid',plan_type='annual',status='cancelled',
        renewal_opt_out=true,cancellation_reason='previous lifecycle',current_period_start=%s,current_period_end=%s,
        cancellation_requested_at=%s WHERE user_id=%s''',
        (start, NOW + relativedelta(years=3) if staleCache else end, start, intent.userId))
    return query(url, 'SELECT * FROM subscriptions WHERE user_id=%s', (intent.userId,), one=True)
