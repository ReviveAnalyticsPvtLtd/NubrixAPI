"""IR-01/05/09/20 races; disposable local PostgreSQL is mandatory."""
import os
import uuid
from dataclasses import replace
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from types import SimpleNamespace
from unittest.mock import patch
import psycopg2
from psycopg2.extras import Json
import pytest
from test.test_manual_billing_postgres_integration import postgres, payment, NOW
from api.services.billing.manualBillingContracts import CheckoutRequest, VerifiedPaymentEvidence, RefundQuote
from api.services.billing.manualPaymentService import ManualPaymentService
from api.services.billing.manualBillingRepository import ManualBillingRepository

pytestmark=pytest.mark.skipif(os.environ.get('RUN_MANUAL_BILLING_INTEGRATION')!='1',reason='Disposable PostgreSQL opt-in required; skipped is UNVERIFIED')
REFERENCE={'amount':10000,'currency':'INR','source':'razorpay_plan_fetch'}


def canonical_row(url, userId):
    """Full canonical subscription row (ensureCanonicalSubscription returns a projection)."""
    from psycopg2.extras import RealDictCursor
    with psycopg2.connect(url) as db:
        with db.cursor(cursor_factory=RealDictCursor) as sql:
            sql.execute('select * from public.subscriptions where user_id=%s and is_canonical', (userId,))
            return dict(sql.fetchone())


def initial_checkout_user(postgres):
    user = 'initial-replacement-' + str(uuid.uuid4())
    with psycopg2.connect(postgres) as db:
        with db.cursor() as sql:
            sql.execute('insert into public."Users"("userId") values(%s)', (user,))
    repo = ManualBillingRepository(lambda: psycopg2.connect(postgres))
    repo.ensureCanonicalSubscription(user)
    return repo, user


