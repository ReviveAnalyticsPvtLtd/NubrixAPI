"""Only a guarded, explicitly opted-in synthetic localhost DB may be recreated."""
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor
import pytest

from api.services.subscriptions.subscriptionModel import SubscriptionAuthorityUnavailable
from api.services.subscriptions.subscriptionModelBackfill import NEW_COLUMNS, buildBackfillProposal, jsonValue
from api.services.subscriptions.subscriptionModelRepository import SubscriptionModelRepository
from api.services.subscriptions.subscriptionSnapshotService import SubscriptionSnapshotService
from test.subscriptionBackfillFixtures import NOW, OWNER, SUB, source

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'supabase/migrations/20261017211222_expand_subscription_model_v2.sql'
pytestmark = pytest.mark.skipif(os.environ.get('RUN_MANUAL_BILLING_INTEGRATION') != '1', reason='Disposable PostgreSQL opt-in; skip is UNVERIFIED')


@pytest.fixture
def modelDb():
    url = os.environ.get('SUBSCRIPTION_MODEL_TEST_DATABASE_URL', '')
    parsed = urlparse(url)
    assert parsed.hostname in ('127.0.0.1', 'localhost') and parsed.path.startswith('/manual_billing_test_')
    assert url != os.environ.get('DATABASE_URL')
    factory = lambda: psycopg2.connect(url)
    with closing(factory()) as db, db, db.cursor() as cursor:
        cursor.execute('DROP SCHEMA public CASCADE; CREATE SCHEMA public')
        for role in ('anon', 'authenticated', 'service_role'):
            cursor.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (role,))
            if cursor.fetchone() is None:
                cursor.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
        cursor.execute((ROOT / 'test/fixtures/subscription_model_current_shape.sql').read_text())
        cursor.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='subscriptions'")
        assert cursor.fetchone()[0] == 24
        cursor.execute(MIGRATION.read_text())
    return factory, SubscriptionModelRepository(factory)


def seed(factory, condition='paid'):
    row, owner, invoices, events = source(condition)
    with closing(factory()) as db, db, db.cursor() as cursor:
        cursor.execute('INSERT INTO public."Users"("userId") VALUES(%s)', (owner['userId'],))
        for table, records in [('subscriptions', [row]), ('Invoices', invoices), ('billing_events', events)]:
            for record in records:
                cursor.execute(sql.SQL('INSERT INTO public.{} ({}) VALUES ({})').format(sql.Identifier(table),
                    sql.SQL(',').join(map(sql.Identifier, record)), sql.SQL(',').join(sql.Placeholder() for _ in record)),
                    [Json(v) if isinstance(v, (dict, list)) else v for v in record.values()])
        amounts = (0, 0, 0, 0) if condition == 'none' else (700, 300, 2500, 1000)
        cursor.execute('INSERT INTO public.credit_balances VALUES(%s,%s,%s,%s,%s,%s)', (owner['userId'], row['id'], *amounts))
        cursor.execute('INSERT INTO public.admin_free_trial_reductions(subscription_id) VALUES(%s)', (row['id'],))
    return row


def decision(repo, **kwargs):
    result = repo.inventory(now=NOW, trialReviews=kwargs.get('trialReviews'))['proposals'][0]
    return result | {'reviewedBy': 'synthetic-reviewer', 'reviewReference': 'synthetic-review-case'}


def financial(factory):
    with closing(factory()) as db, db, db.cursor(cursor_factory=RealDictCursor) as cursor:
        result = {}
        for table in ('Invoices', 'credit_balances', 'admin_free_trial_reductions'):
            cursor.execute(sql.SQL('SELECT * FROM public.{} ORDER BY 1').format(sql.Identifier(table)))
            result[table] = jsonValue(cursor.fetchall())
        cursor.execute("SELECT * FROM public.billing_events WHERE event_type NOT IN ('subscription.legacy_snapshot','subscription.model_backfill') ORDER BY id")
        result['events'] = jsonValue(cursor.fetchall())
        return result


