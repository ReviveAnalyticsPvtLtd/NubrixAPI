"""Read-only contraction review; never applies DDL or authorizes a rollout.

Use a dedicated SUBSCRIPTION_MODEL_CUTOVER_DATABASE_URL, not the application
DSN. External evidence is an operator attestation, not proof generated here.
Run again under the eventual migration lock: a report is only a point-in-time
observation and cannot close the check/apply race.
"""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.services.subscriptions.subscriptionModelBackfill import (  # noqa: E402
    evidenceHash, legacyRowHash, metadata, stableLegacyHash, timestamp,
)

TARGET_COLUMNS = frozenset(('id','user_id','is_canonical','lifecycle_id','plan_code',
    'trial_started_at','trial_ends_at','trial_closed_at','renewal_opt_out',
    'cancellation_requested_at','cancellation_reason','default_currency',
    'admin_credit_generation','version','erasure_pending','created_at','updated_at'))
RETIRED_COLUMNS = frozenset(('billing_mode','current_period_start','current_period_end',
    'renewal_due_at','auto_renew_enabled','payment_collection_mode','status',
    'subscribed_experts','domain_count','pending_removals','pending_additions','billing_state','plan_type'))
REQUIRED_CHECKS = ('frontendAccepted','workersDrained','v2ConsumersOnly',
    'shadowComparisonAccepted','financialReconciliationAccepted',
    'rollbackRehearsed','contractedAcceptancePassed')


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def assessCutover(state, *, phase, sourceDigest, now, evidence=None):
    """Aggregate private records without publishing them or altering input."""
    blockers = set()
    columns = set(state['columns'])
    if columns == TARGET_COLUMNS:
        blockers.add('STORAGE_ALREADY_CONTRACTED')
    elif columns != TARGET_COLUMNS | RETIRED_COLUMNS:
        blockers.add('UNEXPECTED_SUBSCRIPTION_SCHEMA')
    if phase != 'v2':
        blockers.add('PHASE_NOT_V2')
    if not state['retentionAvailable']:
        blockers.add('ERASURE_RETENTION_UNAVAILABLE')
    if not all(state['protections'].get(k) is True for k in ('rls','canonicalUnique','updatedAtTrigger','retentionImmutable')):
        blockers.add('STORAGE_PROTECTION_MISSING')
    if state['authorityFailures']:
        blockers.add('RUNTIME_AUTHORITY_UNAVAILABLE')
    canonical = Counter(r['user_id'] for r in state['rows'] if r.get('is_canonical') is True)
    owners = set(state['owners'])
    if any(r['user_id'] not in owners or canonical[r['user_id']] != 1 for r in state['rows']):
        blockers.add('CANONICAL_OWNER_INVALID')
    archiveFailures = authorityMissing = unresolved = 0
    for row in state['rows']:
        events = [e for e in state['events'] if e.get('event_status') == 'COMMITTED'
            and e.get('user_id') == row['user_id'] and str(e.get('subscription_id')) == str(row['id'])]
        archives, currentArchives = [], []
        for event in events:
            if event.get('event_type') != 'subscription.legacy_snapshot':
                continue
            try:
                archive = metadata(event).get('archive', {})
                original = archive.get('originalRow')
                if (archive.get('schemaVersion') == 2 and isinstance(original, dict)
                        and original.get('user_id') == row['user_id'] and str(original.get('id')) == str(row['id'])
                        and legacyRowHash(original) == archive.get('legacyHash')):
                    archives.append(event)
                    if stableLegacyHash(original) == stableLegacyHash(row):
                        currentArchives.append(event)
            except (ValueError, TypeError, KeyError):
                continue
        if not currentArchives:
            archiveFailures += 1
        if row.get('is_canonical') is not True:
            continue
        native = any(e.get('event_type') == 'subscription.model_initialized'
            and metadata(e).get('schemaVersion') == 2 for e in events)
        stamps = [metadata(e).get('backfill', {}) for e in events if e.get('event_type') == 'subscription.model_backfill']
        linked = [s for s in stamps if any(str(a['id']) == str(s.get('archiveId')) for a in archives)]
        if any(s.get('resolution') == 'consumed_dates_unknown' for s in linked):
            unresolved += 1
        if not native and not any(s.get('resolution') == 'verified'
                and _text(s.get('reviewedBy')) and _text(s.get('reviewReference')) for s in linked):
            authorityMissing += 1
    for count, code in ((archiveFailures,'LEGACY_ARCHIVE_MISSING_OR_CHANGED'),
            (authorityMissing,'OWNED_MODEL_AUTHORITY_MISSING'),(unresolved,'TRIAL_HISTORY_UNRESOLVED')):
        if count:
            blockers.add(code)
    dependencyDigests = sorted({evidenceHash(d) for d in state['dependencies']})
    stateDigest = evidenceHash(state)
    evidence = evidence if isinstance(evidence, dict) else {}
    if (evidence.get('schemaVersion') != 1 or not _text(evidence.get('reviewedBy'))
            or not _text(evidence.get('reviewReference'))):
        blockers.add('REVIEW_EVIDENCE_MISSING')
    if evidence.get('sourceDigest') != sourceDigest:
        blockers.add('REVIEW_SOURCE_CHANGED')
    if evidence.get('stateDigest') != stateDigest:
        blockers.add('REVIEW_STATE_CHANGED')
    try:
        issued, expires = timestamp(evidence['issuedAt']), timestamp(evidence['expiresAt'])
        if not issued <= now < expires or not timedelta(0) < expires-issued <= timedelta(hours=24):
            raise ValueError('STALE')
    except (ValueError, TypeError, KeyError):
        blockers.add('REVIEW_EVIDENCE_STALE')
    reviewed = evidence.get('reviewedDependencies')
    if not isinstance(reviewed, list) or sorted(reviewed) != dependencyDigests:
        blockers.add('UNREVIEWED_DEPENDENCIES')
    checks = evidence.get('checks')
    checks = checks if isinstance(checks, dict) else {}
    missingChecks = [name for name in REQUIRED_CHECKS if not isinstance(checks.get(name), dict)
        or checks[name].get('passed') is not True or not _text(checks[name].get('reference'))]
    if missingChecks:
        blockers.add('EXTERNAL_ACCEPTANCE_INCOMPLETE')
    return dict(schemaVersion=1, readOnly=True, authorizesCutover=False,
        readyForOperatorReview=not blockers, phase=phase, evaluatedAt=timestamp(now).isoformat(),
        stateDigest=stateDigest, sourceDigest=sourceDigest, dependencyDigests=dependencyDigests,
        financial=state['financial'], counts=dict(rows=len(state['rows']),archiveFailures=archiveFailures,
            authorityMissing=authorityMissing,unresolvedTrial=unresolved,authorityFailures=state['authorityFailures']),
        missingChecks=missingChecks, blockers=sorted(blockers))


