"""Dedicated localhost Redis plus durable SQL recovery; never app Redis."""
import os
from urllib.parse import urlparse
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime,timezone
from unittest.mock import patch
import pytest
import psycopg2
import redis
from test.test_manual_billing_postgres_integration import postgres,payment
from api.services.credits.manualCreditRepository import ManualCreditRepository

pytestmark=pytest.mark.skipif(os.environ.get('RUN_MANUAL_BILLING_INTEGRATION')!='1',reason='Dedicated PostgreSQL/Redis opt-in required; skipped is UNVERIFIED')


@pytest.fixture
def isolated_redis():
    url=os.environ.get('MANUAL_BILLING_TEST_REDIS_URL','')
    parsed=urlparse(url)
    assert parsed.scheme=='redis' and parsed.hostname in ('127.0.0.1','localhost')
    assert parsed.port not in (None,6379), 'Explicit disposable random Redis port required'
    client=redis.Redis.from_url(url,decode_responses=True)
    assert client.ping()
    prefix='credits:v3:manual_billing_test_'+uuid.uuid4().hex
    try: yield client,prefix
    finally:
        keys=list(client.scan_iter(prefix+'*'))
        if keys: client.delete(*keys)


def test_retained_redis_adapter_uses_only_isolated_keys(isolated_redis):
    client,key=isolated_redis
    from api.services.credits.creditService import CreditService
    service=CreditService()
    service._redis=lambda:client
    service._redisKey=lambda _:key
    client.hset(key,mapping={'trem':10000,'ttop':500,'tquota':10000,'pend':4102444800,'pnext':4105123200})
    assert service._deduct('test-user',10100)=={'trem':0,'ttop':400,'spill':100,'rolled':0}
    assert service._peek('test-user')['ttop']==400


def test_stale_redis_cannot_grant_or_replace_shared_sql_balance(payment,isolated_redis):
    repository,evidence,url=payment
    period=repository.finalizeCapturedPayment(evidence).currentPeriod
    client,key=isolated_redis
    client.hset(key,mapping={'trem':999999999,'ttop':999999999,'tquota':999999999,'pend':4102444800,'pnext':4105123200})
    credits=ManualCreditRepository(repository)
    context=credits.admit(evidence.userId,'reporting_query','before-recovery')
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute('update public.credit_balances set remaining_tokens=5000,topup_tokens=100 where user_id=%s',(evidence.userId,))
    credits.reportUsage(context,5050,'measured-provider-run')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:credits.settle(context,5050,'measured-provider-run'),range(2)))
    snapshot=credits.balanceSnapshot(evidence.userId)
    assert snapshot['remaining_tokens']==0 and snapshot['topup_tokens']==50
    assert all(result['monthlyCharged']==5000 and result['topupCharged']==50 for result in results)
    with psycopg2.connect(url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("select count(*) from public.billing_events where user_id=%s and event_type='credit.operation_settled'",(evidence.userId,))
            assert cursor.fetchone()[0]==1


def test_sql_usage_outage_recovers_original_measurement_once(payment,isolated_redis,tmp_path,monkeypatch):
    repository,evidence,url=payment
    repository.finalizeCapturedPayment(evidence)
    credits=ManualCreditRepository(repository)
    context=credits.admit(evidence.userId,'reporting_query','outage-measurement')
    before=credits.balanceSnapshot(evidence.userId)['remaining_tokens']
    monkeypatch.setenv('CREDIT_USAGE_SPOOL_DIR',str(tmp_path/'private-spool'))
    from api.services.credits.creditUsageSpool import retainUsage,recoverSpooledUsage
    retainUsage(context,1234,'provider-result')
    assert recoverSpooledUsage(credits)=={'settled':1,'errors':0}
    assert recoverSpooledUsage(credits)=={'settled':0,'errors':0}
    assert credits.balanceSnapshot(evidence.userId)['remaining_tokens']==before-1234
