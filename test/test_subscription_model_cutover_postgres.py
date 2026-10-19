"""Actual current-shaped, disposable database; a skip remains UNVERIFIED."""
from contextlib import closing
from pathlib import Path
import json
import os

import psycopg2
import pytest

from test.test_subscription_model_postgres import modelDb, seed, decision, financial  # noqa: F401
from test.subscriptionBackfillFixtures import NOW, OWNER

pytestmark = pytest.mark.skipif(os.environ.get('RUN_MANUAL_BILLING_INTEGRATION') != '1',
    reason='Guarded disposable PostgreSQL opt-in; skip is UNVERIFIED')


@pytest.fixture
def retainedDb(modelDb):
    # Actual committed retention-table DDL, with the smallest referenced parent.
    factory,_=modelDb
    root=Path(__file__).resolve().parents[1]
    prefix=(root/'supabase/migrations/20261018081215_complete_subscription_erasure_retention.sql').read_text(encoding='utf-8').split(
        'CREATE OR REPLACE FUNCTION public.guard_subscription_model_evidence()',1)[0]
    with closing(factory()) as db,db,db.cursor() as cursor:
        cursor.execute('DROP SCHEMA IF EXISTS private CASCADE')
        cursor.execute('CREATE TABLE public.user_erasure_requests(id uuid PRIMARY KEY)')
        cursor.execute(prefix)
    yield modelDb
    with closing(factory()) as db,db,db.cursor() as cursor:
        cursor.execute('DROP SCHEMA IF EXISTS private CASCADE')


def collect(factory):
    from scripts.subscription_model_cutover import collectState
    return collectState(factory)


def test_real_review_archive_and_finances_are_observed_without_writes(modelDb):
    factory,repo=modelDb;seed(factory)
    repo.applyReviewedDecision(decision(repo),now=NOW)
    before=financial(factory)
    first=collect(factory); second=collect(factory)
    assert first==second and financial(factory)==before
    from scripts.subscription_model_cutover import assessCutover
    result=assessCutover(first,phase='v2',sourceDigest='synthetic-source',now=NOW)
    assert result['counts']['archiveFailures']==0
    assert result['counts']['authorityFailures']==0
    assert result['counts']['authorityMissing']==0
    assert result['financial']['Invoices']['count']==1
    assert result['financial']['credit_balances']['count']==1
    assert 'ERASURE_RETENTION_UNAVAILABLE' in result['blockers']
    assert 'EXTERNAL_ACCEPTANCE_INCOMPLETE' in result['blockers']
    assert OWNER['userId'] not in json.dumps(result)


def test_missing_archive_and_authority_block_actual_unreviewed_rows(modelDb):
    factory,repo=modelDb;seed(factory)
    from scripts.subscription_model_cutover import assessCutover
    result=assessCutover(collect(factory),phase='v2',sourceDigest='synthetic',now=NOW)
    assert result['counts']['archiveFailures']==1
    assert 'OWNED_MODEL_AUTHORITY_MISSING' in result['blockers']


def test_unknown_view_changes_dependency_review_and_financial_change_changes_digest(modelDb):
    factory,repo=modelDb;seed(factory);repo.applyReviewedDecision(decision(repo),now=NOW)
    first=collect(factory)
    with closing(factory()) as db,db,db.cursor() as cursor:
        cursor.execute('CREATE VIEW public.unreviewed_subscription_status AS SELECT id,status FROM subscriptions')
    second=collect(factory)
    assert first['dependencies']!=second['dependencies']
    assert first['financial']==second['financial']
    with closing(factory()) as db,db,db.cursor() as cursor:
        cursor.execute('UPDATE credit_balances SET topup_tokens=topup_tokens+1')
    assert second['financial']!=collect(factory)['financial']


def test_historical_tools_refuse_v2_columns_even_with_legacy_env(modelDb,monkeypatch,tmp_path,capsys):
    factory,repo=modelDb;seed(factory);before=financial(factory)
    with closing(factory()) as db:
        monkeypatch.setenv('DATABASE_URL',db.dsn)
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE','legacy')
    from scripts.manual_billing_inventory import main as inventory
    from scripts.manual_billing_backfill import main as backfill,applyMapping
    assert inventory(['--json'])==2
    assert backfill(['--dry-run'])==2
    path=tmp_path/'synthetic-mapping.json'
    path.write_text(json.dumps({'reviewed':True,'approved_by':'synthetic','mappings':[]}),encoding='utf-8')
    with pytest.raises(ValueError,match='SUBSCRIPTION_MODEL_V2_STORAGE'):
        applyMapping(str(path))
    assert financial(factory)==before
    assert OWNER['userId'] not in capsys.readouterr().out


def test_cli_uses_redacted_report_and_read_only_connection(modelDb,monkeypatch,capsys):
    factory,repo=modelDb;seed(factory);repo.applyReviewedDecision(decision(repo),now=NOW)
    with closing(factory()) as db:
        monkeypatch.setenv('SUBSCRIPTION_MODEL_CUTOVER_DATABASE_URL',db.dsn)
    from scripts.subscription_model_cutover import main
    assert main(['--check','--phase','v2'])==1
    result=json.loads(capsys.readouterr().out)
    assert result['readOnly'] and not result['authorizesCutover']
    assert not result['readyForOperatorReview']
    assert OWNER['userId'] not in json.dumps(result)


def test_collector_enforces_readonly_before_runtime_authority_reads(modelDb,monkeypatch):
    factory,repo=modelDb;seed(factory)
    from api.services.subscriptions.subscriptionModelRepository import SubscriptionModelRepository
    def write(self,cursor,*args):
        cursor.execute('UPDATE public.subscriptions SET version=version+1')
    monkeypatch.setattr(SubscriptionModelRepository,'loadRuntimeFacts',write)
    with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
        collect(factory)