@pytest.mark.parametrize('replacement_key', [None, 'changed-selection'])
def test_two_sessions_replace_initial_checkout_once(postgres, replacement_key):
    repo, user = initial_checkout_user(postgres)
    with patch('api.services.billing.billingEngine._getMonthlyBasePrice', return_value=REFERENCE):
        first = repo.reserveCheckout(CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid', {'domains': ['banking']}, 'original'))
        repo.bindProviderOrder(first.attemptId, {'id': 'original-' + str(uuid.uuid4())})
        changed = CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid', {'domains': ['telecom']}, replacement_key)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(repo.reserveCheckout, [changed, changed]))
    assert results[0].attemptId == results[1].attemptId != first.attemptId
    with psycopg2.connect(postgres) as db:
        with db.cursor() as sql:
            sql.execute("select payment_status,count(*) from public.billing_events where user_id=%s and event_category='payment_attempt' group by payment_status", (user,))
            assert dict(sql.fetchall()) == {'cancelled': 1, 'created': 1}
            sql.execute('select status from public."Invoices" where id=%s', (first.invoiceId,))
            assert sql.fetchone()[0] == 'VOID'


def test_two_sessions_replace_vs_original_capture_cannot_grant_both(postgres):
    repo, user = initial_checkout_user(postgres)
    with patch('api.services.billing.billingEngine._getMonthlyBasePrice', return_value=REFERENCE):
        first = repo.reserveCheckout(CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid', {'domains': ['banking']}, 'original'))
        first = repo.bindProviderOrder(first.attemptId, {'id': 'original-' + str(uuid.uuid4())})
        money = VerifiedPaymentEvidence(first.attemptId, first.invoiceId, user, first.razorpayOrderId,
            'payment-' + str(uuid.uuid4()), 'initial_purchase', 'INR', 'captured', 'server_observation',
            first.amount, NOW, NOW, None, True)
        def replace_checkout():
            try:
                return repo.reserveCheckout(CheckoutRequest(user, 'initial_purchase', 'monthly_prepaid', {'domains': ['telecom']}, 'changed'))
            except ValueError as exc:
                assert str(exc) == 'EXISTING_PAID_COVERAGE'
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            replacement = pool.submit(replace_checkout)
            capture = pool.submit(repo.finalizeCapturedPayment, money)
            new_intent, result = replacement.result(), capture.result()
    if result.finalized:
        assert new_intent is None
        assert repo.getCoverageSnapshot(user).currentPeriod.domains == ('banking',)
    else:
        assert new_intent is not None and result.state == 'requires_reconciliation'
        assert not repo.getCoverageSnapshot(user).accessAllowed
    with psycopg2.connect(postgres) as db:
        with db.cursor() as sql:
            sql.execute("select count(*) from public.billing_events where provider_payment_id=%s", (money.providerPaymentId,))
            assert sql.fetchone()[0] == 1


class OrderProvider:
    def __init__(self):
        self.orders={};self.lock=Lock();self.fail=True
        self.order=SimpleNamespace(create=self.create,all=self.list,payments=lambda *args:{'items':[]})
    def create(self,payload):
        with self.lock:
            if self.fail:
                self.fail=False
                raise TimeoutError('not submitted')
            order={**payload,'id':'order-'+str(uuid.uuid4())}
            self.orders[order['id']]=order
            return order
    def list(self,params):
        with self.lock: return {'items':[row.copy() for row in self.orders.values() if row['receipt']==params['receipt']]}


def test_two_sessions_retry_zero_order_failure_creates_one_replacement(postgres):
    user='zero-order-'+str(uuid.uuid4())
    with psycopg2.connect(postgres) as db:
        with db.cursor() as sql: sql.execute('insert into public."Users"("userId") values(%s)',(user,))
    repo=ManualBillingRepository(lambda:psycopg2.connect(postgres))
    provider=OrderProvider();service=ManualPaymentService.forProduction(provider,repo)
    request=CheckoutRequest(user,'initial_purchase','monthly_prepaid',{'domains':['banking']},None)
    with patch('api.services.billing.billingEngine._getMonthlyBasePrice',return_value=REFERENCE):
        with pytest.raises(RuntimeError): service.createCheckout(request)
        with psycopg2.connect(postgres) as db:
            with db.cursor() as sql:
                sql.execute("update public.billing_events set metadata_json=jsonb_set(metadata_json,'{manualBilling,orderCreationClaimedAt}',to_jsonb((clock_timestamp()-interval '1 hour')::text)) where user_id=%s and event_category='payment_attempt'",(user,))
        def retry(_):
            try:return service.createCheckout(request)
            except RuntimeError:return None  # concurrent acknowledgement still in flight
        with ThreadPoolExecutor(max_workers=2) as pool: list(pool.map(retry,range(2)))
        final=service.createCheckout(request)
    assert final.razorpayOrderId and len(provider.orders)==1
    with psycopg2.connect(postgres) as db:
        with db.cursor() as sql:
            sql.execute("select payment_status,count(*) from public.billing_events where user_id=%s and event_category='payment_attempt' group by payment_status",(user,))
            assert dict(sql.fetchall())=={'failed':1,'pending_provider_ack':1}


def test_two_sessions_cancel_addition_does_not_lose_other_reservation(payment):
    repo,initial,url=payment;repo.finalizeCapturedPayment(initial)
    def reserve(domain,key):
        return repo.reserveCheckout(CheckoutRequest(initial.userId,'expert_addition','monthly_prepaid',{'domains':[domain]},key))
    with patch('api.services.billing.billingEngine._getMonthlyBasePrice',return_value=REFERENCE):
        first=reserve('telecom','first')
        with ThreadPoolExecutor(max_workers=2) as pool:
            cancel=pool.submit(repo.cancelExpertAddition,initial.userId,'telecom')
            other=pool.submit(reserve,'manufacturing','other')
            cancel.result();second=other.result()
    row=canonical_row(url,initial.userId)
    states={item['attemptId']:item['state'] for item in row['pending_additions']}
    assert states[first.attemptId]=='cancelled' and states[second.attemptId]=='awaiting_payment'


def test_two_sessions_cancel_vs_capture_cannot_overwrite_activated_addition(payment):
    repo,initial,url=payment;repo.finalizeCapturedPayment(initial)
    with patch('api.services.billing.billingEngine._getMonthlyBasePrice',return_value=REFERENCE):
        intent=repo.reserveCheckout(CheckoutRequest(initial.userId,'expert_addition','monthly_prepaid',{'domains':['telecom']},'addition'))
    order='order-'+str(uuid.uuid4());repo.bindProviderOrder(intent.attemptId,{'id':order})
    money=VerifiedPaymentEvidence(intent.attemptId,intent.invoiceId,initial.userId,order,'pay-'+str(uuid.uuid4()),'expert_addition','INR','captured','server_observation',intent.amount,NOW,None,None,False)
    def cancel():
        try:return repo.cancelExpertAddition(initial.userId,'telecom')
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        closed=pool.submit(cancel);captured=pool.submit(repo.finalizeCapturedPayment,money)
        closed.result();result=captured.result()
    row=canonical_row(url,initial.userId)
    state=next(item['state'] for item in row['pending_additions'] if item['attemptId']==intent.attemptId)
    if result.finalized:
        assert state=='activated' and 'telecom' in row['subscribed_experts']
    else:
        assert result.state=='requires_reconciliation' and state=='cancelled' and 'telecom' not in row['subscribed_experts']


def test_two_sessions_refund_replay_after_reserve_submits_once(payment,monkeypatch):
    from api.services.billing.subscriptionRefundService import SubscriptionRefundService,_ProductionRefundStore
    repo,money,url=payment;period=repo.finalizeCapturedPayment(money).currentPeriod
    quote=RefundQuote('quote-'+str(uuid.uuid4()),money.userId,'email-case','INR',NOW,NOW+timedelta(minutes=5),3000,
        ({'invoiceId':money.invoiceId},),True,False)
    repo.saveRefundQuote('staff',quote,'Approved unused time')
    with patch('api.services.billing.manualBillingRepository._now',return_value=NOW):
        reserved=repo.reserveUnusedTimeRefund(quote.quoteId,'staff','email-case','Approved unused time',3000,'approval')
    calls=[];lock=Lock()
    class RefundProvider:
        def verifyUnreturnedCapture(self,item):raise AssertionError('Replay must reuse frozen reservation')
        def refund(self,payment,amount,intent):
            with lock:calls.append((payment,amount,intent))
            return {'id':'refund-'+intent,'payment_id':payment,'amount':amount,'status':'processed'}
    monkeypatch.setattr('api.services.billing.manualBillingRepository.getManualBillingRepository',lambda:repo)
    service=SubscriptionRefundService(store=_ProductionRefundStore(object()),provider=RefundProvider())
    payload={'userId':money.userId,'invoiceIds':[money.invoiceId],'quoteId':quote.quoteId,'expectedTotalAmount':3000,'caseReference':'email-case','reason':'Approved unused time'}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:service.initiateUnusedTimeRefund('staff',payload,'approval'),range(2)))
    assert len(calls)==1
    assert all(row.refundIntentId==reserved.refundIntentId for row in results)
    assert service.initiateUnusedTimeRefund('staff',payload,'approval').refundState=='processed'