def sourceTreeDigest(root=ROOT):
    # Git is the source/runtime manifest, including newly added consumers and
    # config of any extension. Never walk dependency, secret or build trees.
    manifest = subprocess.run(['git','-C',str(root),'ls-files','-z','--cached','--others','--exclude-standard'],
        check=True,capture_output=True).stdout.decode('utf-8').split('\0')
    excluded = {'.git','.venv','venv','node_modules','.next','__pycache__','.temp','dist','build','logs','agentic-docs'}
    digest = hashlib.sha256()
    for name in sorted(set(manifest)-{''}):
        relative = Path(name)
        if (set(relative.parts) & excluded or relative.name.startswith('.env')
                or relative.suffix in ('.env','.pem','.key','.p12','.pyc')):
            continue
        path = root/relative
        if path.is_symlink():
            raise ValueError('SOURCE_SYMLINK_REQUIRES_REVIEW')
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode()+b'\0'+path.read_bytes()+b'\0')
    return digest.hexdigest()


def _dependencies(cursor):
    # Include definitions, not just object names: an object can change in place.
    cursor.execute("""SELECT DISTINCT d.classid::regclass::text AS kind,
        pg_describe_object(d.classid,d.objid,d.objsubid) AS identity,
        a.attname AS column_name,
        CASE WHEN d.classid='pg_constraint'::regclass THEN
            pg_get_constraintdef(d.objid)
        WHEN d.classid='pg_attrdef'::regclass THEN
            (SELECT pg_get_expr(adbin,adrelid) FROM pg_attrdef WHERE oid=d.objid)
        WHEN d.classid='pg_rewrite'::regclass THEN
            (SELECT pg_get_viewdef(ev_class,true) FROM pg_rewrite WHERE oid=d.objid)
        WHEN d.classid='pg_class'::regclass THEN
            (SELECT CASE WHEN relkind='i' THEN pg_get_indexdef(oid)
                WHEN relkind IN ('v','m') THEN pg_get_viewdef(oid,true) ELSE relkind::text END
                FROM pg_class WHERE oid=d.objid)
        WHEN d.classid='pg_policy'::regclass THEN
            (SELECT coalesce(pg_get_expr(polqual,polrelid),'') || ':' ||
                coalesce(pg_get_expr(polwithcheck,polrelid),'') FROM pg_policy WHERE oid=d.objid)
        ELSE d.deptype::text END AS definition
        FROM pg_depend d JOIN pg_attribute a ON a.attrelid=d.refobjid AND a.attnum=d.refobjsubid
        WHERE d.refobjid='public.subscriptions'::regclass AND a.attname=ANY(%s)
        ORDER BY kind,identity,column_name,definition""", (sorted(RETIRED_COLUMNS),))
    result = [dict(r) for r in cursor.fetchall()]
    cursor.execute("""SELECT 'trigger' AS kind,t.tgname AS identity,
        pg_get_triggerdef(t.oid) || ':' || pg_get_functiondef(t.tgfoid) AS definition
        FROM pg_trigger t WHERE t.tgrelid='public.subscriptions'::regclass AND NOT t.tgisinternal
        ORDER BY t.tgname""")
    result += [dict(r) for r in cursor.fetchall()]
    # Dynamic SQL/PLpgSQL references are not all represented in pg_depend.
    # These are candidates requiring review, not an automatic SQL safety proof.
    cursor.execute("""SELECT 'function_candidate' AS kind,
        n.nspname||'.'||p.proname||'('||pg_get_function_identity_arguments(p.oid)||')' AS identity,
        pg_get_functiondef(p.oid) AS definition
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE p.prokind IN ('f','p') AND n.nspname NOT IN ('pg_catalog','information_schema')
            AND p.prosrc ILIKE '%subscriptions%' ORDER BY identity""")
    result += [dict(r) for r in cursor.fetchall()]
    return result


