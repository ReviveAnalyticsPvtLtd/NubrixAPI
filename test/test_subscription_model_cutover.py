"""Readiness evidence must fail closed; no test uses customer data."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from test.subscriptionBackfillFixtures import NOW, source
from api.services.subscriptions.subscriptionModelBackfill import legacyRow, legacyRowHash


def inputs():
    from scripts.subscription_model_cutover import TARGET_COLUMNS, RETIRED_COLUMNS
    row, owner, _, _ = source('none')
    row.update(plan_code=None, lifecycle_id=None, trial_started_at=None,
               trial_ends_at=None, trial_closed_at=None, cancellation_requested_at=None)
    archive = dict(id='archive', user_id=row['user_id'], subscription_id=row['id'],
        event_type='subscription.legacy_snapshot', event_status='COMMITTED', metadata_json={'archive': {
            'schemaVersion': 2, 'legacyHash': legacyRowHash(row), 'originalRow': legacyRow(row)}})
    initialized = dict(id='native', user_id=row['user_id'], subscription_id=row['id'],
        event_type='subscription.model_initialized', event_status='COMMITTED', metadata_json={'schemaVersion': 2})
    state = dict(columns=sorted(TARGET_COLUMNS | RETIRED_COLUMNS), rows=[row], owners=[owner['userId']],
        events=[archive, initialized], authorityFailures=0, retentionAvailable=True,
        dependencies=[{'kind':'index', 'identity':'synthetic-index', 'definition':'synthetic-definition'}],
        financial={'Invoices': {'count':0,'digest':'synthetic-invoices'},
                   'credit_balances': {'count':0,'digest':'synthetic-balances'}},
        protections={'rls':True,'canonicalUnique':True,'updatedAtTrigger':True,'retentionImmutable':True})
    return state


def approved(state):
    from scripts.subscription_model_cutover import assessCutover, REQUIRED_CHECKS
    draft = assessCutover(state, phase='v2', sourceDigest='synthetic-source', now=NOW)
    return dict(schemaVersion=1, reviewedBy='synthetic-reviewer', reviewReference='synthetic-review',
        issuedAt=NOW.isoformat(), expiresAt=(NOW+timedelta(hours=1)).isoformat(),
        stateDigest=draft['stateDigest'], sourceDigest=draft['sourceDigest'],
        reviewedDependencies=draft['dependencyDigests'],
        checks={check:{'passed':True,'reference':'synthetic-evidence'} for check in REQUIRED_CHECKS})


def report(state, evidence=None, phase='v2'):
    from scripts.subscription_model_cutover import assessCutover
    return assessCutover(state, phase=phase, sourceDigest='synthetic-source', now=NOW, evidence=evidence)


def test_matching_complete_evidence_only_allows_operator_review_not_ddl():
    state=inputs(); before=deepcopy(state)
    result=report(state, approved(state))
    assert result['readyForOperatorReview'] and not result['blockers']
    assert result['readOnly'] and result['authorizesCutover'] is False
    assert state==before
    encoded=json.dumps(result)
    assert state['rows'][0]['user_id'] not in encoded
    assert 'originalRow' not in encoded and 'synthetic-definition' not in encoded


@pytest.mark.parametrize('change,expected',[
    ('missing_archive','LEGACY_ARCHIVE_MISSING_OR_CHANGED'),
    ('changed_archive','LEGACY_ARCHIVE_MISSING_OR_CHANGED'),
    ('archive_owner','LEGACY_ARCHIVE_MISSING_OR_CHANGED'),
    ('changed_legacy','LEGACY_ARCHIVE_MISSING_OR_CHANGED'),
    ('missing_authority','OWNED_MODEL_AUTHORITY_MISSING'),
    ('unresolved_trial','TRIAL_HISTORY_UNRESOLVED'),
    ('duplicate_canonical','CANONICAL_OWNER_INVALID'),
    ('missing_owner','CANONICAL_OWNER_INVALID'),
    ('authority_failure','RUNTIME_AUTHORITY_UNAVAILABLE'),
    ('retention','ERASURE_RETENTION_UNAVAILABLE'),
    ('rls','STORAGE_PROTECTION_MISSING'),
    ('retention_guard','STORAGE_PROTECTION_MISSING'),
    ('extra_column','UNEXPECTED_SUBSCRIPTION_SCHEMA'),
    ('new_dependency','UNREVIEWED_DEPENDENCIES'),
    ('money','REVIEW_STATE_CHANGED'),
])
def test_real_state_changes_block_even_with_previously_approved_evidence(change,expected):
    state=inputs(); evidence=approved(state)
    if change=='missing_archive': state['events'].pop(0)
    elif change=='changed_archive': state['events'][0]['metadata_json']['archive']['originalRow']['billing_state']={}
    elif change=='archive_owner': state['events'][0]['user_id']='different-owner'
    elif change=='changed_legacy': state['rows'][0]['billing_state']={'lost':'evidence'}
    elif change=='missing_authority': state['events'].pop()
    elif change=='unresolved_trial':
        state['events'][-1].update(event_type='subscription.model_backfill', metadata_json={'backfill':{
            'archiveId':'archive','resolution':'consumed_dates_unknown','trialConsumed':True}})
    elif change=='duplicate_canonical': state['rows'].append({**state['rows'][0], 'id':'duplicate'})
    elif change=='missing_owner': state['owners']=[]
    elif change=='authority_failure': state['authorityFailures']=1
    elif change=='retention': state['retentionAvailable']=False
    elif change=='rls': state['protections']['rls']=False
    elif change=='retention_guard': state['protections']['retentionImmutable']=False
    elif change=='extra_column': state['columns'].append('unknown_column')
    elif change=='new_dependency': state['dependencies'].append({'kind':'view','identity':'unknown','definition':'unknown'})
    elif change=='money': state['financial']['Invoices']['count']=1
    result=report(state,evidence)
    assert expected in result['blockers'] and not result['readyForOperatorReview']


@pytest.mark.parametrize('phase',['legacy','shadow','dual','contracted'])
def test_expanded_storage_requires_v2_phase(phase):
    state=inputs(); assert 'PHASE_NOT_V2' in report(state,approved(state),phase)['blockers']


@pytest.mark.parametrize('phase',['legacy','shadow','dual','v2','contracted'])
def test_already_contracted_storage_never_certifies_a_pending_drop(phase):
    from scripts.subscription_model_cutover import TARGET_COLUMNS
    state=inputs();state['columns']=sorted(TARGET_COLUMNS)
    assert 'STORAGE_ALREADY_CONTRACTED' in report(state,approved(state),phase)['blockers']


@pytest.mark.parametrize('change',['missing','stale','future','unbounded','source','state','reviewer','boolean','reference'])
def test_external_evidence_must_be_fresh_exact_and_explicit(change):
    state=inputs(); evidence=approved(state)
    if change=='missing': evidence=None
    elif change=='stale': evidence['expiresAt']=(NOW-timedelta(seconds=1)).isoformat()
    elif change=='future': evidence['issuedAt']=(NOW+timedelta(seconds=1)).isoformat()
    elif change=='unbounded': evidence['expiresAt']=(NOW+timedelta(days=365)).isoformat()
    elif change=='source': evidence['sourceDigest']='different-source'
    elif change=='state': evidence['stateDigest']='different-state'
    elif change=='reviewer': evidence['reviewedBy']=''
    elif change=='boolean': next(iter(evidence['checks'].values()))['passed']='true'
    elif change=='reference': next(iter(evidence['checks'].values()))['reference']=''
    assert not report(state,evidence)['readyForOperatorReview']


@pytest.mark.parametrize('tool',['inventory','backfill','planner'])
@pytest.mark.parametrize('phase',['dual','v2','contracted'])
def test_legacy_cli_refuses_unified_phase_before_connecting(monkeypatch,capsys,tool,phase):
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE',phase)
    monkeypatch.setenv('DATABASE_URL','not-a-real-dsn')
    import psycopg2
    monkeypatch.setattr(psycopg2,'connect',lambda *a,**kw:pytest.fail('Must refuse before connecting'))
    if tool=='inventory':
        from scripts.manual_billing_inventory import main
        assert main(['--json'])==2
    elif tool=='backfill':
        from scripts.manual_billing_backfill import main
        assert main(['--dry-run'])==2
    else:
        from scripts.manual_billing_migration_plan import buildMigrationPlan
        with pytest.raises(ValueError,match='SUBSCRIPTION_MODEL_V2_STORAGE'):
            buildMigrationPlan(set(),'expand')


def test_new_check_requires_dedicated_dsn_and_never_uses_application_url(monkeypatch,capsys):
    from scripts.subscription_model_cutover import main
    monkeypatch.delenv('SUBSCRIPTION_MODEL_CUTOVER_DATABASE_URL',raising=False)
    monkeypatch.setenv('DATABASE_URL','must-not-be-used')
    assert main(['--check','--phase','v2'])==2
    assert 'must-not-be-used' not in capsys.readouterr().out


def test_authority_links_to_original_review_even_after_retained_fact_change_and_fresh_archive():
    state=inputs(); row=state['rows'][0]
    state['events'][-1].update(event_type='subscription.model_backfill',metadata_json={'backfill':{
        'archiveId':'archive','resolution':'verified','reviewedBy':'synthetic','reviewReference':'synthetic'}})
    row['renewal_opt_out']=True
    latest=deepcopy(state['events'][0]);latest['id']='fresh-checkpoint'
    latest['metadata_json']['archive'].update(originalRow=legacyRow(row),legacyHash=legacyRowHash(row))
    state['events'].append(latest)
    assert report(state,approved(state))['readyForOperatorReview']


@pytest.mark.parametrize('name',['supervisord.conf','Dockerfile','.github/workflows/subscriptionCron.yaml',
    'config/tax_rules.json','supabase/functions/billingEmail/index.ts','new-worker-launcher.sh'])
def test_source_evidence_covers_runtime_configuration_and_non_python_consumers(tmp_path,name):
    import subprocess
    from scripts.subscription_model_cutover import sourceTreeDigest
    subprocess.run(['git','init','-q',str(tmp_path)],check=True,capture_output=True)
    path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('before',encoding='utf-8')
    before=sourceTreeDigest(tmp_path)
    path.write_text('after',encoding='utf-8')
    assert before!=sourceTreeDigest(tmp_path)


def test_source_manifest_excludes_secrets_dependencies_and_build_output(tmp_path):
    import subprocess
    from scripts.subscription_model_cutover import sourceTreeDigest
    subprocess.run(['git','init','-q',str(tmp_path)],check=True,capture_output=True)
    before=sourceTreeDigest(tmp_path)
    for name in ('.env','.env.local','private.env','node_modules/generated.js','.venv/generated.py','__pycache__/generated.pyc','.next/generated.js'):
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('synthetic-not-a-secret',encoding='utf-8')
    assert before==sourceTreeDigest(tmp_path)
