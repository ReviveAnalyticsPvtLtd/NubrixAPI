"""Opt-in real SQL checks; guarded disposable DB, never the application DB."""
import os
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import pytest

from test.test_pricing_catalog_contracts import BASELINE


@pytest.fixture(scope='module')
def database():
    url = os.environ.get('PRICING_TEST_DATABASE_URL')
    if not url: pytest.skip('Set PRICING_TEST_DATABASE_URL to a disposable local database')
    parsed = urlparse(url)
    assert parsed.hostname in ('127.0.0.1', 'localhost')
    assert parsed.path.startswith('/pricing_catalog_test_')
    connection = psycopg2.connect(url)
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute('DROP SCHEMA IF EXISTS private CASCADE')
        cursor.execute('DROP TABLE IF EXISTS public.pricing_catalog_state CASCADE')
        cursor.execute('DROP TABLE IF EXISTS public.admin_audit_log CASCADE')
        cursor.execute('CREATE TABLE IF NOT EXISTS public.admin_sessions(id uuid PRIMARY KEY)')
        cursor.execute(Path('supabase/migrations/20260817090000_create_admin_audit_log.sql').read_text())
        cursor.execute("ALTER TABLE public.admin_audit_log ADD COLUMN details jsonb NOT NULL DEFAULT '{}'::jsonb")
        cursor.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname='supabase_realtime') THEN CREATE PUBLICATION supabase_realtime; END IF; END $$")
        migrations = list(Path('supabase/migrations').glob('*_create_pricing_catalog.sql'))
        assert len(migrations) == 1, 'Catalog migration is required'
        cursor.execute(migrations[0].read_text())
    connection.close()
    return url


def test_seed_is_complete_and_unchanged(database):
    with psycopg2.connect(database) as connection, connection.cursor() as cursor:
        cursor.execute('SELECT currency, sku, amount_minor FROM private.pricing_catalog_prices WHERE revision=1')
        rows = cursor.fetchall()
        assert len(rows) == 10
        assert {(c, s): a for c, s, a in rows} == {(c, s): a for c, values in BASELINE.items() for s, a in values.items()}
        cursor.execute('SELECT id, revision FROM public.pricing_catalog_state')
        assert cursor.fetchall() == [(1, 1)]


@pytest.mark.parametrize('role', ['anon', 'authenticated'])
def test_browser_roles_can_only_read_notification_row(database, role):
    with psycopg2.connect(database) as connection, connection.cursor() as cursor:
        cursor.execute(f'SET LOCAL ROLE {role}')
        cursor.execute('SELECT revision FROM public.pricing_catalog_state')
        assert cursor.fetchall() == [(1,)]
    for sql in (
        'UPDATE public.pricing_catalog_state SET revision=1',
        'DELETE FROM public.pricing_catalog_state',
        'INSERT INTO public.pricing_catalog_state(id,revision) VALUES(2,1)',
        'SELECT * FROM private.pricing_catalog_revisions',
        'SELECT * FROM private.pricing_catalog_prices',
        'SELECT * FROM private.pricing_catalog_publications',
    ):
        with psycopg2.connect(database) as connection, connection.cursor() as cursor:
            cursor.execute(f'SET LOCAL ROLE {role}')
            with pytest.raises(psycopg2.errors.InsufficientPrivilege): cursor.execute(sql)


@pytest.mark.parametrize('currency,sku,amount', [('EUR','small',100),('USD','custom',100),('USD','small',0),('INR','small',99),('USD','small',9007199254740992)])
def test_database_rejects_invalid_price_rows(database, currency, sku, amount):
    connection = psycopg2.connect(database)
    try:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO private.pricing_catalog_revisions(admin_email,reason) VALUES('system','Constraint test') RETURNING revision")
            revision = cursor.fetchone()[0]
            with pytest.raises(psycopg2.IntegrityError):
                cursor.execute('INSERT INTO private.pricing_catalog_prices(revision,currency,sku,amount_minor) VALUES(%s,%s,%s,%s)', (revision,currency,sku,amount))
    finally:
        connection.rollback()
        connection.close()
