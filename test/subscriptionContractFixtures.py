"""Test-only 24 -> 30 -> 17 storage. Never a production migration runner."""
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json,RealDictCursor
import pytest

from scripts.subscription_model_cutover import TARGET_COLUMNS, RETIRED_COLUMNS
from api.services.subscriptions.subscriptionModelBackfill import NEW_COLUMNS
from api.services.subscriptions.subscriptionModelRepository import SubscriptionModelRepository
from test.test_live_billing_schema_postgres import actualSchema  # noqa: F401
from test.subscriptionBackfillFixtures import source,NOW,SUB,LIFE,INVOICE,EVENT,OWNER

ROOT=Path(__file__).resolve().parents[1]


def columns(cursor):
    cursor.execute("""SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name='subscriptions'""")
    return {r['column_name'] if isinstance(r,dict) else r[0] for r in cursor.fetchall()}


def identities(cursor):
    cursor.execute("""SELECT conrelid::regclass::text,conname,pg_get_constraintdef(oid) FROM pg_constraint
        WHERE confrelid='public.subscriptions'::regclass OR
            (conrelid='public.subscriptions'::regclass AND contype IN ('p','f')) ORDER BY 1,2""")
    constraints=cursor.fetchall()
    cursor.execute("SELECT relrowsecurity,relacl::text FROM pg_class WHERE oid='public.subscriptions'::regclass")
    access=cursor.fetchone()
    cursor.execute("""SELECT pg_get_triggerdef(oid) FROM pg_trigger
        WHERE tgrelid='public.subscriptions'::regclass AND NOT tgisinternal ORDER BY tgname""")
    triggers=cursor.fetchall()
    cursor.execute("""SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i
        WHERE i.indrelid='public.subscriptions'::regclass AND NOT EXISTS (
            SELECT 1 FROM pg_depend d JOIN pg_attribute a ON a.attrelid=d.refobjid AND a.attnum=d.refobjsubid
            WHERE d.classid='pg_class'::regclass AND d.objid=i.indexrelid
                AND d.refobjid=i.indrelid AND a.attname=ANY(%s)) ORDER BY 1""",(sorted(RETIRED_COLUMNS),))
    return constraints,access,triggers,cursor.fetchall()


def financial(cursor):
    result={}
    for name in ('Invoices','credit_balances','billing_events'):
        cursor.execute(sql.SQL('SELECT to_jsonb(t)::text FROM public.{} t ORDER BY to_jsonb(t)::text').format(sql.Identifier(name)))
        result[name]=cursor.fetchall()
    return result


def seedReviewed(factory,condition):
    row,owner,invoices,events=source(condition)
    user='reviewed-'+condition+'-'+str(uuid4())
    mapping={OWNER['userId']:user,**{key:str(uuid4()) for key in (SUB,LIFE,INVOICE,EVENT)}}
    def replace(value):
        if isinstance(value,dict):return {k:replace(v) for k,v in value.items()}
        if isinstance(value,list):return [replace(v) for v in value]
        return mapping.get(value,value) if isinstance(value,str) else value
    row,owner,invoices,events=map(replace,(row,owner,invoices,events))
    with closing(factory()) as db,db,db.cursor() as cursor:
        cursor.execute('INSERT INTO public."Users"("userId",email) VALUES(%s,%s)',(user,user+'@example.test'))
        for name,records in (('subscriptions',[row]),('Invoices',invoices),('billing_events',events)):
            for record in records:
                cursor.execute(sql.SQL('INSERT INTO public.{} ({}) VALUES ({})').format(sql.Identifier(name),
                    sql.SQL(',').join(map(sql.Identifier,record)),sql.SQL(',').join(sql.Placeholder() for _ in record)),
                    [Json(v) if isinstance(v,(dict,list)) else v for v in record.values()])
        if condition=='paid':
            cursor.execute('''INSERT INTO credit_balances(user_id,subscription_id,plan_tier,domain_count,
                used_tokens,remaining_tokens,topup_tokens,monthly_token_quota,lifecycle_id,credit_period_id,period_start,period_end)
                VALUES(%s,%s,'pro',1,700,300,2500,1000,%s,%s,%s,%s)''',
                (user,row['id'],mapping[LIFE],str(uuid4()),row['current_period_start'],row['current_period_end']))
    model=SubscriptionModelRepository(factory)
    proposal=next(p for p in model.inventory(now=NOW)['proposals'] if p['userId']==user)
    assert proposal['resolution']=='verified'
    model.applyReviewedDecision({**proposal,'reviewedBy':'synthetic-review','reviewReference':'synthetic-contract-fixture'},now=NOW)
    return user


