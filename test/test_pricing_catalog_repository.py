"""Publication behavior against real PostgreSQL, including competing admins."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from types import SimpleNamespace

import psycopg2
import pytest

from api.services.billing.pricingCatalogContracts import CatalogConflict, CatalogUnavailable
from api.services.billing.pricingCatalogRepository import PricingCatalogRepository
from test.test_pricing_catalog_contracts import BASELINE
from test.test_pricing_catalog_postgres import database

ADMIN = SimpleNamespace(adminId='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', email='pricing@example.test', sessionId='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')


@pytest.fixture()
def repo(database):
    with psycopg2.connect(database) as conn, conn.cursor() as cursor:
        cursor.execute('TRUNCATE private.pricing_catalog_publications, private.pricing_catalog_prices, private.pricing_catalog_revisions RESTART IDENTITY CASCADE')
        cursor.execute('DELETE FROM public.admin_audit_log')
        cursor.execute("INSERT INTO private.pricing_catalog_revisions(admin_email,reason) VALUES('system','Initial pricing catalog')")
        cursor.executemany('INSERT INTO private.pricing_catalog_prices(revision,currency,sku,amount_minor) VALUES(1,%s,%s,%s)', [(c,s,a) for c,v in BASELINE.items() for s,a in v.items()])
        cursor.execute('INSERT INTO public.pricing_catalog_state(id,revision) VALUES(1,1)')
    return PricingCatalogRepository(connectionFactory=lambda: psycopg2.connect(database))


def publish(repo, key='a', expected=1, prices=None):
    changed = deepcopy(BASELINE) if prices is None else prices
    if prices is None: changed['USD']['monthly'] = 2500
    return repo.publish(admin=ADMIN, expectedRevision=expected, reason='Regional review', prices=changed, idempotencyKey=key)


def test_reads_are_immutable_and_caller_connection_is_retained(repo, database):
    with psycopg2.connect(database) as connection:
        catalog = repo.loadCurrent(connection=connection, forShare=True)
        assert catalog.prices == BASELINE
        with pytest.raises(TypeError): catalog.prices['USD']['monthly'] = 1
        assert not connection.closed
    assert repo.loadRevision(1) == catalog


@pytest.mark.parametrize('no_op', [False, True])
def test_identical_retry_retains_original_result_after_publish_and_audit_retention(repo, database, no_op):
    original_prices = deepcopy(BASELINE)
    if not no_op: original_prices['USD']['monthly'] = 2500
    first = publish(repo, prices=original_prices)
    assert first.noOp is no_op
    second_prices = deepcopy(original_prices)
    second_prices['USD']['small'] = 600
    second = publish(repo, key='b', expected=first.catalog.revision, prices=second_prices)
    with psycopg2.connect(database) as conn, conn.cursor() as cursor:
        cursor.execute('DELETE FROM public.admin_audit_log')
    assert publish(repo, prices=original_prices) == first
    assert repo.loadCurrent() == second.catalog
    with pytest.raises(CatalogConflict, match='IDEMPOTENCY_CONFLICT'):
        publish(repo, prices=second_prices)


def test_no_op_does_not_allocate_a_revision(repo, database):
    result = publish(repo, prices=BASELINE)
    assert result.noOp and result.catalog.revision == 1
    with psycopg2.connect(database) as conn, conn.cursor() as cursor:
        cursor.execute('SELECT count(*) FROM private.pricing_catalog_revisions')
        assert cursor.fetchone()[0] == 1


def test_simultaneous_admins_cannot_overwrite_each_other(repo):
    barrier = Barrier(2)
    def attempt(key):
        barrier.wait(timeout=5)
        try: return publish(repo, key=key)
        except CatalogConflict as exc: return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ['first', 'second']))
    assert sum(result == 'PRICING_CONFLICT' for result in results) == 1
    assert repo.loadCurrent().prices['USD']['monthly'] == 2500


@pytest.mark.parametrize('table', ['public.admin_audit_log','private.pricing_catalog_prices'])
def test_failed_write_rolls_back_prices_pointer_and_replay_ledger(repo, database, table):
    with psycopg2.connect(database) as conn, conn.cursor() as cursor:
        cursor.execute("CREATE OR REPLACE FUNCTION public.reject_pricing_audit() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN RAISE EXCEPTION 'write unavailable'; END $$")
        condition = " WHEN (NEW.sku='large' AND NEW.currency='USD')" if table.endswith('pricing_catalog_prices') else ''
        cursor.execute(f'CREATE TRIGGER reject_pricing_audit BEFORE INSERT ON {table} FOR EACH ROW{condition} EXECUTE FUNCTION public.reject_pricing_audit()')
    try:
        with pytest.raises(CatalogUnavailable): publish(repo)
        assert repo.loadCurrent().revision == 1
        with psycopg2.connect(database) as conn, conn.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM private.pricing_catalog_publications')
            assert cursor.fetchone()[0] == 0
            cursor.execute('SELECT count(*) FROM private.pricing_catalog_prices')
            assert cursor.fetchone()[0] == 10
    finally:
        with psycopg2.connect(database) as conn, conn.cursor() as cursor:
            cursor.execute(f'DROP TRIGGER reject_pricing_audit ON {table}')
            cursor.execute('DROP FUNCTION public.reject_pricing_audit()')


def test_history_has_bounded_cursor_pages_without_duplicates(repo):
    expected = 1
    for index in range(24):
        prices = deepcopy(BASELINE)
        prices['USD']['small'] += index + 1
        expected = publish(repo, key=str(index), expected=expected, prices=prices).catalog.revision
    first, cursor = repo.listHistory()
    second, end = repo.listHistory(beforeRevision=cursor)
    assert len(first) == 20 and len(second) == 5 and end is None
    assert len({entry.revision for entry in first + second}) == 25
    assert repo.loadRevision(1).prices == BASELINE


def test_unavailable_storage_is_not_replaced_with_any_other_price():
    def unavailable(): raise psycopg2.OperationalError('unavailable')
    with pytest.raises(CatalogUnavailable): PricingCatalogRepository(connectionFactory=unavailable).loadCurrent()