def _financialDigests(cursor):
    from psycopg2 import sql
    # information_schema hides relations without privileges. Discover through
    # the catalog so an inaccessible financial relation fails rather than vanishes.
    cursor.execute("""SELECT n.nspname AS table_schema,c.relname AS table_name
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p') AND
        ((n.nspname='public' AND (c.relname IN ('Invoices','billing_events','credit_balances')
            OR c.relname LIKE 'credit\\_%%' ESCAPE '\\'
            OR c.relname LIKE 'admin\\_credit\\_%%' ESCAPE '\\'))
        OR (n.nspname='private' AND c.relname IN ('retained_user_subjects','retained_user_records')))
        ORDER BY n.nspname,c.relname""")
    tables = list(cursor.fetchall())
    result = {}
    for table in tables:
        schema, name = table['table_schema'], table['table_name']
        # Server-side canonical JSON makes numeric/date encoding deterministic.
        # Stream sorted rows instead of aggregating all financial JSON in memory.
        cursor.execute(sql.SQL('SELECT to_jsonb(t)::text AS row FROM {}.{} t ORDER BY to_jsonb(t)::text')
            .format(sql.Identifier(schema), sql.Identifier(name)))
        digest, count = hashlib.sha256(), 0
        while batch := cursor.fetchmany(128):
            for item in batch:
                digest.update(item['row'].encode()+b'\n')
                count += 1
        result[name if schema == 'public' else schema+'.'+name] = dict(count=count,digest=digest.hexdigest())
    if not {'Invoices','billing_events','credit_balances'} <= result.keys():
        raise ValueError('FINANCIAL_STORAGE_MISSING')
    return result