@pytest.fixture(scope='module')
def bootstrapSchema(actualSchema):
    url=actualSchema
    with closing(psycopg2.connect(url)) as db,db,db.cursor() as cursor:
        # actualSchema guards localhost/manual_billing_test_* and starts empty.
        cursor.execute('SELECT count(*) FROM public.subscriptions')
        assert cursor.fetchone()[0]==0
        cursor.execute((ROOT/'supabase/manual_billing_contract/20261005195626_contract_recurring_billing_fields.sql').read_text(encoding='utf-8'))
        assert columns(cursor)==(TARGET_COLUMNS | RETIRED_COLUMNS)-NEW_COLUMNS
        assert len(columns(cursor))==24
        cursor.execute((ROOT/'supabase/migrations/20261017211222_expand_subscription_model_v2.sql').read_text(encoding='utf-8'))
        for name in ('20261017214302_protect_subscription_purchase_authority.sql',
                     '20261018070140_protect_subscription_trial_authority.sql'):
            cursor.execute((ROOT/'supabase/migrations'/name).read_text(encoding='utf-8'))
        assert columns(cursor)==TARGET_COLUMNS | RETIRED_COLUMNS
        assert len(columns(cursor))==30
    factory=lambda:psycopg2.connect(url)
    return url,{condition:seedReviewed(factory,condition) for condition in ('none','trial','paid')}


@pytest.fixture(scope='module',params=['expanded','retained'])
def bootstrapStorage(bootstrapSchema,request):
    url,reviewed=bootstrapSchema
    factory=lambda:psycopg2.connect(url)
    if request.param=='retained':
        model=SubscriptionModelRepository(factory)
        with closing(factory()) as db,db,db.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute('SELECT * FROM public.subscriptions ORDER BY user_id,id')
            for row in list(cursor.fetchall()):
                model.archiveLegacySnapshot(cursor,row)
        with closing(factory()) as db,db,db.cursor() as cursor:
            before,balances=identities(cursor),financial(cursor)
            cursor.execute('SET LOCAL lock_timeout=\'5s\'')
            cursor.execute(sql.SQL('ALTER TABLE public.subscriptions {}').format(sql.SQL(',').join(
                sql.SQL('DROP COLUMN {}').format(sql.Identifier(name)) for name in sorted(RETIRED_COLUMNS))))
            assert columns(cursor)==TARGET_COLUMNS and len(columns(cursor))==17
            assert identities(cursor)==before
            assert financial(cursor)==balances
    return request.param,factory,reviewed


@pytest.fixture
def bootstrapAccount(bootstrapStorage,monkeypatch):
    from api.services.billing.manualBillingRepository import ManualBillingRepository
    shape,factory,_=bootstrapStorage
    monkeypatch.setenv('SUBSCRIPTION_MODEL_PHASE','contracted' if shape=='retained' else 'v2')
    user='contract-bootstrap-'+str(uuid4())
    with closing(factory()) as db,db,db.cursor() as cursor:
        cursor.execute('INSERT INTO public."Users"("userId",email) VALUES(%s,%s)',(user,user+'@example.test'))
    return ManualBillingRepository(factory),factory,user,shape
