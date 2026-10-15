"""Actual invoice/publication serialization in a separate financial test DB."""
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Event
import uuid
from datetime import datetime, timezone
from dataclasses import replace

import psycopg2
import pytest

from api.services.billing.manualBillingContracts import CheckoutRequest
from api.services.billing.manualBillingRepository import ManualBillingRepository
from api.services.billing.pricingCatalogRepository import PricingCatalogRepository
from test.test_manual_billing_postgres_integration import postgres
from test.test_pricing_catalog_contracts import BASELINE
from test.test_pricing_catalog_repository import ADMIN

pytestmark = pytest.mark.skipif(os.environ.get('RUN_MANUAL_BILLING_INTEGRATION') != '1', reason='Disposable local DB required')


@pytest.fixture(scope='module')
def invoice_db(postgres):
    with psycopg2.connect(postgres) as connection, connection.cursor() as cursor:
        cursor.execute('DROP SCHEMA IF EXISTS private CASCADE')
        cursor.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname='supabase_realtime') THEN CREATE PUBLICATION supabase_realtime; END IF; END $$")
        migration = next(Path('supabase/migrations').glob('*_create_pricing_catalog.sql'))
        cursor.execute(migration.read_text())
    return postgres


@pytest.mark.parametrize('currency', ['INR','USD'])
def test_invoice_holds_one_revision_then_replays_after_publication(invoice_db,currency):
    repo = ManualBillingRepository(lambda:psycopg2.connect(invoice_db))
    catalog_repo = PricingCatalogRepository(lambda:psycopg2.connect(invoice_db))
    user = 'catalog-' + str(uuid.uuid4())
    with psycopg2.connect(invoice_db) as connection, connection.cursor() as cursor:
        cursor.execute('INSERT INTO public."Users"("userId") VALUES(%s)',(user,))
    repo.ensureCanonicalSubscription(user)
    assert repo.paidSubscriptionCurrency(user) is None
    current = catalog_repo.loadCurrent()
    request = CheckoutRequest(user,'initial_purchase','monthly_prepaid',{'domains':['banking']},'original',
                              currency=currency,expectedCatalogRevision=current.revision)
    invoice = repo.reserveCheckout(request)
    original_amount = invoice.amount
    changed = {c:dict(v) for c,v in current.prices.items()}
    changed[currency]['monthly'] += 100
    catalog_repo.publish(admin=ADMIN,expectedRevision=current.revision,reason='Local race check',prices=changed,idempotencyKey=str(uuid.uuid4()))
    assert repo.reserveCheckout(request).amount == original_amount
    stale = CheckoutRequest(user,'initial_purchase','monthly_prepaid',{'domains':['manufacturing']},'stale',
                            currency=currency,expectedCatalogRevision=current.revision)
    with pytest.raises(ValueError,match='PRICING_CHANGED'): repo.reserveCheckout(stale)
    with psycopg2.connect(invoice_db) as connection, connection.cursor() as cursor:
        cursor.execute('SELECT count(*) FROM public."Invoices" WHERE "userId"=%s',(user,))
        assert cursor.fetchone()[0] == 1


def test_shared_catalog_lock_blocks_publication_until_invoice_transaction_finishes(invoice_db):
    catalog_repo = PricingCatalogRepository(lambda:psycopg2.connect(invoice_db))
    connection = psycopg2.connect(invoice_db)
    current = catalog_repo.loadCurrent(connection=connection,forShare=True)
    started, finished = Event(), Event()
    changed = {c:dict(v) for c,v in current.prices.items()}
    changed['USD']['small'] += 1
    def publish():
        started.set()
        result = catalog_repo.publish(admin=ADMIN,expectedRevision=current.revision,reason='Serialized local publication',
            prices=changed,idempotencyKey=str(uuid.uuid4()))
        finished.set()
        return result
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(publish)
            assert started.wait(5)
            assert not finished.wait(.15)
            connection.commit()
            assert future.result(timeout=5).catalog.revision > current.revision
    finally: connection.close()


