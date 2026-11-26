"""Replay repository migrations against a disposable synthetic legacy catalog."""

import os
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

import psycopg2
import pytest
from psycopg2.extensions import parse_dsn

from test.test_manual_billing_postgres_integration import BASELINE


MIGRATIONS = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
LEGACY_SCHEMA = Path(__file__).with_name("fixtures") / "migration_rehearsal_legacy.sql"


def test_tracked_migration_chain_and_catalog_contracts():
    url = os.environ.get("MIGRATION_REHEARSAL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires the disposable migration_rehearsal_test database")

    uri = urlsplit(url)
    parameters = parse_dsn(url)
    assert uri.scheme in {"postgres", "postgresql"} and not uri.query
    assert parameters.get("host") in {"localhost", "127.0.0.1"}
    assert parameters.get("dbname") == "migration_rehearsal_test"

    with closing(psycopg2.connect(url)) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('public.subscriptions') IS NULL")
        assert cursor.fetchone()[0], "The rehearsal requires a fresh fixture database"
        cursor.execute(BASELINE)
        cursor.execute(LEGACY_SCHEMA.read_text(encoding="utf-8"))
        cursor.execute("CREATE PUBLICATION supabase_realtime")

        migrations = sorted(MIGRATIONS.glob("*.sql"))
        assert migrations, "No migrations found"
        for migration in migrations:
            try:
                cursor.execute(migration.read_text(encoding="utf-8"))
            except psycopg2.Error as exc:
                pytest.fail(f"{migration.name}: {exc}", pytrace=False)

        cursor.execute("""
            SELECT
              to_regclass('public.user_erasure_batches') IS NULL,
              to_regclass('private.retained_user_subjects') IS NOT NULL,
              to_regclass('private.payment_recovery_runtime') IS NOT NULL,
              to_regclass('public.payment_slots') IS NOT NULL,
              EXISTS (
                SELECT 1 FROM pg_publication_tables
                WHERE pubname = 'supabase_realtime'
                  AND schemaname = 'public' AND tablename = 'pricing_catalog_state'
              ),
              EXISTS (
                SELECT 1 FROM pg_class
                WHERE oid = 'public.page_visits'::regclass AND relrowsecurity
              ),
              has_table_privilege('service_role', 'public.page_visits', 'INSERT'),
              has_table_privilege('service_role', 'public.page_visits', 'SELECT'),
              NOT has_table_privilege('service_role', 'public.page_visits', 'UPDATE'),
              NOT has_table_privilege('anon', 'public.page_visits', 'SELECT'),
              (SELECT user_id FROM public."WebhookEvents") = 'migration-fixture-user'
        """)
        assert cursor.fetchone() == (True,) * 11
        # Closing the connection rolls back the rehearsal. The separate release
        # contract directory is outside the migration chain by design.