# -- renewal solicitation hold vs dispatch (owner-locked authorization) --------

def _heldReady(payment):
    """Paid cycle ending in five real days with an unpaid renewal and a claimed T-7 delivery.

    Authorization reads clock_timestamp(), so the cycle is moved onto the real clock.
    """
    from api.services.notifications.notificationDeliveryRepository import NotificationDeliveryRepository
    repo, money, url = payment
    period = repo.finalizeCapturedPayment(money).currentPeriod
    invoice = str(uuid.uuid4())
    with psycopg2.connect(url) as db:
        with db.cursor() as sql:
            sql.execute("update public.subscriptions set current_period_end=date_trunc('second',now())+interval '5 days' where id=%s returning current_period_end",
                        (period.subscriptionId,))
            end = sql.fetchone()[0]
            sql.execute('''insert into public."Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,currency,period_start,period_end,metadata_json)
                values(%s,%s,%s,'UPCOMING','renewal',3000,'INR',%s,%s + interval '1 month',%s)''',
                (invoice, money.userId, period.subscriptionId, end, end,
                 Json({'manualBilling': {'lifecycleId': period.lifecycleId, 'billingMode': 'monthly_prepaid'}})))
    deliveries = NotificationDeliveryRepository(lambda: psycopg2.connect(url))
    row, _ = deliveries.enqueueBillingNotification(money.userId, period.subscriptionId, 'monthly_renewal_ready',
        'ready-hold:' + invoice, end.isoformat(), {'invoiceId': invoice})
    with psycopg2.connect(url) as db:
        with db.cursor() as sql:
            sql.execute("update public.notification_deliveries set status='SENDING',lease_owner='hold-worker',claimed_payload_version=payload_version,lease_expires_at=now()+interval '5 minutes' where id=%s",
                        (row['id'],))
    return SimpleNamespace(repo=repo, url=url, user=money.userId, sub=period.subscriptionId, invoice=invoice,
                           end=end, deliveries=deliveries, delivery=str(row['id']))


