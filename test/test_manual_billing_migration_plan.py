import pytest


def test_expansion_excludes_older_contract_and_includes_forward_notification_chain():
    from scripts.manual_billing_migration_plan import buildMigrationPlan
    names=[path.name for path in buildMigrationPlan(set(),'expand')]
    assert '20261005195626_contract_recurring_billing_fields.sql' not in names
    assert '20261005195635_extend_monthly_notifications.sql' in names
    assert '20261006173531_fence_billing_notification_revisions.sql' in names
    assert '20261007154210_index_unresolved_cycle_money.sql' in names
    assert names==sorted(names)


def test_contract_refuses_missing_expansion_versions():
    from scripts.manual_billing_migration_plan import buildMigrationPlan
    with pytest.raises(ValueError,match='EXPANSION_NOT_APPLIED'):
        buildMigrationPlan(set(),'contract')
    applied={path.name.split('_')[0] for path in buildMigrationPlan(set(),'expand')}
    assert [path.name for path in buildMigrationPlan(applied,'contract')]==['20261005195626_contract_recurring_billing_fields.sql']
    applied.add('20261005195626')
    assert buildMigrationPlan(applied,'contract')==[]


def test_migration_plan_rejects_unknown_phase():
    from scripts.manual_billing_migration_plan import buildMigrationPlan
    with pytest.raises(ValueError,match='INVALID_MIGRATION_PHASE'):
        buildMigrationPlan(set(),'all')


def test_contract_migration_is_outside_the_chronological_migrations_folder():
    # A blanket `supabase db push` must never apply the contraction mid-chain.
    from pathlib import Path
    from scripts.manual_billing_migration_plan import buildMigrationPlan
    assert not list(Path('supabase/migrations').glob('*_contract_recurring_billing_fields.sql'))
    applied={path.name.split('_')[0] for path in buildMigrationPlan(set(),'expand')}
    [contract]=buildMigrationPlan(applied,'contract')
    assert contract.parent.name=='manual_billing_contract' and contract.is_file()