def _storageObjects(cursor):
    cursor.execute("""SELECT 'constraint' AS kind,conrelid::regclass::text||'.'||conname AS identity,
        pg_get_constraintdef(oid) AS definition FROM pg_constraint
        WHERE conrelid IN ('public.subscriptions'::regclass,'public."Invoices"'::regclass,
            'public.billing_events'::regclass,'public.credit_balances'::regclass)
            OR confrelid='public.subscriptions'::regclass
            OR conrelid IN (to_regclass('private.retained_user_subjects'),to_regclass('private.retained_user_records'))
            ORDER BY identity""")
    result = [dict(r) for r in cursor.fetchall()]
    cursor.execute("""SELECT 'index' AS kind,indexrelid::regclass::text AS identity,
        pg_get_indexdef(indexrelid)||':'||indisvalid::text AS definition FROM pg_index
        WHERE indrelid IN ('public.subscriptions'::regclass,to_regclass('private.retained_user_subjects'),
            to_regclass('private.retained_user_records')) ORDER BY identity""")
    result += [dict(r) for r in cursor.fetchall()]
    cursor.execute("""SELECT 'policy' AS kind,polrelid::regclass::text||'.'||polname AS identity,to_jsonb(p)::text AS definition
        FROM pg_policy p WHERE polrelid IN ('public.subscriptions'::regclass,to_regclass('private.retained_user_subjects'),
            to_regclass('private.retained_user_records')) ORDER BY identity""")
    result += [dict(r) for r in cursor.fetchall()]
    cursor.execute("""SELECT 'acl' AS kind,c.oid::regclass::text AS identity,
        coalesce(c.relacl::text,'')||':'||r.rolname||':'||c.relrowsecurity::text||':'||c.relforcerowsecurity::text AS definition
        FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner
        WHERE c.oid IN ('public.subscriptions'::regclass,'public."Invoices"'::regclass,
            'public.billing_events'::regclass,'public.credit_balances'::regclass)
            OR c.relnamespace=to_regnamespace('private') ORDER BY identity""")
    result += [dict(r) for r in cursor.fetchall()]
    cursor.execute("""SELECT 'financial_trigger' AS kind,t.tgrelid::regclass::text||'.'||t.tgname AS identity,
        pg_get_triggerdef(t.oid)||':'||pg_get_functiondef(t.tgfoid)||':'||t.tgenabled::text AS definition
        FROM pg_trigger t WHERE NOT t.tgisinternal AND t.tgrelid IN
            ('public.billing_events'::regclass,'public."Invoices"'::regclass,
                to_regclass('private.retained_user_subjects'),to_regclass('private.retained_user_records')) ORDER BY identity""")
    result += [dict(r) for r in cursor.fetchall()]
    cursor.execute("""SELECT 'retention_function' AS kind,
        p.oid::regprocedure::text AS identity,
        pg_get_functiondef(p.oid)||':'||p.prosecdef::text||':'||coalesce(p.proacl::text,'') AS definition
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='private' AND p.prokind IN ('f','p') ORDER BY identity""")
    return result + [dict(r) for r in cursor.fetchall()]


def _retentionProtected(cursor):
    cursor.execute("""SELECT count(*) AS count FROM pg_trigger t JOIN (VALUES
        ('private.retained_user_subjects','retained_subjects_immutable',27),
        ('private.retained_user_records','retained_records_immutable',27),
        ('private.retained_user_subjects','retained_subjects_no_truncate',34),
        ('private.retained_user_records','retained_records_no_truncate',34)
        ) AS expected(relation_name,trigger_name,trigger_type)
        ON t.tgrelid=to_regclass(expected.relation_name) AND t.tgname=expected.trigger_name
        WHERE t.tgtype=expected.trigger_type AND t.tgenabled IN ('O','A') AND t.tgqual IS NULL
            AND t.tgnargs=0 AND t.tgfoid=to_regprocedure('private.guard_retained_erasure_records()')""")
    triggers = cursor.fetchone()['count'] == 4
    cursor.execute("""SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE
        conrelid=to_regclass('private.retained_user_records') AND contype='c' AND convalidated
        AND pg_get_constraintdef(oid) LIKE '%record_hash%' AND pg_get_constraintdef(oid) LIKE '%sha256%') AS present""")
    return triggers and cursor.fetchone()['present']