def _recordCapture(sql, case, invoice=None, status='REQUIRES_RECONCILIATION'):
    key = 'held-' + str(uuid.uuid4())
    sql.execute('''insert into public.billing_events(user_id,subscription_id,invoice_id,event_category,event_type,event_status,
        provider,provider_payment_id,amount,currency,idempotency_key,occurred_at,created_at)
        values(%s,%s,%s,'reconciliation','payment.capture',%s,'razorpay',%s,3000,'INR',%s,clock_timestamp(),clock_timestamp())
        returning created_at''', (case.user, case.sub, invoice or case.invoice, status, 'pay-' + key, key))
    return sql.fetchone()[0]


def _submissionStarted(case):
    with psycopg2.connect(case.url) as db:
        with db.cursor() as sql:
            sql.execute('select submission_started_at from public.notification_deliveries where id=%s', (case.delivery,))
            return sql.fetchone()[0]


def test_hold_predicate_can_use_the_unresolved_money_index(payment):
    """The predicate runs under the owner lock for every solicitation; it must not scan history."""
    class ExplainingCursor:
        def __init__(self, sql):
            self.sql, self.plans = sql, []
        def execute(self, query, parameters=None):
            self.sql.execute('explain ' + query, parameters)
            self.plans.extend(row[0] for row in self.sql.fetchall())
        def fetchone(self):
            return None
    case = _heldReady(payment)
    holder = psycopg2.connect(case.url)
    try:
        with holder.cursor() as sql:
            sql.execute('set local enable_seqscan = off')
            cursor = ExplainingCursor(sql)
            case.repo._unresolvedCycleCaptureLocked(cursor, canonical_row(case.url, case.user), case.end)
    finally:
        holder.rollback(); holder.close()
    assert any('idx_billing_events_unresolved_cycle_money' in line for line in cursor.plans), cursor.plans


def test_capture_committed_under_owner_lock_holds_waiting_dispatch(payment):
    import time
    case = _heldReady(payment)
    holder = psycopg2.connect(case.url)
    try:
        with holder.cursor() as sql:
            case.repo._lockUser(sql, case.user)
            _recordCapture(sql, case)
            with ThreadPoolExecutor(max_workers=1) as pool:
                waiting = pool.submit(case.deliveries.authorizeBillingSubmissionResult, case.delivery, 'hold-worker', 1)
                time.sleep(0.5)
                assert not waiting.done(), 'authorization must wait for the financial owner lock'
                holder.commit()
                assert waiting.result() == 'HELD'
    finally:
        holder.close()
    assert _submissionStarted(case) is None