def test_current_shape_roundtrip_preserves_financial_identity(modelDb):
    factory, repo = modelDb
    seed(factory)
    before = financial(factory)
    approved = decision(repo)
    assert approved['resolution'] == 'verified'
    assert repo.applyReviewedDecision(approved, now=NOW)['status'] == 'applied'
    assert repo.applyReviewedDecision(approved, now=NOW)['status'] == 'already_applied'
    assert financial(factory) == before
    snapshot = SubscriptionSnapshotService(repo.snapshotFactsLoader).get(OWNER['userId'], NOW)
    assert snapshot.status == 'active' and snapshot.planCode == 'pro' and snapshot.billingInterval == 'month'
    with closing(factory()) as db, db, db.cursor() as cursor:
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='subscriptions'")
        columns = {r[0] for r in cursor.fetchall()}
        assert len(columns) == 30 and NEW_COLUMNS <= columns
        retired = {'billing_mode','current_period_start','current_period_end','renewal_due_at','auto_renew_enabled',
            'payment_collection_mode','status','subscribed_experts','domain_count','pending_removals','pending_additions','billing_state','plan_type'}
        assert len(columns - retired) == 17  # Target specification only; no contraction in this commit.
        cursor.execute("SELECT data_type FROM information_schema.columns WHERE table_schema='public' AND table_name='subscriptions' AND column_name='user_id'")
        assert cursor.fetchone()[0] == 'text'
        cursor.execute("SELECT count(*) FROM pg_constraint WHERE contype='f' AND confrelid='public.subscriptions'::regclass")
        assert cursor.fetchone()[0] == 4
        cursor.execute("SELECT relrowsecurity FROM pg_class WHERE oid='public.subscriptions'::regclass")
        assert cursor.fetchone()[0] is True
        cursor.execute("SELECT tgname FROM pg_trigger WHERE tgrelid='public.subscriptions'::regclass AND NOT tgisinternal")
        assert ('trg_subscriptions_updated_at',) in cursor.fetchall()
        cursor.execute("SELECT count(*) FROM public.billing_events WHERE event_type='subscription.legacy_snapshot'")
        assert cursor.fetchone()[0] == 1
        cursor.execute("SELECT metadata_json->'archive'->'originalRow'->'billing_state'->'unknown' FROM public.billing_events WHERE event_type='subscription.legacy_snapshot'")
        assert cursor.fetchone()[0] == {'keep': [1, 2]}
        cursor.execute(MIGRATION.read_text())  # Reapplicable SQL, not fake migration history.


@pytest.mark.parametrize('mutation', ['version', 'legacy', 'financial', 'patch'])
def test_changed_review_evidence_rejects_atomically(modelDb, mutation):
    factory, repo = modelDb
    seed(factory)
    approved = decision(repo)
    if mutation == 'patch':
        approved['patch']['plan_code'] = None
    else:
        statement = {'version': 'UPDATE subscriptions SET version=version+1',
            'legacy': "UPDATE subscriptions SET cancellation_reason='changed'",
            'financial': 'UPDATE "Invoices" SET total_amount=total_amount+1'}[mutation]
        with closing(factory()) as db, db, db.cursor() as cursor:
            cursor.execute(statement)
    before = financial(factory)
    with pytest.raises(ValueError, match='REVIEW'):
        repo.applyReviewedDecision(approved, now=NOW)
    assert financial(factory) == before
    with closing(factory()) as db, db, db.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM billing_events WHERE event_type='subscription.legacy_snapshot'")
        assert cursor.fetchone()[0] == 0


def test_concurrent_replay_archives_and_updates_once(modelDb):
    factory, repo = modelDb
    seed(factory)
    approved = decision(repo)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: repo.applyReviewedDecision(approved, now=NOW), range(2)))
    assert sorted(r['status'] for r in results) == ['already_applied', 'applied']


@pytest.mark.parametrize('statement', [
    "UPDATE billing_events SET metadata_json='{}' WHERE event_type='subscription.legacy_snapshot'",
    "DELETE FROM billing_events WHERE event_type='subscription.legacy_snapshot'", 'TRUNCATE billing_events',
])
def test_legacy_archive_is_immutable(modelDb, statement):
    factory, repo = modelDb
    seed(factory)
    repo.applyReviewedDecision(decision(repo), now=NOW)
    with pytest.raises(psycopg2.errors.RaiseException, match='immutable'):
        with closing(factory()) as db, db, db.cursor() as cursor:
            cursor.execute(statement)


