from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260911120000_create_admin_free_trial_reductions.sql"
)


def migrationSql():
    assert MIGRATION.exists(), "trial-reduction migration is missing"
    return MIGRATION.read_text(encoding="utf-8").lower()


def test_trial_reduction_ledger_enforces_idempotency_and_result_shapes():
    sql = migrationSql()

    assert "create table public.admin_free_trial_reductions" in sql
    assert "idempotency_key uuid not null unique" in sql
    assert "request_hash text not null" in sql
    assert "reason text not null" in sql
    assert "char_length(btrim(reason)) between 1 and 1000" in sql
    assert "check (days between 1 and 30)" in sql
    assert "outcome in ('pending', 'reduced', 'failed')" in sql
    assert "days_removed is null or days_removed between 1 and 30" in sql
    assert "outcome = 'reduced'" in sql
    assert "new_expiry is not null" in sql
    assert "days_removed = days" in sql
    assert "previous_expiry - new_expiry = days * interval '1 day'" in sql


def test_trial_reduction_ledger_is_service_role_only_and_erasure_safe():
    sql = migrationSql()

    assert (
        "alter table public.admin_free_trial_reductions enable row level security"
        in sql
    )
    assert "from anon, authenticated" in sql
    assert "to service_role" in sql
    assert "on delete set null" in sql
