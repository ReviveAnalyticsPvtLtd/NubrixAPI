import json
import pytest
from test.test_billing_notification_revisions import deliveries
from test.test_manual_billing_runtime import database,sqlTransaction,USER,NOW


@pytest.fixture
def obligations(deliveries):
    deliveryRepo,path=deliveries
    with sqlTransaction(path) as db:
        # Same timestamps exercise the ID tie-breaker rather than timestamp-only pagination.
        for identifier,category,kind,status,metadata in [
            ('ack','payment_attempt','payment.checkout','pending_provider_ack',{}),
            ('capture','reconciliation','payment.capture','REQUIRES_RECONCILIATION',{'reason':'DUPLICATE_MONEY'}),
            ('unmapped','reconciliation','payment.unmapped','REQUIRES_RECONCILIATION',{}),
            ('refund','reconciliation','refund.intent','unknown',{'amount':2000,'reason':'private reason','caseReference':'private case'}),
            ('usage','audit','credit.usage_reported','PENDING',{'tokensUsed':500}),
            ('bridge','notification','email.billing_intent.committed','COMMITTED',{'metadata':{'holdForRenewalEvidence':True}}),
        ]:
            db.execute('INSERT INTO billing_events(id,user_id,event_category,event_type,event_status,payment_status,metadata_json,occurred_at) VALUES(?,?,?,?,?,?,?,?)',
                (identifier,USER,category,kind,status,status,json.dumps(metadata),NOW.isoformat()))
        db.execute("UPDATE billing_events SET user_id=null WHERE id='capture'")
    deliveryRepo.enqueueBillingNotification(USER,None,'payment_receipt','receipt:pending',NOW.isoformat(),{})
    with sqlTransaction(path) as db:
        db.execute("UPDATE notification_deliveries SET status='RETRY_PENDING',last_error_code='AMBIGUOUS_SEND'")
        db.execute('ALTER TABLE notification_deliveries ADD COLUMN created_at TEXT')
        db.execute('UPDATE notification_deliveries SET created_at=?',(NOW.isoformat(),))
    from api.services.billing.reconciliationService import ReconciliationService
    from api.services.billing.manualBillingRepository import ManualBillingRepository
    return ReconciliationService(repository=ManualBillingRepository(deliveryRepo.connectionFactory)),path


def test_reports_include_all_manual_obligations_and_stable_pagination(obligations):
    service,_=obligations
    page=service.listManualObligations(limit=2)
    rows=list(page['items'])
    while page['nextCursor']:
        page=service.listManualObligations(limit=2,cursor=page['nextCursor'])
        rows.extend(page['items'])
    assert {row['id'] for row in rows}=={'ack','capture','unmapped','refund','usage','bridge','delivery-one'}
    assert len(rows)==7 and page['total']==7
    assert page['totals']['refund_obligation']==1
    assert page['totals']['delivery_ambiguous']==1
    assert page['available']
    assert 'private reason' not in json.dumps(rows) and 'private case' not in json.dumps(rows)
    assert next(row for row in rows if row['id']=='capture')['userId'] is None


def test_metrics_match_report_categories(obligations):
    service,_=obligations
    from api.services.billing.billingMetricsService import BillingMetricsService
    metrics=BillingMetricsService.__new__(BillingMetricsService)
    metrics.manualRepository=service.repository
    assert metrics.collectManualObligations()['totals']==service.listManualObligations()['totals']


def test_database_failure_is_unavailable_not_zero():
    from api.services.billing.reconciliationService import ReconciliationService
    class Broken:
        def _run(self,_): raise RuntimeError('contains private credentials')
    report=ReconciliationService(repository=Broken()).listManualObligations()
    assert not report['available'] and report['total'] is None
    assert report['errors']==['billing_database_unavailable']
    assert 'private credentials' not in json.dumps(report)


@pytest.mark.parametrize('status',['FAILED','CANCELLED'])
def test_terminal_ambiguous_delivery_remains_an_operator_obligation(obligations,status):
    service,path=obligations
    with sqlTransaction(path) as db:
        db.execute("UPDATE notification_deliveries SET status=?,last_error_code='AMBIGUOUS_SEND_UNRESOLVED',user_id=null",(status,))
    report=service.listManualObligations()
    delivery=next(row for row in report['items'] if row['source']=='delivery')
    assert delivery['category']=='delivery_ambiguous'
    assert delivery['safeAction']=='reconcile_original_tracking_tag'
    assert delivery['userId'] is None