def test_read_only_inventory_shadow_and_unstamped_or_stale_loads(modelDb):
    factory, repo = modelDb
    seed(factory)
    with pytest.raises(SubscriptionAuthorityUnavailable):
        SubscriptionSnapshotService(repo.snapshotFactsLoader).get(OWNER['userId'], NOW)
    approved = decision(repo)
    repo.applyReviewedDecision(approved, now=NOW)
    before = financial(factory)
    report = repo.shadow(OWNER['userId'], NOW)
    assert report['available'] and report['v2']['status'] == 'active'
    assert financial(factory) == before
    with closing(factory()) as db, db, db.cursor() as cursor:
        cursor.execute("UPDATE subscriptions SET status='expired'")  # Even a legacy writer that forgot version fencing is detected.
    assert not repo.shadow(OWNER['userId'], NOW)['available']


def test_reviewed_consumed_unknown_dates_stay_ineligible_and_noncanonical_row_archives(modelDb):
    factory, repo = modelDb
    row = seed(factory, 'none')
    with closing(factory()) as db, db, db.cursor() as cursor:
        cursor.execute("UPDATE subscriptions SET status='expired',plan_type='free',billing_state=jsonb_set(billing_state,'{trialConsumed}','true')")
    trialReview = {'consumed': True, 'reviewReference': 'synthetic-historical-proof'}
    approved = decision(repo, trialReviews={SUB: trialReview}) | {'trialReview': trialReview}
    assert approved['resolution'] == 'consumed_dates_unknown'
    repo.applyReviewedDecision(approved, now=NOW)
    snapshot = SubscriptionSnapshotService(repo.snapshotFactsLoader).get(OWNER['userId'], NOW)
    assert not snapshot.trialAvailable and snapshot.status == 'expired' and snapshot.purchaseAllowed
    with closing(factory()) as db, db, db.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("SELECT metadata_json->'backfill'->'trialReview'->>'reviewReference' AS proof FROM billing_events WHERE event_type='subscription.model_backfill'")
        assert cursor.fetchone()['proof'] == 'synthetic-historical-proof'
        cursor.execute("INSERT INTO subscriptions(user_id,is_canonical) VALUES(%s,false) RETURNING *", (row['user_id'],))
        historical = dict(cursor.fetchone())
        repo.archiveLegacySnapshot(cursor, historical)
        cursor.execute("SELECT metadata_json->'archive'->'originalRow'->>'is_canonical' AS canonical FROM billing_events WHERE subscription_id=%s", (historical['id'],))
        assert cursor.fetchone()['canonical'] == 'false'


def test_missing_archive_remains_unavailable_even_if_a_stamp_survives(modelDb):
    factory, repo = modelDb
    seed(factory)
    repo.applyReviewedDecision(decision(repo), now=NOW)
    # Model recovery from an external damaged schema, not a permitted application operation.
    with closing(factory()) as db, db, db.cursor() as cursor:
        cursor.execute('DROP TRIGGER trg_subscription_model_evidence ON billing_events')
        cursor.execute("DELETE FROM billing_events WHERE event_type='subscription.legacy_snapshot'")
    with pytest.raises(SubscriptionAuthorityUnavailable):
        SubscriptionSnapshotService(repo.snapshotFactsLoader).get(OWNER['userId'], NOW)


