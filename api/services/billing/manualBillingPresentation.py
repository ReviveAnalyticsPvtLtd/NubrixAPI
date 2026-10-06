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
        'invoiceStatus':'PAID' if result.finalized else 'PAYMENT_PENDING'}