def test_two_sessions_capture_vs_dispatch_never_authorize_after_hold(payment):
    case = _heldReady(payment)
    def capture():
        with psycopg2.connect(case.url) as db:
            with db.cursor() as sql:
                case.repo._lockUser(sql, case.user)
                return _recordCapture(sql, case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = pool.submit(case.deliveries.authorizeBillingSubmissionResult, case.delivery, 'hold-worker', 1)
        held = pool.submit(capture)
        outcome, capturedAt = sent.result(), held.result()
    assert outcome in ('AUTHORIZED', 'HELD')
    started = _submissionStarted(case)
    if outcome == 'AUTHORIZED':
        assert started < capturedAt
    else:
        assert started is None


def test_two_sessions_resolution_vs_dispatch_never_send_held_money(payment):
    case = _heldReady(payment)
    with psycopg2.connect(case.url) as db:
        with db.cursor() as sql:
            _recordCapture(sql, case)
    def resolve():
        with psycopg2.connect(case.url) as db:
            with db.cursor() as sql:
                case.repo._lockUser(sql, case.user)
                sql.execute("update public.billing_events set event_status='FINALIZED' where invoice_id=%s and event_type='payment.capture'", (case.invoice,))
                sql.execute('update public."Invoices" set status=%s where id=%s', ('PAID', case.invoice))
    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = pool.submit(case.deliveries.authorizeBillingSubmissionResult, case.delivery, 'hold-worker', 1)
        done = pool.submit(resolve)
        outcome = sent.result(); done.result()
    assert outcome in ('HELD', 'REJECTED')
    assert _submissionStarted(case) is None


def test_two_sessions_cancellation_vs_held_dispatch_never_authorize(payment):
    case = _heldReady(payment)
    with psycopg2.connect(case.url) as db:
        with db.cursor() as sql:
            _recordCapture(sql, case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = pool.submit(case.deliveries.authorizeBillingSubmissionResult, case.delivery, 'hold-worker', 1)
        cancelled = pool.submit(case.repo.setRenewalOptOut, case.user, True, 'Finished project', 'hold-optout')
        outcome = sent.result(); cancelled.result()
    assert outcome in ('HELD', 'REJECTED')
    assert case.deliveries.authorizeBillingSubmissionResult(case.delivery, 'hold-worker', 1) == 'REJECTED'


def test_two_sessions_repricing_vs_held_dispatch_never_authorize(payment):
    case = _heldReady(payment)
    with psycopg2.connect(case.url) as db:
        with db.cursor() as sql:
            _recordCapture(sql, case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = pool.submit(case.deliveries.authorizeBillingSubmissionResult, case.delivery, 'hold-worker', 1)
        revised = pool.submit(case.deliveries.enqueueBillingNotification, case.user, case.sub, 'monthly_renewal_ready',
            'ready-hold:' + case.invoice, case.end.isoformat(), {'invoiceId': case.invoice, 'repriced': True})
        outcome = sent.result(); revised.result()
    assert outcome in ('HELD', 'REJECTED')
    assert _submissionStarted(case) is None


@pytest.mark.parametrize('boundary', ['future_paid_cycle', 'previous_cycle'])
def test_capture_on_another_cycle_boundary_does_not_hold_this_renewal(payment, boundary):
    case = _heldReady(payment)
    other = str(uuid.uuid4())
    shift = timedelta(days=31) if boundary == 'future_paid_cycle' else -timedelta(days=31)
    with psycopg2.connect(case.url) as db:
        with db.cursor() as sql:
            sql.execute('''insert into public."Invoices"(id,"userId",subscription_id,status,billing_reason,total_amount,currency,period_start,period_end,metadata_json)
                values(%s,%s,%s,'UPCOMING','renewal',3000,'INR',%s,%s,'{}')''',
                (other, case.user, case.sub, case.end + shift, case.end + shift + timedelta(days=30)))
            _recordCapture(sql, case, invoice=other)
    assert case.deliveries.authorizeBillingSubmissionResult(case.delivery, 'hold-worker', 1) == 'AUTHORIZED'


# -- audited payment-case actions (fake provider: code paths, not live Razorpay) --

class _AttestedPayments:
    def __init__(self, entity):
        self.entity, self.calls = entity, 0
    def fetch(self, paymentId):
        self.calls += 1
        return dict(self.entity)


def _renewalCase(payment):
    """A held T-7 renewal whose real capture was observed after the checkout deadline."""
    from datetime import datetime, timezone
    case = _heldReady(payment)
    period_lifecycle = canonical_row(case.url, case.user)['billing_state']['manualBilling']['lifecycleId']
    expires = datetime.now(timezone.utc) + timedelta(minutes=30)
    attempt = case.repo.reserveCheckoutIntent(case.user, 'renewal', case.invoice, case.invoice, {
        'subscriptionId': case.sub, 'invoiceId': case.invoice, 'lifecycleId': period_lifecycle,
        'billingMode': 'monthly_prepaid', 'amount': 3000, 'currency': 'INR', 'domains': ['banking'],
        'expiresAt': expires.isoformat()})
    order = 'order-' + str(uuid.uuid4())
    case.repo.bindProviderOrder(attempt.attemptId, {'id': order})
    late = VerifiedPaymentEvidence(attempt.attemptId, case.invoice, case.user, order, 'pay-' + str(uuid.uuid4()),
        'renewal', 'INR', 'captured', 'server_observation', 3000, expires + timedelta(hours=1), None, None, False)
    observed = case.repo.finalizeCapturedPayment(late)
    assert observed.state == 'requires_reconciliation'
    case.capture, case.evidence = str(observed.anomalyId), late
    case.entity = {'id': late.providerPaymentId, 'order_id': order, 'amount': 3000, 'currency': 'INR',
                   'status': 'captured', 'captured_at': int((expires - timedelta(minutes=20)).timestamp())}
    return case


def _caseService(case):
    from api.services.adminPaymentCaseService import AdminPaymentCaseService
    return AdminPaymentCaseService(repository=case.repo, provider=SimpleNamespace(payment=_AttestedPayments(case.entity)))


def _caseAction(service, case, action='recheck', key='case-key', admin=None):
    # Keys are scoped per admin, and this module shares one database.
    from api.adminModels import AdminPaymentCaseActionRequest
    from test.test_admin_credit_resets import ADMIN
    request = AdminPaymentCaseActionRequest(action=action, caseReference='SUP-7', reason='Customer bank statement')
    return service.act(case.capture, request, key + ':' + case.capture, admin or ADMIN)


def _invoiceStatus(case):
    with psycopg2.connect(case.url) as db:
        with db.cursor() as sql:
            sql.execute('select status from public."Invoices" where id=%s', (case.invoice,))
            return sql.fetchone()[0]


def test_recheck_finalizes_original_renewal_with_audit_and_releases_hold(payment):
    from api.services.billing.manualObligationReport import listObligations
    case = _renewalCase(payment)
    assert case.capture in {item['id'] for item in listObligations(case.repo, 500)['items']}
    assert case.deliveries.authorizeBillingSubmissionResult(case.delivery, 'hold-worker', 1) == 'HELD'
    result = _caseAction(_caseService(case), case)
    assert (result['financialStatus'], result['actionOutcome']) == ('FINALIZED', 'FINALIZED_ORIGINAL')
    assert result['finalization']['state'] == 'paid_scheduled' and _invoiceStatus(case) == 'PAID'
    assert case.capture not in {item['id'] for item in listObligations(case.repo, 500)['items']}
    assert case.deliveries.authorizeBillingSubmissionResult(case.delivery, 'hold-worker', 1) == 'REJECTED'
    with psycopg2.connect(case.url) as db:
        with db.cursor() as sql:
            sql.execute("select action,outcome from public.admin_audit_log where target_id=%s", (case.capture,))
            assert sql.fetchall() == [('billing.payment_case.recheck', 'FINALIZED_ORIGINAL')]


def test_two_sessions_note_vs_dispatch_keeps_money_held(payment):
    case = _renewalCase(payment)
    service = _caseService(case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = pool.submit(case.deliveries.authorizeBillingSubmissionResult, case.delivery, 'hold-worker', 1)
        noted = pool.submit(_caseAction, service, case, 'note')
        outcome, note = sent.result(), noted.result()
    assert outcome == 'HELD' and note['actionOutcome'] == 'NOTE_RECORDED' and note['financialStatus'] == 'OPEN'
    assert case.deliveries.authorizeBillingSubmissionResult(case.delivery, 'hold-worker', 1) == 'HELD'
    assert _submissionStarted(case) is None and _invoiceStatus(case) == 'UPCOMING'


def test_two_sessions_recheck_finalization_vs_dispatch_never_authorize(payment):
    case = _renewalCase(payment)
    service = _caseService(case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sent = pool.submit(case.deliveries.authorizeBillingSubmissionResult, case.delivery, 'hold-worker', 1)
        rechecked = pool.submit(_caseAction, service, case)
        outcome, result = sent.result(), rechecked.result()
    assert result['actionOutcome'] == 'FINALIZED_ORIGINAL'
    assert outcome in ('HELD', 'REJECTED') and _submissionStarted(case) is None


def test_two_sessions_duplicate_rechecks_finalize_once(payment):
    from api.adminErrors import AdminApiError
    case = _renewalCase(payment)
    service = _caseService(case)
    def attempt(key):
        try:
            return _caseAction(service, case, key=key)
        except AdminApiError as error:
            return error.statusCode
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ['first-key', 'second-key']))
    assert sorted(str(item if isinstance(item, int) else item['actionOutcome']) for item in outcomes) == ['409', 'FINALIZED_ORIGINAL']
    winner = 'first-key' if outcomes[0] != 409 else 'second-key'
    with ThreadPoolExecutor(max_workers=2) as pool:
        replays = list(pool.map(lambda _: _caseAction(service, case, key=winner), range(2)))
    assert replays[0] == replays[1] == next(item for item in outcomes if item != 409)
    with psycopg2.connect(case.url) as db:
        with db.cursor() as sql:
            sql.execute("select count(*) from public.billing_events where event_type='admin.payment_case.action' and user_id=%s", (case.user,))
            assert sql.fetchone()[0] == 1
            sql.execute("select count(*) from public.billing_events where event_type='email.billing_intent.committed' and user_id=%s and metadata_json->>'notificationType'='payment_receipt' and metadata_json->'metadata'->>'paymentId'=%s",
                        (case.user, case.evidence.providerPaymentId))
            assert sql.fetchone()[0] == 1


def test_two_sessions_browser_replay_vs_recheck_grant_once(payment):
    case = _renewalCase(payment)
    service = _caseService(case)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replay = pool.submit(case.repo.finalizeCapturedPayment, case.evidence)
        rechecked = pool.submit(_caseAction, service, case)
        browser, result = replay.result(), rechecked.result()
    assert result['actionOutcome'] == 'FINALIZED_ORIGINAL'
    assert browser.state in ('requires_reconciliation', 'already_finalized')
    assert case.repo.finalizeCapturedPayment(case.evidence).state == 'already_finalized'
    with psycopg2.connect(case.url) as db:
        with db.cursor() as sql:
            sql.execute("select count(*) from public.billing_events where provider_payment_id=%s", (case.evidence.providerPaymentId,))
            assert sql.fetchone()[0] == 1


def test_payment_case_audit_failure_rolls_back_original_finalization(payment):
    from api.adminErrors import AdminApiError
    from test.test_admin_credit_resets import ADMIN
    case = _renewalCase(payment)
    with pytest.raises(AdminApiError) as error:
        _caseAction(_caseService(case), case, admin=replace(ADMIN, email='   '))
    assert error.value.statusCode == 503
    assert _invoiceStatus(case) == 'UPCOMING'
    with psycopg2.connect(case.url) as db:
        with db.cursor() as sql:
            sql.execute('select event_status,metadata_json from public.billing_events where id=%s', (case.capture,))
            status, metadata = sql.fetchone()
            assert status == 'REQUIRES_RECONCILIATION' and 'reconsideredAt' not in metadata
            sql.execute("select count(*) from public.billing_events where event_type='admin.payment_case.action' and user_id=%s", (case.user,))
            assert sql.fetchone()[0] == 0
    assert case.deliveries.authorizeBillingSubmissionResult(case.delivery, 'hold-worker', 1) == 'HELD'
