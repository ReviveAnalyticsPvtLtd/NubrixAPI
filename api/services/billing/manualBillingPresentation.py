"""Public billing responses from persisted intent and committed coverage facts."""

def serializePeriod(period):
    if period is None:
        return None
    return {'start':period.start.isoformat(), 'end':period.end.isoformat(),
        'domains':list(period.domains), 'creditPeriodId':period.creditPeriodId}


def serializeCheckoutIntent(intent, publicKey, identity=None):
    state = {'created':'payment_pending', 'pending_provider_ack':'payment_pending',
        'authorized':'awaiting_capture', 'captured':'already_finalized',
        'expired':'expired', 'cancelled':'cancelled', 'superseded':'superseded'}.get(intent.state,'requires_reconciliation')
    deadline = None if intent.billingMode == 'annual_prepaid' and intent.expiresAt.year == 9999 else intent.expiresAt.isoformat()
    return {**(identity or {}), 'userId':intent.userId, 'invoiceId':intent.invoiceId,
        'attemptId':intent.attemptId, 'razorpayOrderId':intent.razorpayOrderId, 'orderId':intent.razorpayOrderId,
        'razorpayKeyId':publicKey, 'razorpayKey':publicKey, 'amount':intent.amount,
        'currency':intent.currency, 'expiresAt':deadline, 'state':state, 'billingMode':intent.billingMode,
        'domains':intent.snapshot.get('domains') or [], 'quantity':len(intent.snapshot.get('domains') or []),
        'tokens':intent.snapshot.get('tokens'), 'packId':intent.snapshot.get('packId'),
        'period':{'start':intent.snapshot.get('periodStart'), 'end':intent.snapshot.get('periodEnd'),
            'estimated':intent.purpose == 'initial_purchase'}}


def serializeFinalizationResult(result):
    state = {'expert_activated':'activated', 'topup_granted':'activated', 'elapsed':'expired',
        'unchanged':'awaiting_finalization'}.get(result.state,result.state)
    return {'verified':True, 'invoiceId':result.invoiceId, 'attemptId':result.attemptId,
        'state':state, 'finalized':result.finalized, 'alreadyFinalized':result.state == 'already_finalized',
        'currentPeriod':serializePeriod(result.currentPeriod), 'nextPeriod':serializePeriod(result.nextPeriod),
        'creditsRefilled':result.creditsRefilled, 'creditState':result.creditState,
        'renewalOptOut':result.renewalOptOut, 'anomalyId':result.anomalyId,
        'invoiceStatus':result.invoiceStatus}


def serializeInvoice(invoice):
    """Financial history with an allowlisted public coverage snapshot."""
    import json
    fields = ('id','subscription_id','status','amount','total_amount','currency','billing_reason',
        'payment_flow','requires_customer_auth','due_date','expires_at','period_start','period_end',
        'amount_before_tax','tax_amount','tax_breakdown_json','tax_rule_version','place_of_supply_snapshot',
        'pricing_version','razorpayPaymentId','razorpay_order_id','paidAt','createdAt','created_at','updated_at')
    public = {key:invoice[key] for key in fields if key in invoice}
    metadata = invoice.get('metadata_json') or {}
    metadata = json.loads(metadata) if isinstance(metadata,str) else metadata
    billing = metadata.get('manualBilling') or {}
    public['coverage'] = {key:billing[key] for key in ('coverageState','billingMode','purpose','domains','revokedAt') if key in billing}
    public['domains'] = billing.get('domains') or metadata.get('renewalDomains') or metadata.get('domains') or []
    return public


def subscriptionDisplayFacts(subscription):
    """Display real paid periods; unavailable reads expose an unknown state."""
    from api.services.billing.manualBillingRepository import getManualBillingRepository
    facts = {'billingStateAvailable':False,'renewalOptOut':subscription.get('renewal_opt_out'),
        'cancellationEffectiveEnd':None,'currentPeriod':None,'nextPeriod':None}
    try:
        coverage = getManualBillingRepository().getCoverageSnapshot(subscription['user_id'])
        return {'billingStateAvailable':True,'renewalOptOut':coverage.renewalOptOut,
            'cancellationEffectiveEnd':coverage.finalPaidEnd.isoformat() if coverage.renewalOptOut and coverage.finalPaidEnd else None,
            'currentPeriod':serializePeriod(coverage.currentPeriod),'nextPeriod':serializePeriod(coverage.nextPeriod)}
    except Exception:
        return facts