def test_cli_defaults_to_read_only_and_requires_explicit_reviewed_apply(modelDb, monkeypatch, tmp_path, capsys):
    import json
    from scripts.subscription_model_inventory import main as inventory
    from scripts.subscription_model_backfill import main as backfill
    factory, repo = modelDb
    seed(factory, 'none')
    monkeypatch.setenv('SUBSCRIPTION_MODEL_BACKFILL_DATABASE_URL', os.environ['SUBSCRIPTION_MODEL_TEST_DATABASE_URL'])
    reviewFile = tmp_path / 'private-review.json'
    assert inventory(['--review-output', str(reviewFile), '--shadow']) == 0
    public = capsys.readouterr().out
    assert OWNER['userId'] not in public and 'postgresql://' not in public
    report = json.loads(reviewFile.read_text())
    for proposed in report['proposals']:
        proposed.update(reviewedBy='synthetic-reviewer', reviewReference='synthetic-proof')
    reviewFile.write_text(json.dumps(report))
    before = financial(factory)
    assert backfill(['--reviewed-file', str(reviewFile)]) == 0
    with closing(factory()) as db, db, db.cursor() as cursor:
        cursor.execute('SELECT version FROM subscriptions')
        assert cursor.fetchone()[0] == 1
        cursor.execute("SELECT count(*) FROM billing_events WHERE event_type='subscription.legacy_snapshot'")
        assert cursor.fetchone()[0] == 0
    assert backfill(['--reviewed-file', str(reviewFile), '--apply']) == 0
    assert backfill(['--reviewed-file', str(reviewFile), '--apply']) == 0
    assert backfill(['--reviewed-file', str(reviewFile)]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])['alreadyAppliedDecisions'] == 1
    with closing(factory()) as db, db, db.cursor() as cursor:
        cursor.execute('UPDATE subscriptions SET version=version+1')
    assert backfill(['--reviewed-file', str(reviewFile)]) == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])['staleDecisions'] == 1
    assert financial(factory) == before


def test_unexplained_retained_credits_never_make_an_old_account_trial_eligible(modelDb):
    factory, repo = modelDb
    seed(factory, 'none')
    with closing(factory()) as db, db, db.cursor() as cursor:
        cursor.execute('UPDATE credit_balances SET topup_tokens=2500')
    proposed = decision(repo)
    assert proposed['resolution'] == 'needs_review'
    assert repo.applyReviewedDecision(proposed, now=NOW)['status'] == 'needs_review'
    with pytest.raises(SubscriptionAuthorityUnavailable):
        SubscriptionSnapshotService(repo.snapshotFactsLoader).get(OWNER['userId'], NOW)


def test_markerless_reviewed_consumption_survives_apply_and_load(modelDb):
    factory, repo = modelDb
    seed(factory, 'none')
    approved = decision(repo, trialReviews={SUB: {'consumed': True, 'reviewReference': 'original-trial-proof'}})
    assert approved['resolution'] == 'consumed_dates_unknown'
    repo.applyReviewedDecision(approved, now=NOW)
    snapshot = SubscriptionSnapshotService(repo.snapshotFactsLoader).get(OWNER['userId'], NOW)
    assert snapshot.status == 'expired' and not snapshot.trialAvailable and snapshot.purchaseAllowed


@pytest.mark.parametrize('mutation', ['add_attempt', 'settle_attempt', 'change_invoice'])
def test_historical_subscription_money_fences_apply_and_replay(modelDb, mutation):
    factory, repo = modelDb
    seed(factory, 'none')
    with closing(factory()) as db, db, db.cursor() as cursor:
        cursor.execute('INSERT INTO subscriptions(id,user_id,is_canonical) VALUES(\'10000000-0000-4000-8000-000000000009\',%s,false) RETURNING id', (OWNER['userId'],))
        old = str(cursor.fetchone()[0])
        cursor.execute('INSERT INTO "Invoices"("userId",subscription_id,status) VALUES(%s,%s,\'PAYMENT_PENDING\') RETURNING id', (OWNER['userId'], old))
        invoice = str(cursor.fetchone()[0])
        cursor.execute('''INSERT INTO billing_events(user_id,subscription_id,invoice_id,event_category,event_type,payment_status)
            VALUES(%s,%s,%s,'payment_attempt','checkout','authorized')''', (OWNER['userId'], old, invoice))
    approved = decision(repo, trialReviews={SUB: {'consumed': True, 'reviewReference': 'original-trial-proof'}})
    assert repo.checkReviewedDecision(approved, now=NOW) == 'consumed_dates_unknown'
    repo.applyReviewedDecision(approved, now=NOW)
    assert repo.checkReviewedDecision(approved, now=NOW) == 'already_applied'
    snapshot = SubscriptionSnapshotService(repo.snapshotFactsLoader).get(OWNER['userId'], NOW)
    assert not snapshot.purchaseAllowed and snapshot.purchaseBlockReason == 'recovery_required'
    with closing(factory()) as db, db, db.cursor() as cursor:
        if mutation == 'add_attempt':
            cursor.execute('''INSERT INTO billing_events(user_id,subscription_id,invoice_id,event_category,event_type,payment_status)
                VALUES(%s,%s,%s,'payment_attempt','checkout','authorized')''', (OWNER['userId'], old, invoice))
        elif mutation == 'settle_attempt':
            cursor.execute("UPDATE billing_events SET payment_status='cancelled',metadata_json=%s WHERE subscription_id=%s",
                (Json({'manualBilling': {'closedAt': NOW.isoformat(), 'closureReconciledAt': NOW.isoformat()}}), old))
        else:
            cursor.execute('UPDATE "Invoices" SET total_amount=99 WHERE id=%s', (invoice,))
    with pytest.raises(ValueError, match='REVIEW_REPLAY_CHANGED'):
        repo.applyReviewedDecision(approved, now=NOW)
    assert repo.checkReviewedDecision(approved, now=NOW) == 'stale'
    with pytest.raises(SubscriptionAuthorityUnavailable):
        SubscriptionSnapshotService(repo.snapshotFactsLoader).get(OWNER['userId'], NOW)


