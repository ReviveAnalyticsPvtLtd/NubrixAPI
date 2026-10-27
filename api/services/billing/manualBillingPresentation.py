"""Public billing responses from persisted intent and committed coverage facts."""

from api.services.subscriptions.subscriptionModel import SubscriptionSnapshotV2


def serializeSubscriptionSnapshot(snapshot: SubscriptionSnapshotV2) -> dict:
    """Allowlist public V2 fields; keep explicit absence separate from history."""
    def timestamp(value):
        return value.isoformat() if value is not None else None

    def period(value):
        if value is None:
            return None
        return {'id': value.id, 'start': timestamp(value.start), 'end': timestamp(value.end),
                'experts': list(value.experts), 'billingInterval': value.billingInterval,
                'creditPeriodId': value.creditPeriodId}

    trial = snapshot.trial
    recovery = snapshot.recoverableCheckout
    return {
        'schemaVersion': snapshot.schemaVersion, 'subscriptionId': snapshot.subscriptionId,
        'lifecycleId': snapshot.lifecycleId, 'planCode': snapshot.planCode,
        'billingInterval': snapshot.billingInterval, 'status': snapshot.status,
        'accessAllowed': snapshot.accessAllowed, 'accessSource': snapshot.accessSource,
        'denialReason': snapshot.denialReason, 'trialAvailable': snapshot.trialAvailable,
        'trial': {'startedAt': timestamp(trial.startedAt), 'endsAt': timestamp(trial.endsAt),
                  'closedAt': timestamp(trial.closedAt)} if trial else None,
        'currentPeriod': period(snapshot.currentPeriod), 'nextPeriod': period(snapshot.nextPeriod),
        'paidUntil': timestamp(snapshot.paidUntil), 'lastPaidEnd': timestamp(snapshot.lastPaidEnd),
        'subscribedExperts': list(snapshot.subscribedExperts), 'domainCount': snapshot.domainCount,
        'nextSelectedExperts': list(snapshot.nextSelectedExperts) if snapshot.nextSelectedExperts is not None else None,
        'renewalOptOut': snapshot.renewalOptOut, 'renewalAvailable': snapshot.renewalAvailable,
        'resumeRenewalAvailable': snapshot.resumeRenewalAvailable,
        'cancellationEffectiveAt': timestamp(snapshot.cancellationEffectiveAt),
        'purchaseAllowed': snapshot.purchaseAllowed, 'purchaseBlockReason': snapshot.purchaseBlockReason,
        'recoverableCheckout': {'invoiceId': recovery.invoiceId, 'attemptId': recovery.attemptId,
                                'state': recovery.state} if recovery else None,
        'evaluatedAt': timestamp(snapshot.evaluatedAt),
    }

def serializePeriod(period):
    if period is None:
        return None
    return {'start':period.start.isoformat(), 'end':period.end.isoformat(),
        'domains':list(period.domains), 'creditPeriodId':period.creditPeriodId}


# Razorpay Standard Checkout `config.display.hide`: EMI and pay-later are not
# offered for any Nubrix payment.
HIDDEN_CHECKOUT_METHODS = ('emi', 'cardless_emi', 'paylater')


_EXPERT_NAMES = {'banking': 'Banking', 'manufacturing': 'Manufacturing', 'supplychain': 'Supply Chain', 'telecom': 'Telecom'}


def purchaseSelectionLabel(snapshot):
    """Customer wording for a frozen selection; never an invoice or attempt identity."""
    names = [_EXPERT_NAMES.get(domain, str(domain)) for domain in snapshot.get('domains') or []]
    interval = 'annual' if snapshot.get('billingMode') == 'annual_prepaid' else 'monthly'
    experts = names[0] if len(names) == 1 else ', '.join(names[:-1]) + ' and ' + names[-1] if names else ''
    purpose = snapshot.get('purpose')
    if purpose == 'topup':
        return 'a credit top-up'
    if purpose == 'expert_addition':
        return 'adding ' + experts if experts else 'adding experts'
    plan = 'Pro ' + interval + (' renewal' if purpose == 'renewal' else '')
    return plan + ' with ' + experts if experts else plan


def _authorizationInFlight(intent):
    """The evidence and window payment status waits on: past created_at + window the payment is dead."""
    from api.services.billing.manualBillingRepository import _now
    from api.services.subscriptions.subscriptionModelBackfill import paymentInFlight
    return paymentInFlight({'event_category': 'payment_attempt', 'payment_status': 'authorized',
        'metadata_json': {'manualBilling': intent.snapshot}, 'attempted_at': intent.attemptStartedAt}, _now())