@pytest.mark.parametrize('mode,currency', [('monthly_prepaid','INR'),('monthly_prepaid','USD'),('annual_prepaid','INR'),('annual_prepaid','USD')])
def test_paid_additions_and_topups_freeze_current_revision(invoice_db,mode,currency):
    from api.services.billing.manualBillingContracts import VerifiedPaymentEvidence
    repo = ManualBillingRepository(lambda:psycopg2.connect(invoice_db))
    catalog_repo = PricingCatalogRepository(lambda:psycopg2.connect(invoice_db))
    current = catalog_repo.loadCurrent()
    user = 'paid-catalog-' + str(uuid.uuid4())
    with psycopg2.connect(invoice_db) as connection, connection.cursor() as cursor:
        cursor.execute('INSERT INTO public."Users"("userId") VALUES(%s)',(user,))
    repo.ensureCanonicalSubscription(user)
    initial = repo.reserveCheckout(CheckoutRequest(user,'initial_purchase',mode,{'domains':['banking']},'initial',currency=currency,expectedCatalogRevision=current.revision))
    order = 'order-' + str(uuid.uuid4())
    repo.bindProviderOrder(initial.attemptId,{'id':order})
    evidence = VerifiedPaymentEvidence(initial.attemptId,initial.invoiceId,user,order,'pay-'+str(uuid.uuid4()),
        'initial_purchase',currency,'captured','server_observation',initial.amount,datetime.now(timezone.utc),None,None,False)
    repo.finalizeCapturedPayment(evidence)
    assert repo.paidSubscriptionCurrency(user) == currency
    paid_price = repo.getPaidPeriodPrice(user)
    sku = 'monthly' if mode == 'monthly_prepaid' else 'annual'
    assert paid_price['amountBeforeTax'] == current.prices[currency][sku]
    assert paid_price['invoiceIds'] == [initial.invoiceId]
    for purpose,payload in [('expert_addition',{'domains':['manufacturing']}),('topup',{'packId':'small'})]:
        request = CheckoutRequest(user,purpose,mode,payload,purpose,currency=currency,expectedCatalogRevision=current.revision)
        intent = repo.reserveCheckout(request)
        assert intent.currency == currency and intent.snapshot['catalogRevision'] == current.revision
        assert intent.amount > 0
        assert repo.reserveCheckout(replace(request,expectedCatalogRevision=current.revision+100)).amount == intent.amount
        # Unpaid additions and credit purchases do not alter the paid-period sum.
        assert repo.getPaidPeriodPrice(user) == paid_price
        order = 'order-' + str(uuid.uuid4())
        repo.bindProviderOrder(intent.attemptId, {'id':order})
        captured = VerifiedPaymentEvidence(intent.attemptId,intent.invoiceId,user,order,'pay-'+str(uuid.uuid4()),
            purpose,currency,'captured','server_observation',intent.amount,datetime.now(timezone.utc),None,None,False)
        repo.finalizeCapturedPayment(captured)
        after = repo.getPaidPeriodPrice(user)
        if purpose == 'expert_addition':
            with psycopg2.connect(invoice_db) as connection, connection.cursor() as cursor:
                cursor.execute('SELECT amount_before_tax FROM public."Invoices" WHERE id=%s',(intent.invoiceId,))
                addition = cursor.fetchone()[0]
            assert after['amountBeforeTax'] == paid_price['amountBeforeTax'] + addition
            assert after['invoiceIds'] == sorted([initial.invoiceId,intent.invoiceId])
            paid_price = after
        else:
            assert after == paid_price
    changed = {c:dict(v) for c,v in current.prices.items()}
    changed[currency][sku] += 100
    catalog_repo.publish(admin=ADMIN,expectedRevision=current.revision,reason='Future quotes only',prices=changed,idempotencyKey=str(uuid.uuid4()))
    assert repo.getPaidPeriodPrice(user) == paid_price