def collectState(connectionFactory):
    """One read-only repeatable snapshot; only aggregate output leaves main()."""
    from psycopg2.extras import RealDictCursor
    from api.services.subscriptions.subscriptionModel import SubscriptionAuthorityUnavailable, evaluateSubscription
    from api.services.subscriptions.subscriptionModelRepository import SubscriptionModelRepository
    from api.services.userErasureRetention import retentionAvailable
    with closing(connectionFactory()) as db:
        db.set_session(isolation_level='REPEATABLE READ', readonly=True)
        with db, db.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SET LOCAL statement_timeout='30s'")
            cursor.execute("SET LOCAL lock_timeout='5s'")
            cursor.execute("SET LOCAL TIME ZONE 'UTC'")
            # This does not bypass RLS. PostgreSQL errors if a policy would
            # filter results, instead of silently certifying a partial snapshot.
            cursor.execute("SET LOCAL row_security=off")
            cursor.execute("""SELECT column_name,data_type,is_nullable,column_default
                FROM information_schema.columns WHERE table_schema='public'
                    AND table_name='subscriptions' ORDER BY column_name""")
            schema = [dict(r) for r in cursor.fetchall()]
            cursor.execute('SELECT * FROM public.subscriptions ORDER BY user_id,id')
            rows = [dict(r) for r in cursor.fetchall()]
            cursor.execute('SELECT "userId","isBanned" FROM public."Users" ORDER BY "userId"')
            ownerRecords = [dict(r) for r in cursor.fetchall()]
            cursor.execute("""SELECT * FROM public.billing_events WHERE event_type IN
                ('subscription.legacy_snapshot','subscription.model_backfill','subscription.model_initialized') ORDER BY id""")
            events = [dict(r) for r in cursor.fetchall()]
            cursor.execute("SELECT relrowsecurity FROM pg_class WHERE oid='public.subscriptions'::regclass")
            rls = cursor.fetchone()['relrowsecurity']
            cursor.execute("""SELECT EXISTS(SELECT 1 FROM pg_index WHERE indrelid='public.subscriptions'::regclass
                AND indisunique AND indisvalid AND indnkeyatts=1 AND indnatts=1
                AND pg_get_indexdef(indexrelid,1,true)='user_id'
                AND regexp_replace(pg_get_expr(indpred,indrelid),'[()[:space:]]','','g')='is_canonical') AS present""")
            unique = cursor.fetchone()['present']
            cursor.execute("""SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='public.subscriptions'::regclass
                AND tgname='trg_subscriptions_updated_at' AND NOT tgisinternal AND tgenabled IN ('O','A')) AS present""")
            trigger = cursor.fetchone()['present']
            model, failures = SubscriptionModelRepository(connectionFactory), 0
            now = model._now(cursor)
            for row in rows:
                if row.get('is_canonical') is True:
                    try:
                        evaluateSubscription(model.loadRuntimeFacts(cursor,row,now),now)
                    except SubscriptionAuthorityUnavailable:
                        failures += 1
            return dict(columns=[r['column_name'] for r in schema],columnDefinitions=schema,
                rows=rows,owners=[r['userId'] for r in ownerRecords],ownerRecords=ownerRecords,
                events=events,authorityFailures=failures,retentionAvailable=retentionAvailable(cursor),
                dependencies=_dependencies(cursor),financial=_financialDigests(cursor),storageObjects=_storageObjects(cursor),
                protections=dict(rls=rls,canonicalUnique=unique,updatedAtTrigger=trigger,
                    retentionImmutable=_retentionProtected(cursor)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', required=True)
    parser.add_argument('--phase', choices=('legacy','shadow','dual','v2','contracted'), required=True)
    parser.add_argument('--evidence', type=Path, help='Private external review JSON; never stage')
    parser.add_argument('--dependency-output', type=Path,
        help='Explicit private dependency definitions for review; create only, never overwrite or stage')
    args = parser.parse_args(argv)
    url = os.environ.get('SUBSCRIPTION_MODEL_CUTOVER_DATABASE_URL')
    if not url:
        print('Dedicated SUBSCRIPTION_MODEL_CUTOVER_DATABASE_URL required; nothing read or written.', file=sys.stderr)
        return 2
    try:
        import psycopg2
        evidence = json.loads(args.evidence.read_text(encoding='utf-8')) if args.evidence else None
        state = collectState(lambda: psycopg2.connect(url, application_name='subscription-contraction-check', connect_timeout=5))
        report = assessCutover(state, phase=args.phase, sourceDigest=sourceTreeDigest(),
            now=datetime.now(timezone.utc), evidence=evidence)
        if args.dependency_output:
            with args.dependency_output.open('x', encoding='utf-8', newline='\n') as target:
                json.dump(dict(schemaVersion=1,sourceDigest=report['sourceDigest'],stateDigest=report['stateDigest'],
                    dependencies=state['dependencies'],storageObjects=state['storageObjects'],
                    protections=state['protections'],columnDefinitions=state['columnDefinitions'],
                    authorizesCutover=False),target,indent=2)
                target.write('\n')
        print(json.dumps(report, sort_keys=True))
        return 0 if report['readyForOperatorReview'] else 1
    except Exception:
        # Database errors may embed credentials, raw retained JSON or SQL values.
        print('Subscription contraction check unavailable; no database writes requested.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