def test_inventory_reports_malformed_row_without_losing_other_accounts(modelDb):
    factory, repo = modelDb
    seed(factory, 'none')
    with closing(factory()) as db, db, db.cursor() as cursor:
        cursor.execute('UPDATE subscriptions SET billing_state=%s', (Json({'manualBilling': None}),))
        cursor.execute('INSERT INTO "Users"("userId") VALUES(\'second-owner\')')
        cursor.execute('INSERT INTO subscriptions(user_id,is_canonical) VALUES(\'second-owner\',true)')
    report = repo.inventory(now=NOW)
    assert report['counts'] == {'verified': 1, 'needs_review': 1, 'consumed_dates_unknown': 0}


def test_archive_refuses_an_already_pending_erasure(modelDb):
    factory, repo = modelDb
    seed(factory, 'none')
    with closing(factory()) as db, db, db.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute('UPDATE subscriptions SET erasure_pending=true RETURNING *')
        row = dict(cursor.fetchone())
    with pytest.raises(ValueError, match='REVIEW_ERASURE_PENDING'):
        with closing(factory()) as db, db, db.cursor(cursor_factory=RealDictCursor) as cursor:
            repo.archiveLegacySnapshot(cursor, row)
    with closing(factory()) as db, db, db.cursor() as cursor:
        cursor.execute('SELECT count(*) FROM billing_events')
        assert cursor.fetchone()[0] == 0


@pytest.mark.parametrize('requestStatus', ['PENDING', 'IN_PROGRESS', 'PARTIALLY_FAILED', 'COMPLETED'])
def test_active_erasure_request_prevents_archiving_a_late_unflagged_subscription(modelDb, requestStatus):
    from api.services.billing.manualBillingRepository import ManualBillingRepository
    factory, repo = modelDb
    with closing(factory()) as db, db, db.cursor() as cursor:
        cursor.execute('INSERT INTO "Users"("userId") VALUES(%s)', (OWNER['userId'],))
        # Only the actual request columns consumed by the interlock are needed.
        cursor.execute('CREATE TABLE user_erasure_requests(target_user_id text,status text NOT NULL)')
        cursor.execute('INSERT INTO user_erasure_requests VALUES(%s,%s)', (OWNER['userId'], requestStatus))
    created = ManualBillingRepository(factory).ensureCanonicalSubscription(OWNER['userId'])
    assert created['erasure_pending'] is False
    with closing(factory()) as db, db, db.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute('SELECT * FROM subscriptions WHERE id=%s', (created['id'],))
        row = dict(cursor.fetchone())
    if requestStatus == 'COMPLETED':
        with closing(factory()) as db, db, db.cursor(cursor_factory=RealDictCursor) as cursor:
            repo.archiveLegacySnapshot(cursor, row)
    else:
        with pytest.raises(ValueError, match='REVIEW_ERASURE_PENDING'):
            with closing(factory()) as db, db, db.cursor(cursor_factory=RealDictCursor) as cursor:
                repo.archiveLegacySnapshot(cursor, row)
    with closing(factory()) as db, db, db.cursor() as cursor:
        cursor.execute('SELECT count(*) FROM billing_events')
        assert cursor.fetchone()[0] == (1 if requestStatus == 'COMPLETED' else 0)
