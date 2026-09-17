from pathlib import Path


def _migrationSql() -> str:
    migrations = list(
        Path("supabase/migrations").glob("*_create_notification_deliveries.sql")
    )
    assert len(migrations) == 1, "notification delivery migration is missing"
    return migrations[0].read_text(encoding="utf-8").lower()


def _hardeningSql() -> str:
    migrations = list(
        Path("supabase/migrations").glob("*_harden_notification_claims.sql")
    )
    assert len(migrations) == 1, "notification claim hardening is missing"
    return migrations[0].read_text(encoding="utf-8").lower()


def test_notificationOutboxHasDedupeLeasesAndSeparateProviderPolling():
    sql = _migrationSql()

    assert "create table public.notification_deliveries" in sql
    assert "dedupe_key text not null unique" in sql
    assert "attempt_count integer not null default 0" in sql
    assert "next_attempt_at timestamptz not null default now()" in sql
    assert "next_reconcile_at timestamptz" in sql
    assert "lease_owner text" in sql
    assert "lease_expires_at timestamptz" in sql
    assert "provider_message_id text" in sql
    assert "metadata_json jsonb not null default '{}'::jsonb" in sql


def test_claimFunctionIsAtomicAndRecoversExpiredLeases():
    sql = _migrationSql()

    assert "create or replace function public.claim_notification_deliveries" in sql
    assert "for update skip locked" in sql
    assert "status in ('pending', 'retry_pending')" in sql
    assert "lease_expires_at <= now()" in sql
    assert "attempt_count = delivery.attempt_count + 1" in sql


def test_notificationOutboxIsServiceRoleOnly():
    sql = _migrationSql()

    assert "enable row level security" in sql
    assert (
        "revoke all on table public.notification_deliveries "
        "from anon, authenticated"
    ) in sql
    assert "grant select, insert, update, delete" in sql
    assert "to service_role" in sql
    assert "revoke all on function public.claim_notification_deliveries" in sql
    assert "grant execute on function public.claim_notification_deliveries" in sql


def test_claimPreservesAmbiguousMarkerForFinalProviderLookup():
    sql = _hardeningSql()

    assert "create or replace function public.claim_notification_deliveries" in sql
    assert "delivery.last_error_code = 'ambiguous_send'" in sql
    assert "then delivery.last_error_code" in sql
