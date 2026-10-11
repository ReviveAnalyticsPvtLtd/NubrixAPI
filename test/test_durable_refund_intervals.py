from datetime import timedelta
from unittest.mock import patch
import pytest
from api.services.billing.manualBillingContracts import RefundQuote
from test.test_manual_billing_runtime import database,USER,NOW,seed_payment,sqlTransaction,read_row
from test.test_manual_checkout_http import checkout_database
from test.test_manual_payment_entrypoints import paid_then_request,evidence


@pytest.mark.parametrize('days',[28,29,30,31])
@pytest.mark.parametrize('amount',[3000,599])
@pytest.mark.parametrize('hours',[0,12])
def test_actual_refund_uses_integer_unused_duration_and_exact_cutoff(database,days,amount,hours):
    repo,path=database
    paid=seed_payment(database)
    repo.finalizeCapturedPayment(paid)
    end=NOW+timedelta(days=days)
    cutoff=NOW+timedelta(days=10,hours=hours)
    with sqlTransaction(path) as db:
        db.execute('UPDATE "Invoices" SET period_end=?,total_amount=?',(end.isoformat(),amount))
        db.execute('UPDATE subscriptions SET current_period_end=?',(end.isoformat(),))
        db.execute('UPDATE credit_balances SET topup_tokens=123')
    expected=amount*((end-cutoff)//timedelta(microseconds=1))//((end-NOW)//timedelta(microseconds=1))
    quote=RefundQuote('duration-quote',USER,'email-case','INR',cutoff,cutoff+timedelta(minutes=5),expected,
        ({'invoiceId':paid.invoiceId},),True,False)
    repo.saveRefundQuote('staff',quote,'Approved unused time')
    with patch('api.services.billing.manualBillingRepository._now',return_value=cutoff):
        intent=repo.reserveUnusedTimeRefund(quote.quoteId,'staff','email-case','Approved unused time',expected,'duration-refund')
    assert intent.amount==expected
    assert read_row(path,'subscriptions')['current_period_end']==cutoff.isoformat()
    assert read_row(path,'subscriptions')['status']=='expired'
    assert read_row(path,'credit_balances')['topup_tokens']==123


@pytest.mark.parametrize('futureOnly',[False,True])
def test_actual_refund_selection_settles_paid_future_without_reopening_current(checkout_database,futureOnly):
    repo,path=checkout_database
    manual,checkout=paid_then_request(checkout_database,'monthly_prepaid','renewal')
    intent=manual.createCheckout(checkout)
    paid=manual.finalizeCapturedPayment(evidence(intent))
    cutoff=NOW+timedelta(days=10)
    futureAmount=intent.amount
    items=[{'invoiceId':intent.invoiceId}]
    amount=futureAmount
    if not futureOnly:
        with sqlTransaction(path) as db:
            current=db.execute('SELECT id,total_amount,period_start,period_end FROM "Invoices" WHERE id=?',(paid.currentPeriod.invoiceId,)).fetchone()
        from api.services.subscriptions.paymentValidationService import parseUtc
        amount+=current[1]*((parseUtc(current[3])-cutoff)//timedelta(microseconds=1))//((parseUtc(current[3])-parseUtc(current[2]))//timedelta(microseconds=1))
        items.append({'invoiceId':current[0]})
    quote=RefundQuote('future-selection',USER,'email-case','INR',cutoff,cutoff+timedelta(minutes=5),amount,tuple(items),not futureOnly,futureOnly)
    repo.saveRefundQuote('staff',quote,'Approved unused time')
    with patch('api.services.billing.manualBillingRepository._now',return_value=cutoff):
        reserved=repo.reserveUnusedTimeRefund(quote.quoteId,'staff','email-case','Approved unused time',amount,'future-refund')
    snapshot=repo.getCoverageSnapshot(USER,cutoff)
    assert snapshot.accessAllowed==futureOnly and snapshot.nextPeriod is None
    assert reserved.amount==amount and len(reserved.items)==(1 if futureOnly else 2)
    futureStart=paid.nextPeriod.start
    assert not repo.activateDueCoverage(USER,futureStart).creditsRefilled


def test_removal_from_paid_next_cycle_is_rejected(checkout_database):
    from dateutil.relativedelta import relativedelta
    from test.test_manual_checkout_http import request
    from test.test_manual_payment_entrypoints import RecoverableProvider
    from api.services.billing.manualPaymentService import ManualPaymentService
    repo,path=checkout_database
    manual=ManualPaymentService.forProduction(RecoverableProvider(path),repo)
    initial=manual.createCheckout(request('two-experts',domains=('banking','telecom')))
    period=manual.finalizeCapturedPayment(evidence(initial)).currentPeriod
    row=read_row(path,'subscriptions')
    invoice=repo.createFrozenRenewalInvoice({'userId':USER,'subscription_id':row['id'],
        'billing_reason':'renewal','status':'UPCOMING','total_amount':3000,'currency':'INR',
        'period_start':period.end.isoformat(),'period_end':(period.end+relativedelta(months=1)).isoformat(),
        'metadata_json':{'manualBilling':{'lifecycleId':period.lifecycleId,'domains':['banking','telecom']}}},row['version'])
    future=manual.createCheckout(request('paid-future',purpose='renewal',invoiceId=invoice['id']))
    manual.finalizeCapturedPayment(evidence(future,'future-payment'))
    before=repo.getCoverageSnapshot(USER,NOW)
    with pytest.raises(ValueError,match='PAID_FUTURE_SELECTION_IMMUTABLE'):
        repo.scheduleExpertRemoval(USER,['telecom'])
    after=repo.getCoverageSnapshot(USER,NOW)
    assert after.currentPeriod==before.currentPeriod and after.nextPeriod==before.nextPeriod