def test_dependency_review_output_is_explicit_inspectable_and_never_overwrites(modelDb,monkeypatch,tmp_path,capsys):
    factory,repo=modelDb;seed(factory)
    with closing(factory()) as db:
        monkeypatch.setenv('SUBSCRIPTION_MODEL_CUTOVER_DATABASE_URL',db.dsn)
    from scripts.subscription_model_cutover import main
    path=tmp_path/'private-dependency-review.json'
    args=['--check','--phase','v2','--dependency-output',str(path)]
    assert main(args)==1
    review=json.loads(path.read_text(encoding='utf-8'))
    assert review['dependencies'] and review['sourceDigest'] and review['stateDigest']
    assert review['storageObjects'] and 'retentionImmutable' in review['protections']
    assert all(d['identity'] for d in review['dependencies'])
    assert OWNER['userId'] not in json.dumps(review)
    original=path.read_bytes()
    assert main(args)==2 and path.read_bytes()==original
    assert 'originalRow' not in capsys.readouterr().out


def test_identity_foreign_keys_and_policies_are_bound_to_the_state_review(modelDb):
    from psycopg2 import sql
    factory,repo=modelDb;seed(factory)
    first=collect(factory)
    assert first['storageObjects']
    with closing(factory()) as db,db,db.cursor() as cursor:
        cursor.execute("""SELECT conname FROM pg_constraint WHERE conrelid='public.\"Invoices\"'::regclass
            AND confrelid='public.subscriptions'::regclass""")
        constraint=cursor.fetchone()[0]
        cursor.execute(sql.SQL('ALTER TABLE public."Invoices" DROP CONSTRAINT {}').format(sql.Identifier(constraint)))
    second=collect(factory)
    assert first['financial']==second['financial']
    assert first['dependencies']==second['dependencies']
    assert first['storageObjects']!=second['storageObjects']


def restrictedFactory(factory, *, bypass=False, retainedRecords=True):
    from psycopg2 import sql
    role='subscription_cutover_auditor' if bypass else 'subscription_cutover_restricted'
    with closing(factory()) as db,db,db.cursor() as cursor:
        cursor.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,))
        if not cursor.fetchone():
            cursor.execute(sql.SQL('CREATE ROLE {} NOLOGIN {}').format(sql.Identifier(role),sql.SQL('BYPASSRLS' if bypass else 'NOBYPASSRLS')))
        cursor.execute(sql.SQL('GRANT USAGE ON SCHEMA public,private TO {}').format(sql.Identifier(role)))
        cursor.execute(sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}').format(sql.Identifier(role)))
        cursor.execute(sql.SQL('GRANT SELECT ON private.retained_user_subjects TO {}').format(sql.Identifier(role)))
        if retainedRecords:
            cursor.execute(sql.SQL('GRANT SELECT ON private.retained_user_records TO {}').format(sql.Identifier(role)))
    def connect():
        db=factory()
        with db.cursor() as cursor:
            cursor.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)))
        db.commit()
        return db
    return connect


def test_row_security_cannot_silently_hide_subscription_or_financial_rows(retainedDb):
    factory,_=retainedDb;seed(factory)
    with pytest.raises(psycopg2.errors.InsufficientPrivilege,match='row-level security'):
        collect(restrictedFactory(factory))


def test_hidden_retained_financial_table_is_not_silently_omitted(retainedDb):
    factory,_=retainedDb;seed(factory)
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        collect(restrictedFactory(factory,bypass=True,retainedRecords=False))


@pytest.mark.parametrize('change',['disable_trigger','drop_trigger','replace_guard','drop_hash_constraint'])
def test_retention_protections_are_mandatory_and_bound_to_existing_evidence(retainedDb,change):
    from scripts.subscription_model_cutover import assessCutover
    from test.test_subscription_model_cutover import approved
    factory,repo=retainedDb;seed(factory);repo.applyReviewedDecision(decision(repo),now=NOW)
    first=collect(factory);evidence=approved(first)
    before=assessCutover(first,phase='v2',sourceDigest='synthetic-source',now=NOW,evidence=evidence)
    assert before['readyForOperatorReview']
    with closing(factory()) as db,db,db.cursor() as cursor:
        if change=='disable_trigger':cursor.execute('ALTER TABLE private.retained_user_records DISABLE TRIGGER retained_records_immutable')
        elif change=='drop_trigger':cursor.execute('DROP TRIGGER retained_records_no_truncate ON private.retained_user_records')
        elif change=='replace_guard':cursor.execute("""CREATE OR REPLACE FUNCTION private.guard_retained_erasure_records()
            RETURNS trigger LANGUAGE plpgsql SET search_path='' AS $$ BEGIN RETURN NEW; END $$""")
        else:
            from psycopg2 import sql
            cursor.execute("""SELECT conname FROM pg_constraint WHERE conrelid='private.retained_user_records'::regclass
                AND contype='c' AND pg_get_constraintdef(oid) LIKE '%sha256%'""")
            name=cursor.fetchone()[0]
            cursor.execute(sql.SQL('ALTER TABLE private.retained_user_records DROP CONSTRAINT {}').format(sql.Identifier(name)))
    second=collect(factory)
    result=assessCutover(second,phase='v2',sourceDigest='synthetic-source',now=NOW,evidence=evidence)
    assert second['financial']==first['financial']
    assert 'REVIEW_STATE_CHANGED' in result['blockers']
    if change!='replace_guard':assert 'STORAGE_PROTECTION_MISSING' in result['blockers']