def serializeCheckoutIntent(intent, publicKey, identity=None):
    # Only money that may still capture holds Pay (spec 4.3): created-only payments leave the Order
    # payable, and a dead authorization retires this replay so the next Pay replaces it.
    state = {'created':'payment_pending', 'pending_provider_ack':'payment_pending',
        'authorized':'awaiting_capture', 'provider_processing':'payment_pending', 'captured':'already_finalized',
        'expired':'expired', 'cancelled':'cancelled', 'superseded':'superseded',
        'confirming_earlier_payment':'confirming_earlier_payment'}.get(intent.state,'requires_reconciliation')
    if intent.state == 'authorized' and not _authorizationInFlight(intent):
        state = 'expired'
    if state == 'confirming_earlier_payment':
        # Only the earlier payment's summary: no Order, key or identity that could reopen checkout on it.
        return {'state': state, 'attemptId': intent.attemptId, 'earlierPayment': dict(intent.earlierPayment or {})}
    deadline = None if intent.billingMode == 'annual_prepaid' and intent.expiresAt.year == 9999 else intent.expiresAt.isoformat()
    return {**(identity or {}), 'userId':intent.userId, 'invoiceId':intent.invoiceId,
        'attemptId':intent.attemptId, 'razorpayOrderId':intent.razorpayOrderId, 'orderId':intent.razorpayOrderId,
        'razorpayKeyId':publicKey, 'razorpayKey':publicKey, 'amount':intent.amount,
        'currency':intent.currency, 'expiresAt':deadline, 'state':state, 'billingMode':intent.billingMode,
        'catalogRevision':intent.snapshot.get('catalogRevision'),
        'domains':intent.snapshot.get('domains') or [], 'quantity':len(intent.snapshot.get('domains') or []),
        'tokens':intent.snapshot.get('tokens'), 'packId':intent.snapshot.get('packId'),
        'period':{'start':intent.snapshot.get('periodStart'), 'end':intent.snapshot.get('periodEnd'),
            'estimated':intent.purpose == 'initial_purchase'},
        'checkoutConfig':{'display':{'hide':[{'method':method} for method in HIDDEN_CHECKOUT_METHODS]}}}


def serializeFinalizationResult(result):
    state = {'expert_activated':'activated', 'topup_granted':'activated', 'elapsed':'expired',
        'unchanged':'awaiting_finalization'}.get(result.state,result.state)
    payload = {'verified':True, 'invoiceId':result.invoiceId, 'attemptId':result.attemptId,
        'state':state, 'finalized':result.finalized, 'alreadyFinalized':result.state == 'already_finalized',
        'currentPeriod':serializePeriod(result.currentPeriod), 'nextPeriod':serializePeriod(result.nextPeriod),
        'creditsRefilled':result.creditsRefilled, 'creditState':result.creditState,
        'renewalOptOut':result.renewalOptOut, 'anomalyId':result.anomalyId,
        'invoiceStatus':result.invoiceStatus}
    subscription = getattr(result, 'subscription', None)
    if subscription is not None:
        payload['subscription'] = serializeSubscriptionSnapshot(subscription)
    return payload


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
    from api.services.subscriptions.subscriptionCompatibility import usesUnifiedSubscription, legacySubscriptionFields
    if usesUnifiedSubscription():
        snapshot = subscription.get('_subscriptionSnapshot')
        if not isinstance(snapshot, SubscriptionSnapshotV2):
            from api.services.subscriptions.subscriptionSnapshotService import getSubscriptionSnapshotService
            snapshot = getSubscriptionSnapshotService().get(subscription['user_id'])
        aliases = legacySubscriptionFields(snapshot)
        aliases.pop('status')  # Login's envelope status remains SUCCESS.
        return {**aliases, 'subscription':serializeSubscriptionSnapshot(snapshot)}
    from api.services.billing.manualBillingRepository import getManualBillingRepository
    facts = {'billingStateAvailable':False,'renewalOptOut':subscription.get('renewal_opt_out'),
        'cancellationEffectiveEnd':None,'currentPeriod':None,'nextPeriod':None,'paidPeriodPrice':None}
    try:
        coverage = getManualBillingRepository().getCoverageSnapshot(subscription['user_id'])
        try:
            paid = getManualBillingRepository().getPaidPeriodPrice(subscription['user_id'])
            if paid and (not coverage.currentPeriod or paid['periodStart'] != coverage.currentPeriod.start.isoformat()
                         or paid['periodEnd'] != coverage.currentPeriod.end.isoformat()): paid = None
        except Exception:
            paid = None
        return {'billingStateAvailable':True,'renewalOptOut':coverage.renewalOptOut,
            'cancellationEffectiveEnd':coverage.finalPaidEnd.isoformat() if coverage.renewalOptOut and coverage.finalPaidEnd else None,
            'currentPeriod':serializePeriod(coverage.currentPeriod),'nextPeriod':serializePeriod(coverage.nextPeriod),
            'paidPeriodPrice':paid}
    except Exception:
        return facts
