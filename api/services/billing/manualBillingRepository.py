"""Transactional repository for manual monthly billing mutations.

Every operation runs inside ONE PostgreSQL transaction on a psycopg2
connection, following the connection-factory pattern used by
notificationDeliveryRepository.py. Lock order per spec 02:
per-user advisory lock -> canonical subscription -> invoices (by id) ->
attempts/events (by id) -> credit balance. Provider network calls never
happen inside these methods.
"""

__all__ = [
    "ManualBillingRepository",
    "getManualBillingRepository",
    "defaultConnectionFactory",
]


import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from api.services.billing.paymentPolicy import captureWindowMinutes
from api.services.billing.manualBillingContracts import (
    CheckoutIntent,
    CheckoutProviderSnapshot,
    CheckoutRequest,
    CoveragePeriod,
    CoverageSnapshot,
    FinalizationResult,
    RefundIntent,
    RefundQuote,
    VerifiedPaymentEvidence,
)


def defaultConnectionFactory():
    databaseUrl = os.environ.get("DATABASE_URL")
    if not databaseUrl:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg2.connect(
        databaseUrl,
        application_name="nubrix-manual-billing",
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    from dateutil import parser as dateparser

    try:
        parsed = dateparser.isoparse(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _isoText(value):
    """Stored invoice rows return datetimes; frozen JSON snapshots need ISO text."""
    return value.isoformat() if isinstance(value, datetime) else value


def _payloadHash(payload: dict) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _advisoryKey(userId: str) -> int:
    digest = hashlib.sha256(("manual-billing:" + str(userId)).encode("utf-8"))
    return int(digest.hexdigest()[:15], 16)


class ManualBillingRepository:
    @staticmethod
    def _usesPurchaseAuthority():
        from api.services.subscriptions.subscriptionCompatibility import getSubscriptionModelPhase
        return getSubscriptionModelPhase() in ('dual', 'v2', 'contracted')

    def _modelRepository(self):
        from api.services.subscriptions.subscriptionModelRepository import SubscriptionModelRepository
        return SubscriptionModelRepository(self.connectionFactory)

    def runtimeSnapshotFactsLoader(self, userId, now=None):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                row = self._canonical(cursor, userId)
                evaluated = self._modelRepository()._now(cursor, now)
                return self._modelRepository().loadRuntimeFacts(cursor, row, evaluated), evaluated
        return self._run(operation)

    def _purchaseFactsLocked(self, cursor, subscription, owner, now):
        from dataclasses import replace
        from api.services.subscriptions.subscriptionModel import SubscriptionFacts
        from api.services.subscriptions.subscriptionModelBackfill import unresolvedMoneyFacts, _checkHistoricalService
        model = self._modelRepository()
        periods = model.loadAcceptedPeriods(cursor, str(subscription['id']))
        lifecycle = str(subscription.get('lifecycle_id') or '') or (max(periods, key=lambda p: p.start).lifecycleId if periods else None)
        effective = tuple(replace(p, experts=model.loadEffectiveExperts(cursor, p.id, now)) for p in periods)
        _, invoices, events, others = model._accountPurchaseRecords(cursor, str(subscription['id']))
        try:
            _checkHistoricalService(subscription, others, invoices, events, now)
        except (ValueError, KeyError, TypeError) as exc:
            from api.services.subscriptions.subscriptionModel import SubscriptionAuthorityUnavailable
            raise SubscriptionAuthorityUnavailable('Owned historical service requires review') from exc
        unresolved, attempts = unresolvedMoneyFacts(invoices, events)
        current = next((p for p in effective if p.start <= now < p.end and (p.revokedAt is None or now < p.revokedAt)), None)
        selection = model.loadNextSelection(cursor, str(subscription['id']), lifecycle, current.end) if current else None
        return SubscriptionFacts(subscription['user_id'], str(subscription['id']), lifecycle,
            'pro' if periods else None, True, periods=effective, renewalOptOut=bool(subscription.get('renewal_opt_out')),
            accountBanned=bool(owner['isBanned']), erasurePending=bool(subscription.get('erasure_pending')),
            moneyUnresolved=unresolved, recoverableCheckout=attempts[0] if len(attempts) == 1 else None,
            nextSelectedExperts=tuple(selection['experts']) if selection else None)

    def _purchaseCoverageLocked(self, cursor, subscription, owner, now):
        from api.services.subscriptions.subscriptionModel import evaluateSubscription
        facts = self._purchaseFactsLocked(cursor, subscription, owner, now)
        snapshot = evaluateSubscription(facts, now)
        def legacyPeriod(period):
            if period is None:
                return None
            return CoveragePeriod(period.userId, period.subscriptionId, period.lifecycleId, period.creditPeriodId or '',
                period.invoiceId, period.start, period.end, period.experts,
                'annual_prepaid' if period.billingInterval == 'year' else 'monthly_prepaid', period.revokedAt)
        current, upcoming = legacyPeriod(snapshot.currentPeriod), legacyPeriod(snapshot.nextPeriod)
        mode = current.billingMode if current else 'none'
        return CoverageSnapshot(subscription['user_id'], str(subscription['id']), facts.lifecycleId, mode, now,
            current, upcoming, snapshot.paidUntil, snapshot.renewalOptOut, snapshot.accessAllowed, snapshot.denialReason)

    def _purchaseContextLocked(self, cursor, subscription, coverage, now):
        if not coverage.currentPeriod or not coverage.accessAllowed:
            raise ValueError('PAID_COVERAGE_REQUIRED')
        period = coverage.currentPeriod
        selected = self._modelRepository().loadNextSelection(cursor, str(subscription['id']), period.lifecycleId, period.end)
        experts = list(period.domains)
        nextExperts = selected['experts'] if selected else experts
        state = self._json(subscription.get('billing_state'))
        state.setdefault('manualBilling', {})['lifecycleId'] = period.lifecycleId
        return {**subscription, 'billing_mode': period.billingMode, 'current_period_start': period.start,
            'current_period_end': period.end, 'subscribed_experts': experts, 'domain_count': len(experts),
            'pending_removals': [e for e in experts if e not in nextExperts], 'billing_state': state,
            '_verifiedCoverage': {'experts': experts, 'nextExperts': nextExperts,
                'selectionEventId': selected['id'] if selected else None,
                'selectionVersion': selected.get('version') if selected else None,
                'servicePeriodId': period.invoiceId}}

    def getRenewalContext(self, userId, now=None):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                row = self._canonical(cursor, userId)
                evaluated = _utc(now) if now is not None else self._modelRepository()._now(cursor)
                coverage = self._coverageSnapshotLocked(cursor, row, evaluated, materialize=False)
                return self._purchaseContextLocked(cursor, row, coverage, evaluated)
        return self._run(operation)

    def _pendingExpertAttemptsLocked(self, cursor, subscription, periodId):
        cursor.execute("""SELECT * FROM public.billing_events WHERE user_id=%s AND subscription_id=%s
            AND event_category='payment_attempt' AND payment_status IN ('created','pending_provider_ack','authorized')
            ORDER BY id FOR UPDATE""", (subscription['user_id'], subscription['id']))
        return [a for a in cursor.fetchall() if self._json(a.get('metadata_json')).get('manualBilling', {}).get('purpose') == 'expert_addition'
                and self._json(a['metadata_json'])['manualBilling'].get('servicePeriodId') == periodId
                and not self._json(a['metadata_json'])['manualBilling'].get('closedAt')]

    def __init__(self, connectionFactory=None):
        self.connectionFactory = connectionFactory or defaultConnectionFactory

    # -- transaction helpers ------------------------------------------------

    def _run(self, operation):
        connection = self.connectionFactory()
        try:
            result = operation(connection)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _lockUser(self, cursor, userId: str) -> None:
        cursor.execute("select pg_advisory_xact_lock(%s)", (_advisoryKey(userId),))

    # -- canonical shell ------------------------------------------------------

    def pendingRecoveryRows(self, limit=50):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('''select * from public.billing_events where
                    ((event_category='payment_attempt' and metadata_json->'manualBilling' is not null
                      and (payment_status in ('created','pending_provider_ack','authorized')
                        or (payment_status<>'captured' and metadata_json->'manualBilling'->>'closedAt' is not null
                          and metadata_json->'manualBilling'->>'closureReconciledAt' is null)))
                      or (event_type='refund.intent' and event_status <> 'processed'))
                    and updated_at < now()-interval '15 minutes'
                    order by updated_at,id limit %s''',(limit,))
                return list(cursor.fetchall())
        return self._run(operation)

    def recordRecoveryCheck(self, eventId, outcome=None):
        def operation(connection):
            with connection.cursor() as cursor:
                cursor.execute("update public.billing_events set metadata_json=coalesce(metadata_json,'{}'::jsonb) || %s,updated_at=clock_timestamp() where id=%s",
                    (Json({'lastRecoveryAt':_now().isoformat(),'lastRecoveryOutcome':outcome or 'checked'}),eventId))
        return self._run(operation)

    def _refreshFinancialOwnerLocked(self,cursor,eventId,owner):
        """Erasure can commit between the optimistic owner read and its lock."""
        cursor.execute('SELECT user_id FROM public.billing_events WHERE id=%s',(eventId,))
        current = cursor.fetchone()
        if current is None:
            raise ValueError('FINANCIAL_EVENT_MISSING')
        if current['user_id']!=owner['user_id']:
            if current['user_id'] is not None:
                raise ValueError('FINANCIAL_EVENT_OWNER_CHANGED')
            self._lockUser(cursor,None)
        return {**owner,'user_id':current['user_id']}

    def markClosedAttemptReconciled(self, attemptId):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('select user_id from public.billing_events where id=%s',(attemptId,))
                owner = cursor.fetchone()
                if not owner:
                    raise ValueError('ATTEMPT_MISSING')
                self._lockUser(cursor,owner['user_id'])
                owner = self._refreshFinancialOwnerLocked(cursor,attemptId,owner)
                if owner['user_id'] is not None:
                    self._canonical(cursor,owner['user_id'])
                cursor.execute('select metadata_json from public.billing_events where id=%s for update',(attemptId,))
                row = cursor.fetchone()
                metadata = self._json(row['metadata_json'])
                if metadata.get('manualBilling',{}).get('closedAt'):
                    metadata['manualBilling']['closureReconciledAt'] = _now().isoformat()
                    cursor.execute('update public.billing_events set metadata_json=%s where id=%s',(Json(metadata),attemptId))
        return self._run(operation)

    def orderlessRecoveryRows(self, userId, purpose, mode):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('''select * from public.billing_events where user_id=%s
                    and event_category='payment_attempt' and provider_order_id is null
                    and payment_status='pending_provider_ack' order by id''', (userId,))
                return [row for row in cursor.fetchall()
                        if self._json(row.get('metadata_json')).get('manualBilling', {}).get('purpose') == purpose
                        and self._json(row.get('metadata_json')).get('manualBilling', {}).get('billingMode') == mode]
        return self._run(operation)

    def renewalRecoveryRows(self, userId, mode, invoiceId):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('''select e.* from public.billing_events e
                    join public."Invoices" i on i.id=e.invoice_id
                    where e.user_id=%s and i."userId"=%s and e.invoice_id=%s
                    and e.event_category='payment_attempt' order by e.id''',
                    (userId, userId, invoiceId))
                return [row for row in cursor.fetchall()
                    if self._json(row.get('metadata_json')).get('manualBilling', {}).get('purpose') == 'renewal'
                    and self._json(row.get('metadata_json')).get('manualBilling', {}).get('billingMode') == mode
                    and (row.get('payment_status') != 'created' or row.get('provider_order_id'))
                    and row.get('payment_status') != 'captured']
        return self._run(operation)

    def recordProviderObservation(self, attemptId, snapshot):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('select user_id,invoice_id from public.billing_events where id=%s', (attemptId,))
                owner = cursor.fetchone()
                if not owner:
                    raise ValueError('ATTEMPT_MISSING')
                self._lockUser(cursor, owner['user_id'])
                owner = self._refreshFinancialOwnerLocked(cursor,attemptId,owner)
                if owner['user_id'] is not None:
                    self._canonical(cursor, owner['user_id'])
                cursor.execute('select id from public."Invoices" where id=%s for update', (owner['invoice_id'],))
                cursor.fetchone()
                cursor.execute('select * from public.billing_events where id=%s for update', (attemptId,))
                attempt = cursor.fetchone()
                if (snapshot.attemptId != str(attemptId) or snapshot.providerOrderId != attempt['provider_order_id']
                        or snapshot.amount != int(attempt['amount']) or snapshot.currency != attempt['currency']):
                    raise ValueError('RECOVERY_ORDER_EVIDENCE_MISMATCH')
                metadata = self._json(attempt.get('metadata_json'))
                billing = metadata.setdefault('manualBilling', {})
                previous = billing.get('providerObservation', {})
                if previous.get('outcome') == 'captured' and snapshot.outcome != 'captured':
                    return  # A later non-capture is not proof that captured money vanished.
                if previous.get('checkedAt') and _utc(previous['checkedAt']) > snapshot.observedAt:
                    return
                if previous.get('hasPayments') and not snapshot.payments and snapshot.outcome == 'unpaid':
                    return
                billing['providerObservation'] = {'orderId': snapshot.providerOrderId,
                    'outcome': snapshot.outcome, 'checkedAt': snapshot.observedAt.isoformat(),
                    'complete': snapshot.complete, 'hasPayments': bool(snapshot.payments)}
                status = attempt['payment_status']
                if snapshot.outcome == 'authorized' and (not billing.get('closedAt') or billing.get('purchaseCancellationAcceptedAt')) and status in ('created', 'pending_provider_ack', 'cancelled'):
                    status = 'authorized'
                    if billing.get('purchaseCancellationAcceptedAt'):
                        billing.pop('closureReconciledAt', None)
                cursor.execute('update public.billing_events set metadata_json=%s,payment_status=%s where id=%s',
                    (Json(metadata), status, attemptId))
        return self._run(operation)

    def closeUncreatedOrder(self, attemptId, providerRejected=False):
        """Close after a complete zero-match receipt lookup and a grace period.

        This is operational absence evidence, never capture-timing proof.
        Retain the receipt: subsequently discovered money is an anomaly on
        this closed attempt, not a grant on the replacement checkout.
        ``providerRejected`` is a provider 4xx on creation, which proves no
        Order exists, so the grace period is skipped.
        """
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('select user_id,invoice_id from public.billing_events where id=%s', (attemptId,))
                owner = cursor.fetchone()
                if not owner:
                    return False
                self._lockUser(cursor, owner['user_id'])
                owner = self._refreshFinancialOwnerLocked(cursor,attemptId,owner)
                subscription = self._canonical(cursor, owner['user_id']) if owner['user_id'] is not None else None
                cursor.execute('select * from public."Invoices" where id=%s for update', (owner['invoice_id'],))
                invoice = cursor.fetchone()
                cursor.execute('select * from public.billing_events where id=%s for update', (attemptId,))
                attempt = cursor.fetchone()
                metadata = self._json(attempt.get('metadata_json'))
                billing = metadata.get('manualBilling', {})
                cursor.execute('select clock_timestamp() as current_time')
                now = _utc(cursor.fetchone()['current_time'])
                claimed = _utc(billing.get('orderCreationClaimedAt') or attempt.get('attempted_at'))
                if (attempt.get('provider_order_id') or attempt['payment_status'] == 'captured'
                        or not claimed or (not providerRejected and now < claimed + timedelta(minutes=32)) or invoice['status'] == 'PAID'):
                    return False
                if billing.get('closedAt'):
                    billing['closureReconciledAt'] = now.isoformat()
                    cursor.execute('update public.billing_events set metadata_json=%s where id=%s', (Json(metadata), attemptId))
                    return True
                billing.update(closedAt=now.isoformat(), closedReason='ORDER_NOT_CREATED',
                    zeroOrderLookupAt=now.isoformat(), closureReconciledAt=now.isoformat())
                cursor.execute("update public.billing_events set payment_status='failed',event_status='failed',metadata_json=%s where id=%s",
                    (Json(metadata), attemptId))
                if billing.get('purpose') != 'renewal':
                    self._closeInvoice(cursor, invoice, 'ORDER_NOT_CREATED')
                if subscription and billing.get('purpose') == 'expert_addition':
                    pending = self._jsonList(subscription.get('pending_additions'))
                    for item in pending:
                        if item.get('attemptId') == attemptId and item.get('state') == 'awaiting_payment':
                            item.update(state='failed', closedReason='ORDER_NOT_CREATED')
                    cursor.execute('update public.subscriptions set pending_additions=%s where id=%s', (Json(pending), subscription['id']))
                return True
        return self._run(operation)

    def ensureCanonicalSubscription(self, userId: str) -> dict:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                unified = self._usesPurchaseAuthority()
                fields = '*' if unified else '''id, user_id, billing_mode, status, plan_type,
                    current_period_start, current_period_end, renewal_due_at, auto_renew_enabled,
                    payment_collection_mode, default_currency, version, erasure_pending, is_canonical'''
                cursor.execute(
                    f"""
                    select {fields}
                    from public.subscriptions
                    where user_id = %s and is_canonical = true
                    limit 1
                    """,
                    (userId,),
                )
                row = cursor.fetchone()
                if row is not None:
                    return dict(row)
                cursor.execute("select id from public.subscriptions where user_id=%s limit 1", (userId,))
                if cursor.fetchone() is not None:
                    raise ValueError("CANONICAL_BACKFILL_REQUIRED")
                if unified:
                    cursor.execute('''insert into public.subscriptions
                        (id,user_id,default_currency,is_canonical) VALUES(%s,%s,'INR',true) RETURNING *''',
                        (str(uuid.uuid4()),userId))
                else:
                    cursor.execute(
                    """
                    insert into public.subscriptions (
                        id, user_id, billing_mode, status, plan_type,
                        auto_renew_enabled, payment_collection_mode,
                        default_currency, is_canonical
                    )
                    values (%s, %s, 'none', 'none', 'none', false,
                            'authenticated_checkout', 'INR', true)
                    returning id, user_id, billing_mode, status, plan_type,
                               current_period_start, current_period_end,
                               renewal_due_at, auto_renew_enabled,
                               payment_collection_mode, default_currency,
                               version, erasure_pending, is_canonical
                    """,
                    (str(uuid.uuid4()), userId),
                )
                created = cursor.fetchone()
                if unified:
                    from api.services.subscriptions.subscriptionModelBackfill import observedStorageShape
                    self._modelRepository()._event(cursor, created, 'subscription.model_initialized',
                        'subscription-initialized:'+str(created['id']),
                        {'schemaVersion':2,'storageShape':observedStorageShape(created)})
                return dict(created)

        return self._run(operation)

    def activateTrial(self, userId: str, domains: tuple[str, ...]) -> dict:
        """Consume the existing twelve-day trial once, under the owner lock."""
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                subscription = self._canonical(cursor, userId)
                cursor.execute('select clock_timestamp() as current_time')
                now = _utc(cursor.fetchone()['current_time'])
                if self._usesPurchaseAuthority():
                    from api.services.subscriptions.subscriptionModel import evaluateSubscription
                    facts = self._modelRepository().loadRuntimeFacts(cursor, subscription, now)
                    if not evaluateSubscription(facts, now).trialAvailable or facts.moneyUnresolved:
                        raise ValueError('TRIAL_NOT_ELIGIBLE')
                    if not domains or len(set(domains)) != len(domains) or not set(domains) <= {'banking','manufacturing','supplychain','telecom'}:
                        raise ValueError('INVALID_TRIAL_EXPERTS')
                    end = now + timedelta(days=12)
                    cursor.execute('''UPDATE public.subscriptions SET plan_code='pro',trial_started_at=%s,trial_ends_at=%s,
                        version=version+1,updated_at=%s WHERE id=%s RETURNING *''',
                        (now,end,now,subscription['id']))
                    activated = cursor.fetchone()
                    self._modelRepository()._event(cursor, activated, 'subscription.trial_started',
                        'subscription-trial:'+str(subscription['id']), {'experts':list(domains), 'startedAt':now, 'endsAt':end})
                    from api.services.credits.manualCreditRepository import ManualCreditRepository
                    ManualCreditRepository(self)._balanceLocked(cursor, activated, now)
                    return dict(activated)
                state = self._json(subscription.get('billing_state'))
                cursor.execute('select "isBanned" from public."Users" where "userId"=%s', (userId,))
                owner = cursor.fetchone()
                # Existing trial/paid/churned rows never become first-time users.
                if (not owner or owner.get('isBanned') or subscription.get('erasure_pending')
                        or state.get('trialConsumed') or state.get('churn_snapshot')
                        or subscription.get('status') != 'none'
                        or subscription.get('current_period_start') or subscription.get('current_period_end')):
                    raise ValueError('TRIAL_NOT_ELIGIBLE')
                cursor.execute('select id from public.subscriptions where user_id=%s and is_canonical=false', (userId,))
                if cursor.fetchone():
                    raise ValueError('TRIAL_NOT_ELIGIBLE')
                state.update(trialConsumed=True, trialConsumedAt=now.isoformat())
                end = now + timedelta(days=12)
                cursor.execute('''update public.subscriptions set billing_mode='none',status='trial',plan_type='free',
                    current_period_start=%s,current_period_end=%s,renewal_due_at=%s,
                    subscribed_experts=%s,domain_count=%s,billing_state=%s,version=version+1,updated_at=%s
                    where id=%s and is_canonical=true''',
                    (now,end,end,Json(list(domains)),len(domains),Json(state),now,subscription['id']))
                subscription.update(status='trial',plan_type='free',current_period_start=now,
                    current_period_end=end,billing_state=state,subscribed_experts=list(domains),domain_count=len(domains))
                return subscription
        return self._run(operation)

    # -- checkout intents -----------------------------------------------------

    def _checkoutRequestReplayLocked(self, cursor, request, payloadHash):
        if request.requestKey is None:
            return None
        cursor.execute('''select * from public.billing_events where user_id=%s
            and idempotency_key=%s''',
            (request.userId, f'{request.purpose}:{request.userId}:{request.requestKey}'))
        binding = cursor.fetchone()
        if not binding:
            return None
        if binding.get('event_category') == 'payment_attempt':
            frozen = self._json(binding.get('metadata_json')).get('manualBilling', {})
            if frozen.get('payloadHash') != payloadHash:
                raise ValueError('IDEMPOTENCY_CONFLICT')
            return None if frozen.get('closedReason') == 'ORDER_NOT_CREATED' else binding
        if binding.get('event_type') != 'checkout.request.bound':
            raise ValueError('IDEMPOTENCY_CONFLICT')
        identity = self._json(binding.get('metadata_json')).get('checkoutRequest', {})
        if identity.get('payloadHash') != payloadHash:
            raise ValueError('IDEMPOTENCY_CONFLICT')
        cursor.execute('''select * from public.billing_events where id=%s and user_id=%s
            and event_category='payment_attempt' ''', (identity.get('attemptId'), request.userId))
        attempt = cursor.fetchone()
        if not attempt or str(attempt.get('invoice_id')) != str(binding.get('invoice_id')):
            raise ValueError('CHECKOUT_REQUEST_BINDING_MISMATCH')
        return attempt

    def _bindCheckoutRequestLocked(self, cursor, request, payloadHash, attempt):
        if request.requestKey is None:
            return
        namespace = f'{request.purpose}:{request.userId}:{request.requestKey}'
        if attempt.get('idempotency_key') == namespace:
            return
        cursor.execute('select id from public.billing_events where idempotency_key=%s', (namespace,))
        if cursor.fetchone():
            raise ValueError('IDEMPOTENCY_CONFLICT')
        cursor.execute('''insert into public.billing_events
            (id,user_id,subscription_id,invoice_id,event_category,event_type,event_status,
             idempotency_key,metadata_json,occurred_at)
            values(%s,%s,%s,%s,'audit','checkout.request.bound','processed',%s,%s,%s)''',
            (str(uuid.uuid4()), request.userId, attempt['subscription_id'], attempt['invoice_id'],
             namespace, Json({'checkoutRequest': {'attemptId': str(attempt['id']),
                 'payloadHash': payloadHash}}), _now()))

    def _validateRenewalAttemptLocked(self, cursor, request, subscription, attempt, now):
        if self._usesPurchaseAuthority():
            coverage = self._coverageSnapshotLocked(cursor, subscription, now, materialize=False)
            subscription = self._purchaseContextLocked(cursor, subscription, coverage, now)
        frozen = self._json(attempt.get('metadata_json')).get('manualBilling', {})
        if attempt.get('payment_status') not in ('created', 'pending_provider_ack', 'authorized'):
            return
        if frozen.get('closedAt'):
            raise ValueError('CHECKOUT_CLOSED')
        if ((request.billingMode == 'annual_prepaid' or self._usesPurchaseAuthority())
                and self._coverageSnapshotLocked(cursor, subscription, now, materialize=False).nextPeriod):
            raise ValueError('RENEWAL_ALREADY_PAID')
        end = _utc(subscription.get('current_period_end'))
        lifecycle = self._json(subscription.get('billing_state')).get('manualBilling', {}).get('lifecycleId')
        if (subscription.get('renewal_opt_out') or subscription.get('erasure_pending') or not end
                or request.billingMode != subscription.get('billing_mode')
                or frozen.get('lifecycleId') != lifecycle
                or (request.billingMode == 'monthly_prepaid' and end <= now)):
            raise ValueError('RENEWAL_NOT_ELIGIBLE')
        cursor.execute('select "isBanned" from public."Users" where "userId"=%s', (request.userId,))
        owner = cursor.fetchone()
        if not owner or owner.get('isBanned'):
            raise ValueError('CHECKOUT_OWNER_NOT_ELIGIBLE')
        cursor.execute('select * from public."Invoices" where id=%s and "userId"=%s for update',
            (attempt['invoice_id'], request.userId))
        invoice = cursor.fetchone()
        if (not invoice or str(invoice.get('subscription_id')) != str(subscription['id'])
                or str(invoice['status']).upper() not in ('UPCOMING', 'PAYMENT_PENDING')
                or invoice.get('billing_reason') != 'renewal' or _utc(invoice.get('period_start')) != end):
            raise ValueError('RENEWAL_INVOICE_CLOSED')
        quote = self._json(invoice.get('metadata_json')).get('manualBilling', {})
        if self._usesPurchaseAuthority() and any(quote.get(k) != subscription['_verifiedCoverage'].get(k)
                or frozen.get(k) != quote.get(k) for k in ('selectionEventId', 'selectionVersion')):
            raise ValueError('INVOICE_REVISION_CONFLICT')
        quotedDomains = quote.get('domains') or self._json(invoice.get('metadata_json')).get('renewalDomains')
        selected = set(self._jsonList(subscription.get('subscribed_experts'))) - set(
            self._jsonList(subscription.get('pending_removals')))
        if (int(invoice.get('total_amount') or invoice.get('amount') or 0) != int(frozen.get('frozenAmount') or 0)
                or invoice.get('currency') != frozen.get('currency')
                or _utc(invoice.get('period_end')) != _utc(frozen.get('periodEnd'))
                or set(frozen.get('domains') or []) != selected
                or (quotedDomains is not None and set(quotedDomains) != selected)
                or int(quote.get('revision') or 1) != int(frozen.get('revision') or 1)):
            raise ValueError('INVOICE_REVISION_CONFLICT')

    def reserveCheckout(self, request: CheckoutRequest, *, replayOnly=False) -> CheckoutIntent | None:
        """Freeze server pricing, invoice and attempt in one owner transaction.

        Existing attempts replay before any catalog read. New invoice prices
        are read under a shared catalog lock in this same owner transaction.
        A changed initial selection with a fresh/no key supersedes unpaid
        invoices in the reservation transaction; old money stays auditable.
        replayOnly resolves request identity without reserving or pricing;
        unrelated live-intent conflicts are checked after provider recovery.
        """
        from api.services.billing import billingEngine
        from dateutil.relativedelta import relativedelta
        if request.purpose not in ('initial_purchase', 'renewal', 'expert_addition', 'topup'):
            raise ValueError('INVALID_CHECKOUT_PURPOSE')
        if request.billingMode not in ('monthly_prepaid', 'annual_prepaid'):
            raise ValueError('INVALID_BILLING_MODE')
        if request.requestKey is not None and (not request.requestKey.strip() or len(request.requestKey) > 128):
            raise ValueError('INVALID_REQUEST_KEY')
        if set(request.payload) - {'domains', 'packId', 'invoiceId', 'revision', 'contact'}:
            raise ValueError('INVALID_CHECKOUT_PAYLOAD')
        domains = tuple(sorted(set(str(value).strip().lower() for value in request.payload.get('domains', []))))
        if request.purpose in ('initial_purchase', 'expert_addition') and (not domains or len(domains) > 4
                or not set(domains) <= {'banking', 'manufacturing', 'supplychain', 'telecom'}):
            raise ValueError('INVALID_EXPERT_SELECTION')
        identity = {'billingMode': request.billingMode, 'purpose': request.purpose,
            **{key: value for key, value in request.payload.items() if key in ('packId', 'invoiceId', 'revision')}}
        if domains:
            identity['domains'] = list(domains)
        legacy_hash = _payloadHash(identity)
        if request.currency not in ('INR', 'USD'):
            raise ValueError('BILLING_CURRENCY_UNSUPPORTED')
        if request.purpose == 'initial_purchase':
            identity['currency'] = request.currency
            if request.purchaseDraftRevision is not None:
                identity['purchaseDraftRevision'] = request.purchaseDraftRevision
        payload_hash = _payloadHash(identity)

        def hashMatches(frozen):
            return (frozen.get('payloadHash') == payload_hash or
                    frozen.get('payloadHash') == legacy_hash and
                    (frozen.get('currency') or 'INR') == request.currency)

        def lookup(cursor, now, subscription, matchOnly=False):
            if request.purpose == 'renewal':
                bound = self._checkoutRequestReplayLocked(cursor, request, payload_hash)
                if bound:
                    self._validateRenewalAttemptLocked(cursor, request, subscription, bound, now)
                    return self._intentFromAttemptRow(bound, self._json(bound.get('metadata_json')),
                        request.userId, request.purpose), []
            cursor.execute('''select * from public.billing_events where user_id=%s
                and event_category='payment_attempt' order by id''', (request.userId,))
            rows = []
            for row in cursor.fetchall():
                metadata = self._json(row.get('metadata_json'))
                frozen = metadata.get('manualBilling', {})
                if frozen.get('purpose') != request.purpose or (
                        request.purpose != 'initial_purchase' and frozen.get('billingMode') != request.billingMode):
                    continue
                rows.append((row, metadata, frozen))
            # A historical key must replay/conflict before inspecting any
            # newer live selection, independent of UUID ordering.
            for row, metadata, frozen in rows:
                exact = (request.requestKey is not None and row.get('idempotency_key') ==
                    f'{request.purpose}:{request.userId}:{request.requestKey}')
                if exact:
                    if not hashMatches(frozen):
                        raise ValueError('IDEMPOTENCY_CONFLICT')
                    if frozen.get('closedReason') == 'ORDER_NOT_CREATED':
                        continue
                    if request.purpose == 'renewal':
                        self._validateRenewalAttemptLocked(cursor, request, subscription, row, now)
                    return self._intentFromAttemptRow(row, metadata, request.userId, request.purpose), []
            replacements = []
            for row, metadata, frozen in rows:
                expiry = _utc(frozen.get('expiresAt'))
                live = (row.get('payment_status') in ('created', 'pending_provider_ack', 'authorized')
                        and (expiry is not None and expiry > now or
                             row.get('payment_status') == 'pending_provider_ack' and not row.get('provider_order_id')
                             or request.purpose in ('initial_purchase', 'renewal') and self._checkoutMoneyPending(row, frozen, now)))
                if (request.purpose == 'renewal' and str(row.get('invoice_id')) == str(request.payload.get('invoiceId'))
                        and row.get('payment_status') == 'captured' and frozen.get('payloadHash') == payload_hash):
                    self._bindCheckoutRequestLocked(cursor, request, payload_hash, row)
                    return self._intentFromAttemptRow(row, metadata, request.userId, request.purpose), []
                if request.requestKey is None and live and hashMatches(frozen):
                    if request.purpose == 'renewal':
                        self._validateRenewalAttemptLocked(cursor, request, subscription, row, now)
                    return self._intentFromAttemptRow(row, metadata, request.userId, request.purpose), []
                if matchOnly:
                    continue
                if (not live and (request.purpose == 'initial_purchase' or request.purpose == 'renewal'
                        and str(row.get('invoice_id')) == str(request.payload.get('invoiceId')))
                        and row.get('payment_status') in ('created', 'pending_provider_ack', 'authorized')
                        and not self._mayExpireCheckoutAttempt(row, now)):
                    # Recheck evidence under the owner lock before replacement.
                    raise RuntimeError('PROVIDER_STATUS_UNKNOWN')
                if request.purpose == 'initial_purchase' and live:
                    if (hashMatches(frozen) or (frozen.get('currency') or 'INR') != request.currency
                            or self._checkoutMoneyPending(row, frozen, now) or (
                            row.get('payment_status') == 'pending_provider_ack' and not row.get('provider_order_id'))):
                        # Unsettled money and unknown creation must be recovered
                        # before opening another payment opportunity.
                        raise ValueError('LIVE_INITIAL_CHECKOUT_CONFLICT')
                    replacements.append(row)
                if (request.purpose == 'renewal' and live
                        and str(row.get('invoice_id')) == str(request.payload.get('invoiceId'))):
                    if frozen.get('payloadHash') != payload_hash:
                        raise ValueError('LIVE_RENEWAL_CHECKOUT_CONFLICT')
                    self._validateRenewalAttemptLocked(cursor, request, subscription, row, now)
                    self._bindCheckoutRequestLocked(cursor, request, payload_hash, row)
                    return self._intentFromAttemptRow(row, metadata, request.userId, request.purpose), []
            return None, replacements

        def transaction(connection, reference=None, lookupOnly=False):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, request.userId)
                subscription = self._canonical(cursor, request.userId)
                resolvedCurrency = request.currency if request.purpose == 'initial_purchase' else (subscription.get('default_currency') or 'INR')
                if reference is not None and reference.get('currency') != resolvedCurrency:
                    raise ValueError('BILLING_CURRENCY_CONFLICT')
                cursor.execute('select clock_timestamp() as current_time')
                now = _utc(cursor.fetchone()['current_time'])
                if request.purpose == 'initial_purchase':
                    from api.services.billing.purchaseCheckoutRepository import guardInitialPurchaseLocked
                    guardInitialPurchaseLocked(self, cursor, request, subscription, now)
                existing, replacements = lookup(cursor, now, subscription, matchOnly=replayOnly)
                if existing is not None or lookupOnly:
                    return existing
                if request.requiresExistingAttempt:
                    raise ValueError('PRICING_CONTEXT_REQUIRED')
                coverage = self._coverageSnapshotLocked(cursor, subscription, now, materialize=True)
                if self._usesPurchaseAuthority() and request.purpose != 'initial_purchase':
                    subscription = self._purchaseContextLocked(cursor, subscription, coverage, now)
                if subscription.get('erasure_pending') or coverage.denialReason == 'account_banned':
                    raise ValueError('CHECKOUT_OWNER_NOT_ELIGIBLE')
                if request.purpose == 'renewal' and (request.billingMode == 'annual_prepaid' or self._usesPurchaseAuthority()) and coverage.nextPeriod:
                    raise ValueError('RENEWAL_ALREADY_PAID')
                if self._usesPurchaseAuthority() and request.purpose == 'renewal':
                    # A fresh empty provider observation permits the existing
                    # same-invoice retry policy. Close that original attempt
                    # under the owner lock before checking account-wide money.
                    cursor.execute('''select * from public.billing_events where user_id=%s
                        and invoice_id=%s and event_category='payment_attempt'
                        and payment_status in ('created','pending_provider_ack') order by id for update''',
                        (request.userId, request.payload.get('invoiceId')))
                    for attempt in cursor.fetchall():
                        frozen = self._json(attempt.get('metadata_json')).get('manualBilling', {})
                        expiry = _utc(frozen.get('expiresAt'))
                        if (frozen.get('purpose') == 'renewal' and expiry and expiry <= now
                                and self._mayExpireCheckoutAttempt(attempt, now)):
                            self._expireCheckoutAttemptLocked(cursor, attempt, now, reconciled=True)
                    cursor.execute('SELECT "isBanned" FROM public."Users" WHERE "userId"=%s', (request.userId,))
                    if self._purchaseFactsLocked(cursor, subscription, cursor.fetchone(), now).moneyUnresolved:
                        raise ValueError('RECOVERY_REQUIRED')
                if request.purpose == 'initial_purchase':
                    if self._usesPurchaseAuthority():
                        from api.services.subscriptions.subscriptionModel import evaluatePurchaseEligibility
                        cursor.execute('SELECT "isBanned" FROM public."Users" WHERE "userId"=%s', (request.userId,))
                        allowed, block = evaluatePurchaseEligibility(self._purchaseFactsLocked(cursor, subscription, cursor.fetchone(), now), now)
                        if not allowed:
                            raise ValueError({'recovery_required': 'RECOVERY_REQUIRED', 'current_coverage': 'EXISTING_PAID_COVERAGE',
                                'account_restricted': 'CHECKOUT_OWNER_NOT_ELIGIBLE'}[block])
                    if coverage.currentPeriod or coverage.nextPeriod or (
                            not self._usesPurchaseAuthority() and subscription.get('billing_mode') == 'annual_prepaid'
                            and _utc(subscription.get('current_period_end')) and _utc(subscription['current_period_end']) > now):
                        raise ValueError('EXISTING_PAID_COVERAGE')
                elif request.billingMode == 'monthly_prepaid':
                    if not coverage.accessAllowed:
                        raise ValueError('PAID_COVERAGE_REQUIRED')
                elif request.purpose in ('expert_addition', 'topup'):
                    from api.services.subscriptions.paymentValidationService import isAccessActive
                    if not (coverage.accessAllowed if self._usesPurchaseAuthority() else isAccessActive(subscription, now=now)):
                        raise ValueError('PAID_COVERAGE_REQUIRED')
                if request.purpose != 'initial_purchase' and subscription.get('billing_mode') != request.billingMode:
                    raise ValueError('CHECKOUT_MODE_CHANGED')
                invoice = None
                selected = list(domains)
                end = _utc(subscription.get('current_period_end'))
                start = now
                lifecycle = str(uuid.uuid4()) if request.purpose == 'initial_purchase' else coverage.lifecycleId
                revision = 1
                if request.purpose == 'renewal':
                    if subscription.get('renewal_opt_out') or not end or (request.billingMode == 'monthly_prepaid' and end <= now):
                        raise ValueError('RENEWAL_NOT_ELIGIBLE')
                    invoiceId = request.payload.get('invoiceId')
                    cursor.execute('select * from public."Invoices" where id=%s and "userId"=%s for update', (invoiceId, request.userId))
                    invoice = cursor.fetchone()
                    if not invoice or str(invoice['subscription_id']) != str(subscription['id']):
                        raise ValueError('OWNED_INVOICE_NOT_FOUND')
                    frozen = self._json(invoice.get('metadata_json')).get('manualBilling', {})
                    if self._usesPurchaseAuthority() and any(frozen.get(k) != subscription['_verifiedCoverage'].get(k)
                            for k in ('selectionEventId', 'selectionVersion')):
                        raise ValueError('STALE_EXPERT_SELECTION')
                    if invoice.get('billing_reason') != 'renewal' or str(invoice['status']).upper() not in ('UPCOMING', 'PAYMENT_PENDING') or _utc(invoice.get('period_start')) != end:
                        raise ValueError('RENEWAL_INVOICE_CLOSED')
                    current = subscription.get('subscribed_experts') or []
                    current = json.loads(current) if isinstance(current, str) else current
                    removed = subscription.get('pending_removals') or []
                    removed = json.loads(removed) if isinstance(removed, str) else removed
                    selected = frozen.get('domains') or self._json(invoice.get('metadata_json')).get('renewalDomains') or [value for value in current if value not in removed]
                    if (not selected or set(selected) != set(current) - set(removed)
                            or not set(selected) <= {'banking', 'manufacturing', 'supplychain', 'telecom'}):
                        raise ValueError('STALE_EXPERT_SELECTION')
                    revision = int(frozen.get('revision') or 1)
                    if request.payload.get('revision') is not None and int(request.payload['revision']) != revision:
                        raise ValueError('INVOICE_REVISION_CONFLICT')
                    start = end
                elif request.purpose == 'expert_addition':
                    current = subscription.get('subscribed_experts') or []
                    current = json.loads(current) if isinstance(current, str) else current
                    if set(selected) & set(current) or len(set(selected) | set(current)) > 4:
                        raise ValueError('EXPERT_SELECTION_CONFLICT')
                ttl = int(os.environ.get('MANUAL_CHECKOUT_TTL_SECONDS', '1800'))
                if ttl <= 0:
                    raise ValueError('INVALID_CHECKOUT_TTL')
                deadline = now + timedelta(seconds=ttl)
                if request.billingMode == 'annual_prepaid':
                    if request.purpose == 'expert_addition':
                        deadline = min(deadline, end)
                if request.billingMode == 'monthly_prepaid' and request.purpose in ('renewal', 'expert_addition'):
                    deadline = min(deadline, end)
                if invoice is None:
                    from api.services.billing.pricingCatalogRepository import getPricingCatalogRepository
                    from api.services.billing.priceCatalog import getPlanPrice
                    from api.services.credits.creditConfig import getTopupPack
                    catalog = getPricingCatalogRepository().loadCurrent(connection=connection, forShare=True)
                    if type(request.expectedCatalogRevision) is not int or request.expectedCatalogRevision != catalog.revision:
                        raise ValueError('PRICING_CHANGED')
                    if request.purpose == 'topup':
                        reference = getTopupPack(request.payload.get('packId'), resolvedCurrency, catalog=catalog)
                        if reference is None: raise ValueError('INVALID_TOPUP_PACK')
                        pricing = billingEngine.computeTopupSnapshot(request.payload.get('packId'), request.billingMode,
                                                                   currency=resolvedCurrency, priceReference=reference)
                    else:
                        reference = getPlanPrice(resolvedCurrency, request.billingMode, catalog=catalog)
                        reason = 'proration' if request.purpose == 'expert_addition' else request.purpose
                        pricing = billingEngine.computeInvoiceSnapshot(request.billingMode, reason, len(selected),
                            periodStart=start, priceReference=reference, evaluatedAt=now, currency=resolvedCurrency,
                            prorationAnchorStart=_utc(subscription.get('current_period_start')), prorationAnchorEnd=end)
                    billing = {'schemaVersion': 1, 'lifecycleId': lifecycle, 'purpose': request.purpose,
                        'billingMode': request.billingMode, 'domains': selected, 'revision': revision,
                        'catalogRevision': catalog.revision,
                        'coverageState': 'estimated', 'expiresAt': deadline.isoformat()}
                    billing.update(capturePolicy='automatic', captureWindowMinutes=captureWindowMinutes())
                    if request.purchaseDraftRevision is not None:
                        billing.update(purchaseDraftRevision=request.purchaseDraftRevision)
                    if self._usesPurchaseAuthority() and request.purpose == 'expert_addition':
                        billing['servicePeriodId'] = coverage.currentPeriod.invoiceId
                    if request.purpose == 'topup':
                        billing.update(tokens=pricing.pricing_reference_snapshot_json['tokens'], packId=request.payload['packId'])
                    invoice = {'id': str(uuid.uuid4()), 'userId': request.userId, 'subscription_id': subscription['id'],
                        'billing_reason': 'add_on' if request.purpose == 'topup' else pricing.billing_reason,
                        'payment_flow': 'razorpay_order_checkout', 'requires_customer_auth': True,
                        'status': 'PAYMENT_PENDING', 'amount': pricing.total_amount, 'total_amount': pricing.total_amount,
                        'currency': pricing.currency, 'period_start': pricing.period_start, 'period_end': pricing.period_end,
                        'amount_before_tax': pricing.amount_before_tax, 'tax_amount': pricing.tax.tax_amount,
                        'tax_breakdown_json': pricing.tax.to_dict(), 'tax_rule_version': pricing.tax.tax_rule_version,
                        'place_of_supply_snapshot': pricing.tax.place_of_supply_snapshot, 'pricing_version': pricing.pricing_version,
                        'pricing_reference_snapshot_json': pricing.pricing_reference_snapshot_json,
                        'metadata_json': {'manualBilling': billing, 'domains': selected, 'billingMode': request.billingMode}}
                    if request.purpose == 'topup':
                        invoice['metadata_json'].update(tokens=billing['tokens'], packId=billing['packId'])
                    if replacements:
                        replacedIds = sorted({str(row['invoice_id']) for row in replacements})
                        for invoiceId in replacedIds:
                            cursor.execute('select * from public."Invoices" where id=%s and "userId"=%s for update',
                                (invoiceId, request.userId))
                            oldInvoice = cursor.fetchone()
                            if not oldInvoice or str(oldInvoice['status']).upper() not in ('UPCOMING','PAYMENT_PENDING'):
                                raise ValueError('CHECKOUT_CLOSED')
                            self._closeInvoice(cursor, oldInvoice, 'SUPERSEDED_BY_REQUEST',
                                now=now, replacementInvoiceId=invoice['id'])
                        invoice['metadata_json']['manualBilling']['replacesInvoiceIds'] = replacedIds
                    cursor.execute('insert into public."Invoices" (' + ','.join('"' + key + '"' for key in invoice) +
                        ') values (' + ','.join('%s' for _ in invoice) + ')',
                        [Json(value) if isinstance(value, (dict, list)) else value for value in invoice.values()])
                frozen = self._json(invoice.get('metadata_json')).get('manualBilling', {})
                snapshot = {'subscriptionId': str(subscription['id']), 'invoiceId': str(invoice['id']),
                    'lifecycleId': lifecycle, 'cycleId': str(invoice.get('period_start')), 'revision': revision,
                    'billingMode': request.billingMode, 'domains': selected, 'amount': int(invoice['total_amount']),
                    'currency': invoice['currency'], 'periodStart': _isoText(invoice.get('period_start')),
                    'periodEnd': _isoText(invoice.get('period_end')), 'expiresAt': deadline.isoformat(),
                    'tokens': frozen.get('tokens'), 'packId': frozen.get('packId'), 'requestKey': request.requestKey or str(uuid.uuid4())}
                snapshot['catalogRevision'] = self._json(invoice.get('pricing_reference_snapshot_json')).get('catalogRevision')
                if self._usesPurchaseAuthority():
                    snapshot.update(servicePeriodId=frozen.get('servicePeriodId'),
                        selectionEventId=frozen.get('selectionEventId'), selectionVersion=frozen.get('selectionVersion'))
                snapshot.update(capturePolicy='automatic', captureWindowMinutes=captureWindowMinutes())
                if request.purchaseDraftRevision is not None:
                    snapshot.update(purchaseDraftRevision=request.purchaseDraftRevision)
                intent = self._reserveCheckoutIntentLocked(cursor, subscription, request.userId, request.purpose,
                    snapshot['requestKey'], payload_hash, snapshot, now)
                if request.purchaseDraftRevision is not None:
                    from api.services.billing.purchaseCheckoutRepository import bindPurchaseControlLocked
                    bindPurchaseControlLocked(cursor, request, intent)
                return intent
        existing = self._run(lambda connection: transaction(connection, lookupOnly=True))
        if existing is not None or replayOnly:
            return existing
        if request.requiresExistingAttempt:
            raise ValueError('PRICING_CONTEXT_REQUIRED')
        return self._run(lambda connection: transaction(connection))

    def subscriptionCurrency(self, userId):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                currency = self._canonical(cursor, userId).get('default_currency') or 'INR'
                if currency not in ('INR', 'USD'):
                    raise ValueError('BILLING_CURRENCY_UNSUPPORTED')
                return currency
        return self._run(operation)

    def paidSubscriptionCurrency(self, userId):
        """Read paid currency in one query, without trusting unpaid defaults."""
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('''SELECT DISTINCT coalesce(i.currency,'INR') AS currency
                    FROM public."Invoices" i JOIN public.subscriptions s ON s.id=i.subscription_id
                    WHERE s.user_id=%s AND s.is_canonical=true AND i."userId"=%s AND i.status='PAID' ''',
                    (userId,userId))
                currencies = {row['currency'] for row in cursor.fetchall()}
                if len(currencies) > 1: raise ValueError('BILLING_CURRENCY_CONFLICT')
                return next(iter(currencies), None)
        return self._run(operation)

    def getPaidPeriodPrice(self, userId):
        """Actual before-tax payments backing current coverage; never catalog prices."""
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                subscription = self._canonical(cursor, userId)
                now = _now()
                coverage = self._coverageSnapshotLocked(cursor, subscription, now, materialize=False)
                period = coverage.currentPeriod
                if period is None: return None
                cursor.execute('SELECT * FROM public."Invoices" WHERE "userId"=%s AND subscription_id=%s AND status=\'PAID\' ORDER BY id',
                               (userId, subscription['id']))
                rows = []
                for invoice in cursor.fetchall():
                    billing = self._json(invoice.get('metadata_json')).get('manualBilling', {})
                    if billing.get('revokedAt') or billing.get('coverageState') == 'revoked': continue
                    base = str(invoice['id']) == period.invoiceId
                    addition = (billing.get('purpose') == 'expert_addition' and billing.get('coverageState') == 'active'
                        and billing.get('lifecycleId') == period.lifecycleId
                        and _utc(invoice.get('period_end')) == period.end
                        and period.start <= (_utc(invoice.get('period_start')) or now) <= now)
                    if base or addition: rows.append(invoice)
                if not rows or period.invoiceId not in {str(row['id']) for row in rows}: return None
                amounts = [row.get('amount_before_tax') for row in rows]
                currencies = {row.get('currency') for row in rows}
                if len(currencies) != 1 or not currencies <= {'INR', 'USD'}: return None
                if any(type(value) is not int or value < 0 for value in amounts): return None
                amount = sum(amounts)
                if not 0 < amount <= 9007199254740991: return None
                return {'currency':currencies.pop(), 'amountBeforeTax':amount, 'billingMode':coverage.billingMode,
                    'periodStart':period.start.isoformat(), 'periodEnd':period.end.isoformat(),
                    'invoiceIds':sorted(str(row['id']) for row in rows)}
        return self._run(operation)

    def initialReplayCurrency(self, userId, requestKey, domains, mode):
        if not requestKey:
            return None
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('''select * from public.billing_events where user_id=%s
                    and event_category='payment_attempt' and idempotency_key=%s''',
                    (userId, f'initial_purchase:{userId}:{requestKey}'))
                for row in cursor.fetchall():
                    frozen = self._json(row.get('metadata_json')).get('manualBilling', {})
                    if (frozen.get('purpose') == 'initial_purchase' and frozen.get('billingMode') == mode
                            and frozen.get('closedReason') != 'ORDER_NOT_CREATED'
                            and set(frozen.get('domains') or []) == set(domains)
                            and row.get('currency') in ('INR', 'USD')):
                        return row['currency']
                return None
        return self._run(operation)

    def _annualRenewalPricing(self, connection, subscription, start, end):
        """Caller owns the user/invoice locks; hold the catalog lock until commit."""
        from api.services.billing.pricingCatalogRepository import getPricingCatalogRepository
        from api.services.billing.priceCatalog import getPlanPrice
        from api.services.billing.billingEngine import computeInvoiceSnapshot
        from api.services.subscriptions.subscriptionFieldUtils import buildRenewalPricingMetadata, subscriptionBillingState
        subscription = {**subscription, 'billing_state': self._json(subscription.get('billing_state')),
            'subscribed_experts': self._jsonList(subscription.get('subscribed_experts')),
            'pending_removals': self._jsonList(subscription.get('pending_removals'))}
        metadata = buildRenewalPricingMetadata(subscription, start)
        if not metadata['renewalDomains']:
            raise ValueError('RENEWAL_NOT_ELIGIBLE')
        catalog = getPricingCatalogRepository().loadCurrent(connection=connection, forShare=True)
        currency = subscription.get('default_currency') or 'INR'
        pricing = computeInvoiceSnapshot('annual_prepaid', 'renewal', len(metadata['renewalDomains']),
            currency=currency, customerState=subscriptionBillingState(subscription).get('customerState'),
            periodStart=start, periodEnd=end,
            priceReference=getPlanPrice(currency, 'annual_prepaid', catalog=catalog))
        fields = {'amount': pricing.total_amount, 'total_amount': pricing.total_amount,
            'amount_before_tax': pricing.amount_before_tax, 'tax_amount': pricing.tax.tax_amount,
            'currency': pricing.currency, 'tax_breakdown_json': pricing.tax.to_dict(),
            'tax_rule_version': pricing.tax.tax_rule_version,
            'place_of_supply_snapshot': pricing.tax.place_of_supply_snapshot,
            'pricing_version': pricing.pricing_version,
            'pricing_reference_snapshot_json': pricing.pricing_reference_snapshot_json}
        return fields, metadata

    def createAnnualRenewalInvoice(self, userId, subscriptionId=None):
        """Issue once at the current catalog price, serialized with publication."""
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                subscription = self._canonical(cursor, userId)
                if self._usesPurchaseAuthority():
                    coverage = self._coverageSnapshotLocked(cursor, subscription, _now(), materialize=False)
                    subscription = self._purchaseContextLocked(cursor, subscription, coverage, _now())
                start = _utc(subscription.get('current_period_end'))
                if (subscription.get('billing_mode') != 'annual_prepaid' or not start
                        or subscription.get('erasure_pending') or subscription.get('renewal_opt_out')
                        or (subscriptionId is not None and str(subscription['id']) != str(subscriptionId))):
                    raise ValueError('RENEWAL_NOT_ELIGIBLE')
                from dateutil.relativedelta import relativedelta
                end = start + relativedelta(years=1)
                cursor.execute('''select * from public."Invoices" where "userId"=%s and subscription_id=%s
                    and billing_reason='renewal' and period_start=%s and period_end=%s
                    and status in ('UPCOMING','PAYMENT_PENDING','PAID','EXPIRED','upcoming','payment_pending','paid','expired')
                    order by id for update''', (userId, subscription['id'], start, end))
                for invoice in cursor.fetchall():
                    if self._json(invoice.get('metadata_json')).get('manualBilling', {}).get('coverageState') != 'revoked':
                        return invoice
                fields, metadata = self._annualRenewalPricing(connection, subscription, start, end)
                lifecycle = self._json(subscription.get('billing_state')).get('manualBilling', {}).get('lifecycleId')
                metadata.update(flow='annual_renewal_scheduler_t30', billingMode='annual_prepaid', estimate=False, frozen=True,
                    manualBilling={'schemaVersion': 1, 'lifecycleId': lifecycle, 'purpose': 'renewal',
                        'billingMode': 'annual_prepaid', 'domains': metadata['renewalDomains'],
                        'coverageState': 'scheduled', 'revision': 1})
                if self._usesPurchaseAuthority():
                    metadata['manualBilling'].update({k: subscription['_verifiedCoverage'][k]
                        for k in ('selectionEventId', 'selectionVersion')})
                row = {'id': str(uuid.uuid4()), 'userId': userId, 'subscription_id': subscription['id'],
                    'billing_reason': 'renewal', 'payment_flow': 'razorpay_order_checkout',
                    'requires_customer_auth': True, 'period_start': start, 'period_end': end, 'due_date': start,
                    'status': 'UPCOMING', **fields, 'metadata_json': metadata}
                columns = list(row)
                values = [Json(value) if isinstance(value, (dict, list)) else value for value in row.values()]
                cursor.execute('insert into public."Invoices" (' + ','.join('"' + key + '"' for key in columns)
                    + ') values (' + ','.join('%s' for _ in columns) + ') returning *', values)
                return cursor.fetchone()
        return self._run(operation)

    def prepareAnnualRenewalInvoice(self, invoiceId):
        """Make an issued invoice payable without recalculating its price.

        The existing explicit selection-change recovery is the only repricing
        transition: its previous attempts are closed, its revision increments,
        and the replacement snapshot freezes under the catalog lock.
        """
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('select "userId" from public."Invoices" where id=%s', (invoiceId,))
                owner = cursor.fetchone()
                if not owner or not owner['userId']:
                    return None
                self._lockUser(cursor, owner['userId'])
                subscription = self._canonical(cursor, owner['userId'])
                if self._usesPurchaseAuthority():
                    coverage = self._coverageSnapshotLocked(cursor, subscription, _now(), materialize=False)
                    subscription = self._purchaseContextLocked(cursor, subscription, coverage, _now())
                cursor.execute('select * from public."Invoices" where id=%s for update', (invoiceId,))
                invoice = cursor.fetchone()
                if (not invoice or invoice['billing_reason'] != 'renewal'
                        or str(invoice['subscription_id']) != str(subscription['id'])
                        or subscription.get('billing_mode') != 'annual_prepaid'
                        or subscription.get('erasure_pending')
                        or str(invoice['status']).upper() not in ('UPCOMING', 'PAYMENT_PENDING', 'EXPIRED')):
                    return None
                metadata = self._json(invoice.get('metadata_json'))
                fields = {}
                if metadata.get('repricingRequired'):
                    if str(invoice['status']).upper() != 'EXPIRED':
                        raise ValueError('INVOICE_REVISION_CONFLICT')
                    cursor.execute('''select 1 from public.billing_events where invoice_id=%s
                        and event_category='payment_attempt'
                        and payment_status in ('created','pending_provider_ack','authorized','captured') limit 1''', (invoiceId,))
                    if cursor.fetchone():
                        raise ValueError('INVOICE_REVISION_CONFLICT')
                    fields, selection = self._annualRenewalPricing(connection, subscription,
                        _utc(invoice['period_start']), _utc(invoice['period_end']))
                    manual = dict(metadata.get('manualBilling') or {})
                    for key in ('closedAt', 'closedReason'):
                        manual.pop(key, None)
                    manual.update(domains=selection['renewalDomains'], revision=int(manual.get('revision') or 1) + 1)
                    if self._usesPurchaseAuthority():
                        manual.update({k: subscription['_verifiedCoverage'][k] for k in ('selectionEventId', 'selectionVersion')})
                    metadata.update(selection, manualBilling=manual, repricingRequired=False)
                    fields['razorpay_order_id'] = None
                elif str(invoice['status']).upper() == 'EXPIRED':
                    return None
                from api.services.billing.invoiceService import buildDashboardRenewalUrl
                metadata.update(flow='annual_renewal_scheduler_t7', billingMode='annual_prepaid', estimate=False,
                    frozen=True, paymentFlow='razorpay_order_checkout', dashboardRenewalUrl=buildDashboardRenewalUrl(str(invoiceId)))
                fields.update(status='PAYMENT_PENDING', payment_flow='razorpay_order_checkout',
                    requires_customer_auth=True, metadata_json=metadata)
                values = [Json(value) if isinstance(value, (dict, list)) else value for value in fields.values()]
                cursor.execute('update public."Invoices" set ' + ','.join('"' + key + '"=%s' for key in fields)
                    + ' where id=%s returning *', (*values, invoiceId))
                result = cursor.fetchone()
                result['metadata_json'] = metadata
                return result
        return self._run(operation)

    def createFrozenRenewalInvoice(self, payload, expectedVersion, expectedCatalogRevision=None):
        """Freeze current catalog prices inside the renewal issuance transaction."""
        from copy import deepcopy
        payload = deepcopy(payload)
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor,payload['userId'])
                subscription=self._canonical(cursor,payload['userId'])
                if self._usesPurchaseAuthority():
                    coverage = self._coverageSnapshotLocked(cursor, subscription, _now(), materialize=False)
                    subscription = self._purchaseContextLocked(cursor, subscription, coverage, _now())
                if int(subscription['version']) != int(expectedVersion):
                    raise ValueError('STALE_SUBSCRIPTION_VERSION')
                if subscription.get('renewal_opt_out') or subscription.get('erasure_pending') or not _utc(subscription.get('current_period_end')) or _utc(subscription['current_period_end']) <= _now():
                    raise ValueError('RENEWAL_NOT_ELIGIBLE')
                if str(subscription['id']) != str(payload['subscription_id']) or _utc(payload['period_start']) != _utc(subscription['current_period_end']):
                    raise ValueError('RENEWAL_CYCLE_MISMATCH')
                current=subscription.get('subscribed_experts') or []
                current=json.loads(current) if isinstance(current,str) else list(current)
                removals=subscription.get('pending_removals') or []
                removals=json.loads(removals) if isinstance(removals,str) else list(removals)
                domains=[domain for domain in current if domain not in removals]
                frozen=payload['metadata_json']['manualBilling']
                if self._usesPurchaseAuthority():
                    # Freeze the exact selection read under the owner lock, even
                    # when the decision event existed before this invoice.
                    frozen = payload['metadata_json']['manualBilling']
                    frozen.update({k: subscription['_verifiedCoverage'][k]
                        for k in ('selectionEventId', 'selectionVersion')})
                from dateutil.relativedelta import relativedelta
                lifecycle=self._json(subscription.get('billing_state')).get('manualBilling',{}).get('lifecycleId')
                if frozen.get('lifecycleId') != lifecycle or _utc(payload['period_end']) != _utc(payload['period_start'])+relativedelta(months=1):
                    raise ValueError('INVALID_FROZEN_CALENDAR_PERIOD')
                if not domains or set(frozen['domains']) != set(domains):
                    raise ValueError('STALE_EXPERT_SELECTION')
                cursor.execute('''select * from public."Invoices" where "userId"=%s and subscription_id=%s
                    and billing_reason='renewal' and period_start=%s and status in ('UPCOMING','PAYMENT_PENDING','PAID') order by id for update''',
                    (payload['userId'],subscription['id'],payload['period_start']))
                for invoice in cursor.fetchall():
                    billing=self._json(invoice.get('metadata_json')).get('manualBilling',{})
                    if billing.get('coverageState') != 'revoked': return invoice
                from api.services.billing.pricingCatalogRepository import getPricingCatalogRepository
                from api.services.billing.priceCatalog import getPlanPrice
                from api.services.billing.billingEngine import computeInvoiceSnapshot
                catalog = getPricingCatalogRepository().loadCurrent(connection=connection, forShare=True)
                if expectedCatalogRevision is not None and expectedCatalogRevision != catalog.revision:
                    raise ValueError('PRICING_CHANGED')
                currency = subscription.get('default_currency') or 'INR'
                pricing = computeInvoiceSnapshot('monthly_prepaid', 'renewal', len(domains),
                    periodStart=_utc(payload['period_start']), periodEnd=_utc(payload['period_end']), currency=currency,
                    priceReference=getPlanPrice(currency, 'monthly_prepaid', catalog=catalog))
                renewalPayload = {**payload, 'amount':pricing.total_amount, 'total_amount':pricing.total_amount,
                    'amount_before_tax':pricing.amount_before_tax, 'tax_amount':pricing.tax.tax_amount,
                    'currency':pricing.currency, 'tax_breakdown_json':pricing.tax.to_dict(),
                    'tax_rule_version':pricing.tax.tax_rule_version, 'place_of_supply_snapshot':pricing.tax.place_of_supply_snapshot,
                    'pricing_version':pricing.pricing_version, 'pricing_reference_snapshot_json':pricing.pricing_reference_snapshot_json}
                allowed={'userId','subscription_id','billing_reason','payment_flow','requires_customer_auth',
                    'period_start','period_end','amount_before_tax','tax_amount','total_amount','amount','currency','status',
                    'tax_breakdown_json','tax_rule_version','place_of_supply_snapshot','pricing_version',
                    'pricing_reference_snapshot_json','metadata_json'}
                if set(payload)-allowed: raise ValueError('UNSUPPORTED_INVOICE_FIELDS')
                row={'id':str(uuid.uuid4()),**renewalPayload}
                columns=list(row)
                values=[Json(value) if isinstance(value,(dict,list)) else value for value in row.values()]
                cursor.execute('insert into public."Invoices" ('+','.join('"'+column+'"' for column in columns)+') values ('+','.join('%s' for _ in columns)+') returning *',values)
                return cursor.fetchone()
        return self._run(operation)

    def scheduleExpertRemoval(self,userId,domains):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor,userId)
                subscription=self._canonical(cursor,userId)
                if self._usesPurchaseAuthority():
                    coverage = self._coverageSnapshotLocked(cursor, subscription, _now(), materialize=False)
                    context = self._purchaseContextLocked(cursor, subscription, coverage, _now())
                    current, selected = list(coverage.currentPeriod.domains), context['_verifiedCoverage']['nextExperts']
                    if not domains or len(domains) != len(set(domains)) or not set(domains) <= set(selected):
                        raise ValueError('INVALID_EXPERT_REMOVAL')
                    remaining = [e for e in selected if e not in domains]
                    if not remaining:
                        raise ValueError('USE_CANCEL_FOR_ALL_EXPERTS')
                    self._modelRepository().saveNextSelection(cursor, str(subscription['id']), coverage.lifecycleId,
                        coverage.currentPeriod.end, subscription['version'], remaining, now=_now())
                    pending = [e for e in current if e not in remaining]
                    cursor.execute('UPDATE public.subscriptions SET pending_removals=%s WHERE id=%s', (Json(pending), subscription['id']))
                    return {'currentDomains': current, 'pendingRemovals': pending, 'effectiveAt': coverage.currentPeriod.end.isoformat()}
                end=_utc(subscription.get('current_period_end'))
                if not end or end <= _now() or subscription.get('erasure_pending'):
                    raise ValueError('PAID_COVERAGE_REQUIRED')
                current=subscription.get('subscribed_experts') or []
                current=json.loads(current) if isinstance(current,str) else list(current)
                pending=subscription.get('pending_removals') or []
                pending=json.loads(pending) if isinstance(pending,str) else list(pending)
                if not set(domains)<=set(current) or set(domains)&set(pending): raise ValueError('INVALID_EXPERT_REMOVAL')
                if len(set(current)-set(pending)-set(domains))<1: raise ValueError('USE_CANCEL_FOR_ALL_EXPERTS')
                cursor.execute('''select id,status,metadata_json from public."Invoices" where "userId"=%s
                    and subscription_id=%s and billing_reason='renewal' and period_start=%s for update''',(userId,subscription['id'],end))
                invoices=cursor.fetchall()
                if any(row['status']=='PAID' and self._json(row.get('metadata_json')).get('manualBilling',{}).get('coverageState')!='revoked' for row in invoices):
                    raise ValueError('PAID_FUTURE_SELECTION_IMMUTABLE')
                for invoice in invoices:
                    if str(invoice['status']).upper() not in ('UPCOMING','PAYMENT_PENDING'): continue
                    self._closeInvoice(cursor,invoice,'EXPERT_SELECTION_CHANGED')
                pending.extend(domains)
                cursor.execute('update public.subscriptions set pending_removals=%s,version=version+1 where id=%s',(Json(pending),subscription['id']))
                return {'currentDomains':current,'pendingRemovals':pending,'effectiveAt':end.isoformat()}
        return self._run(operation)

    def _closeInvoice(self,cursor,invoice,reason,*,now=None,replacementInvoiceId=None,preserveAuthorization=False):
        closure = {'closedAt':(now or _now()).isoformat(), 'closedReason':reason}
        if replacementInvoiceId is not None:
            closure['supersededByInvoiceId'] = str(replacementInvoiceId)
        metadata=self._json(invoice.get('metadata_json'))
        metadata.setdefault('manualBilling',{}).update(closure)
        cursor.execute('update public."Invoices" set status=\'VOID\',metadata_json=%s where id=%s',(Json(metadata),invoice['id']))
        cursor.execute("select id,metadata_json,payment_status from public.billing_events where invoice_id=%s and event_category='payment_attempt' and payment_status in ('created','pending_provider_ack','authorized') order by id for update", (invoice['id'],))
        for row in cursor.fetchall():
            old = self._json(row.get('metadata_json'))
            old.setdefault('manualBilling', {}).update(closure)
            status = 'authorized' if preserveAuthorization and row['payment_status'] == 'authorized' else 'cancelled'
            cursor.execute("update public.billing_events set payment_status=%s,event_status=%s,metadata_json=%s where id=%s", (status,status,Json(old), row['id']))

    def cancelExpertAddition(self,userId,domain):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor,userId)
                subscription=self._canonical(cursor,userId)
                if self._usesPurchaseAuthority():
                    coverage = self._coverageSnapshotLocked(cursor, subscription, _now(), materialize=False)
                    if not coverage.currentPeriod:
                        raise ValueError('NO_CANCELLABLE_EXPERT_ATTEMPT')
                    candidates = [a for a in self._pendingExpertAttemptsLocked(cursor, subscription, coverage.currentPeriod.invoiceId)
                        if domain in self._json(a['metadata_json'])['manualBilling'].get('domains', [])]
                    if len(candidates) != 1:
                        raise ValueError('NO_CANCELLABLE_EXPERT_ATTEMPT')
                    attempt = candidates[0]
                    cursor.execute('SELECT * FROM public."Invoices" WHERE id=%s AND "userId"=%s AND subscription_id=%s FOR UPDATE',
                        (attempt['invoice_id'], userId, subscription['id']))
                    invoice = cursor.fetchone()
                    if not invoice or invoice['status'] == 'PAID':
                        raise ValueError('EXPERT_ALREADY_PAID_OR_MISSING')
                    self._closeInvoice(cursor, invoice, 'EXPERT_ADDITION_CANCELLED')
                    return {'domain': domain, 'cancelled': True,
                        'closedAttemptDomains': self._json(attempt['metadata_json'])['manualBilling']['domains'],
                        'orderId': attempt['provider_order_id']}
                pending=subscription.get('pending_additions') or []
                pending=json.loads(pending) if isinstance(pending,str) else list(pending)
                target=next((item for item in pending if item['domain']==domain and item.get('state')=='awaiting_payment'),None)
                if target is None: raise ValueError('NO_CANCELLABLE_EXPERT_ATTEMPT')
                attemptId=target.get('attemptId')
                if not attemptId: raise ValueError('EXPERT_ATTEMPT_IDENTITY_MISSING')
                cursor.execute('select invoice_id,payment_status from public.billing_events where id=%s',(attemptId,))
                attempt=cursor.fetchone()
                cursor.execute('select * from public."Invoices" where id=%s for update',(attempt['invoice_id'],))
                invoice=cursor.fetchone()
                if invoice['status']=='PAID': raise ValueError('EXPERT_ALREADY_PAID')
                self._closeInvoice(cursor,invoice,'EXPERT_ADDITION_CANCELLED')
                closed=[]
                for item in pending:
                    if item.get('attemptId')==attemptId:
                        item.update(state='cancelled',cancelledAt=_now().isoformat())
                        closed.append(item['domain'])
                cursor.execute('update public.subscriptions set pending_additions=%s,version=version+1 where id=%s',(Json(pending),subscription['id']))
                return {'domain':domain,'cancelled':True,'closedAttemptDomains':closed,'orderId':target.get('orderId')}
        return self._run(operation)

    def reserveCheckoutIntent(
        self,
        userId: str,
        purpose: str,
        requestKey: str,
        payloadHash: str,
        snapshot: dict,
    ) -> CheckoutIntent:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                subscription = self._canonical(cursor, userId)
                cursor.execute('select clock_timestamp() as current_time')
                return self._reserveCheckoutIntentLocked(cursor, subscription, userId, purpose,
                    requestKey, payloadHash, snapshot, _utc(cursor.fetchone()['current_time']))
        return self._run(operation)

    def _expireCheckoutAttemptLocked(self, cursor, attempt, now, *, reconciled=False):
        metadata=self._json(attempt.get('metadata_json'))
        metadata.setdefault('manualBilling',{}).update(closedAt=now.isoformat(),closedReason='CHECKOUT_EXPIRED')
        if reconciled:
            metadata['manualBilling']['closureReconciledAt'] = now.isoformat()
        cursor.execute("update public.billing_events set payment_status='expired',event_status='expired',metadata_json=%s where id=%s",
            (Json(metadata),attempt['id']))

    def _checkoutMoneyPending(self, attempt, billing, now):
        observation = billing.get('providerObservation', {})
        if attempt.get('payment_status') == 'authorized' or observation.get('outcome') in ('processing', 'authorized', 'unknown', 'captured'):
            return True
        expiry = _utc(billing.get('expiresAt'))
        return bool(expiry and expiry <= now and observation.get('hasPayments'))

    def _mayExpireCheckoutAttempt(self, attempt, now):
        from api.services.billing.checkoutRecoveryPolicy import mayExpireCheckout
        billing = self._json(attempt.get('metadata_json')).get('manualBilling', {})
        if attempt.get('payment_status') not in ('created', 'pending_provider_ack'):
            return False
        observation = billing.get('providerObservation', {})
        checked = _utc(observation.get('checkedAt'))
        if not checked or not attempt.get('provider_order_id') or observation.get('orderId') != attempt['provider_order_id']:
            return False
        snapshot = CheckoutProviderSnapshot(str(attempt['id']), attempt['provider_order_id'],
            observation.get('outcome'), checked, bool(observation.get('complete')),
            0, '', ({},) if observation.get('hasPayments') else ())
        return mayExpireCheckout(snapshot, now)

    def _expireCheckoutInvoiceLocked(self, cursor, invoiceId, now):
        """Close an abandoned single-use checkout invoice once nothing on it is payable or paid.

        Renewal invoices stay payable until their period boundary closes them.
        A later capture on the closed attempt still routes to reconciliation.
        """
        cursor.execute('select id,status,billing_reason,metadata_json from public."Invoices" where id=%s for update',(invoiceId,))
        invoice=cursor.fetchone()
        if (not invoice or invoice.get('billing_reason') == 'renewal'
                or str(invoice['status']).upper() not in ('UPCOMING','PAYMENT_PENDING')):
            return False
        cursor.execute("""select 1 from public.billing_events where invoice_id=%s and event_category='payment_attempt'
            and payment_status in ('created','pending_provider_ack','authorized','captured') limit 1""",(invoiceId,))
        if cursor.fetchone():
            return False
        metadata=self._json(invoice.get('metadata_json'))
        metadata.setdefault('manualBilling',{}).update(closedAt=now.isoformat(),closedReason='CHECKOUT_EXPIRED')
        cursor.execute('update public."Invoices" set status=%s,metadata_json=%s where id=%s',('EXPIRED',Json(metadata),invoiceId))
        return True

    def _releaseExpiredAdditionsLocked(self, cursor, subscription, attemptIds, now):
        """Expert-addition holds of lapsed attempts must not block a new selection."""
        pending=self._jsonList(subscription.get('pending_additions'))
        released=False
        for item in pending:
            if item.get('attemptId') in attemptIds and item.get('state') == 'awaiting_payment':
                item.update(state='expired',expiredAt=now.isoformat())
                released=True
        if released:
            cursor.execute('update public.subscriptions set pending_additions=%s where id=%s',(Json(pending),subscription['id']))
            subscription['pending_additions']=pending

    def expireStaleCheckouts(self, limit=200):
        """Close checkouts whose payment window lapsed so abandoned invoices stop reading as payable.

        Only attempts with a bound provider order are closed here; unknown order
        acknowledgements and authorized money stay with provider recovery.
        """
        def candidates(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""select distinct user_id from public.billing_events
                    where event_category='payment_attempt' and payment_status in ('created','pending_provider_ack')
                    and provider_order_id is not null and user_id is not null
                    and (metadata_json->'manualBilling'->>'expiresAt')::timestamptz <= clock_timestamp()
                    limit %s""",(limit,))
                return [row['user_id'] for row in cursor.fetchall()]
        summary={'attemptsExpired':0,'invoicesExpired':0}
        for userId in self._run(candidates):
            def operation(connection,userId=userId):
                with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                    self._lockUser(cursor,userId)
                    subscription=self._canonical(cursor,userId)
                    cursor.execute('select clock_timestamp() as current_time')
                    now=_utc(cursor.fetchone()['current_time'])
                    cursor.execute("""select id,invoice_id,metadata_json,payment_status,provider_order_id from public.billing_events where user_id=%s
                        and event_category='payment_attempt' and payment_status in ('created','pending_provider_ack')
                        and provider_order_id is not null order by id for update""",(userId,))
                    expired=[attempt for attempt in cursor.fetchall()
                        if (_utc(self._json(attempt.get('metadata_json')).get('manualBilling',{}).get('expiresAt')) or now) <= now
                        and self._mayExpireCheckoutAttempt(attempt, now)]
                    for attempt in expired:
                        self._expireCheckoutAttemptLocked(cursor,attempt,now)
                    self._releaseExpiredAdditionsLocked(cursor,subscription,{str(attempt['id']) for attempt in expired},now)
                    closed=sum(self._expireCheckoutInvoiceLocked(cursor,str(attempt['invoice_id']),now)
                        for attempt in expired if attempt.get('invoice_id'))
                    return len(expired),closed
            attempts,invoices=self._run(operation)
            summary['attemptsExpired']+=attempts
            summary['invoicesExpired']+=invoices
        return summary

    def _reserveCheckoutIntentLocked(self, cursor, subscription, userId, purpose,
                                    requestKey, payloadHash, snapshot, now):
        manualBilling = {
            "schemaVersion": 1,
            "lifecycleId": snapshot.get("lifecycleId"),
            "cycleId": snapshot.get("cycleId"),
            "revision": snapshot.get("revision", 1),
            "catalogRevision": snapshot.get("catalogRevision"),
            "purpose": purpose,
            "billingMode": snapshot.get("billingMode"),
            "domains": snapshot.get("domains", []),
            "tokens": snapshot.get("tokens"),
            "packId": snapshot.get("packId"),
            "requestKey": snapshot.get("requestKey", requestKey),
            "periodStart": snapshot.get("periodStart"),
            "periodEnd": snapshot.get("periodEnd"),
            "payloadHash": payloadHash,
            "frozenAmount": snapshot.get("amount"),
            "currency": snapshot.get("currency", "INR"),
            "expiresAt": snapshot.get("expiresAt"),
            "closedAt": None,
            "closedReason": None,
        }
        if self._usesPurchaseAuthority():
            manualBilling.update({k: snapshot.get(k) for k in ('servicePeriodId', 'selectionEventId', 'selectionVersion')})
        manualBilling.update(capturePolicy=snapshot.get('capturePolicy', 'automatic'),
            captureWindowMinutes=snapshot.get('captureWindowMinutes') or captureWindowMinutes())
        if snapshot.get('purchaseDraftRevision') is not None:
            manualBilling['purchaseDraftRevision'] = snapshot['purchaseDraftRevision']

        namespaceKey = f"{purpose}:{userId}:{requestKey}"
        if str(subscription['id']) != str(snapshot.get('subscriptionId')) or subscription.get('erasure_pending'):
            raise ValueError('CHECKOUT_OWNER_NOT_ELIGIBLE')
        cursor.execute('select * from public."Invoices" where id=%s for update',(snapshot.get('invoiceId'),))
        invoice = cursor.fetchone()
        if not invoice or invoice['userId'] != userId or int(invoice.get('total_amount') or invoice.get('amount') or 0) != int(snapshot.get('amount') or 0):
            raise ValueError('CHECKOUT_INVOICE_MISMATCH')
        cursor.execute(
            """
            select id, user_id, subscription_id, invoice_id,
                   payment_status, provider_order_id,
                   metadata_json
            from public.billing_events
            where idempotency_key = %s
               or (idempotency_key like %s and invoice_id=%s and event_category='payment_attempt')
              and event_category = 'payment_attempt'
            order by attempted_at desc, idempotency_key desc
            limit 1
            """,
            (namespaceKey, namespaceKey+':revision:%', snapshot.get('invoiceId')),
        )
        existing = cursor.fetchone()
        if existing is not None:
            storedMeta = existing.get("metadata_json") or {}
            if isinstance(storedMeta, str):
                try:
                    storedMeta = json.loads(storedMeta)
                except (ValueError, TypeError):
                    storedMeta = {}
            storedBilling = (
                storedMeta.get("manualBilling") or {}
                if isinstance(storedMeta, dict)
                else {}
            )
            if storedBilling.get("payloadHash") != payloadHash:
                raise ValueError(
                    "IDEMPOTENCY_CONFLICT: same request key with a "
                    "different payload"
                )
            intent=self._intentFromAttemptRow(existing,storedMeta,userId,purpose)
            if intent.expiresAt > now and intent.state in ('created','pending_provider_ack','authorized'):
                return intent
            if not intent.razorpayOrderId and intent.state in ('created', 'pending_provider_ack', 'authorized'):
                # Unknown provider creation must be recovered before a new opportunity.
                return intent
            if invoice['status'] == 'PAID': return intent
            if storedBilling.get('closedReason') != 'ORDER_NOT_CREATED':
                storedBilling.update(closedAt=now.isoformat(),closedReason='CHECKOUT_EXPIRED')
                cursor.execute("update public.billing_events set payment_status='expired',event_status='expired',metadata_json=%s where id=%s",
                    (Json(storedMeta),existing['id']))
            revision=int(storedBilling.get('revision') or 1)+1
            namespaceKey += ':revision:'+str(revision)
            manualBilling['revision']=revision

        if str(invoice['status']).upper() not in ('UPCOMING','PAYMENT_PENDING') or _utc(snapshot.get('expiresAt')) <= now:
            raise ValueError('CHECKOUT_CLOSED')
        if purpose == 'renewal' and (subscription.get('renewal_opt_out') or not _utc(subscription.get('current_period_end')) or (snapshot.get('billingMode') == 'monthly_prepaid' and _utc(subscription.get('current_period_end')) <= now)):
            raise ValueError('RENEWAL_NOT_ELIGIBLE')
        manualBilling['expiresAt']=(_utc(snapshot['expiresAt']) if snapshot.get('billingMode') == 'annual_prepaid'
            else min(_utc(snapshot['expiresAt']),now+timedelta(seconds=int(os.environ.get('MANUAL_CHECKOUT_TTL_SECONDS','1800'))))).isoformat()
        if purpose == 'initial_purchase' and not self._usesPurchaseAuthority() and subscription.get('billing_mode') in ('monthly_prepaid','annual_prepaid') and _utc(subscription.get('current_period_end')) and _utc(subscription['current_period_end']) > now:
            raise ValueError('EXISTING_PAID_COVERAGE')
        if purpose == 'topup' and (not _utc(subscription.get('current_period_end')) or _utc(subscription['current_period_end']) <= now):
            raise ValueError('TOPUP_NOT_ELIGIBLE')
        expiredAttemptIds=set()
        expiredInvoiceIds=set()
        cursor.execute("select id,invoice_id,metadata_json,payment_status,provider_order_id from public.billing_events where user_id=%s and event_category='payment_attempt' and payment_status in ('created','pending_provider_ack','authorized') order by id for update",(userId,))
        for attempt in cursor.fetchall():
            expires=_utc(self._json(attempt.get('metadata_json')).get('manualBilling',{}).get('expiresAt'))
            if expires and expires <= now and self._mayExpireCheckoutAttempt(attempt, now):
                expiredAttemptIds.add(str(attempt['id']))
                self._expireCheckoutAttemptLocked(cursor,attempt,now)
                if attempt.get('invoice_id'):
                    expiredInvoiceIds.add(str(attempt['invoice_id']))
        # The invoice this reservation is about to use stays open.
        for expiredInvoiceId in expiredInvoiceIds-{str(snapshot.get('invoiceId'))}:
            self._expireCheckoutInvoiceLocked(cursor,expiredInvoiceId,now)
        if purpose != 'expert_addition':
            # An expert_addition reservation releases these itself below.
            self._releaseExpiredAdditionsLocked(cursor,subscription,expiredAttemptIds,now)

        subscriptionId = snapshot.get("subscriptionId")
        invoiceId = snapshot.get("invoiceId")
        attemptId = snapshot.get("attemptId") or str(uuid.uuid4())
        if purpose == 'expert_addition':
            domains=list(snapshot.get('domains') or [])
            if self._usesPurchaseAuthority():
                coverage = self._coverageSnapshotLocked(cursor, subscription, now, materialize=False)
                subscription = self._purchaseContextLocked(cursor, subscription, coverage, now)
                if snapshot.get('servicePeriodId') != coverage.currentPeriod.invoiceId:
                    raise ValueError('EXPERT_PERIOD_MISMATCH')
            current=subscription.get('subscribed_experts') or []
            current=json.loads(current) if isinstance(current,str) else list(current)
            pending=subscription.get('pending_additions') or []
            pending=json.loads(pending) if isinstance(pending,str) else list(pending)
            for item in pending:
                if item.get('attemptId') in expiredAttemptIds and item.get('state')=='awaiting_payment':
                    item.update(state='expired',expiredAt=now.isoformat())
            openDomains={item.get('domain') for item in pending if item.get('state')=='awaiting_payment'}
            if self._usesPurchaseAuthority():
                openDomains = {d for a in self._pendingExpertAttemptsLocked(cursor, subscription, coverage.currentPeriod.invoiceId)
                    for d in self._json(a['metadata_json'])['manualBilling']['domains']}
            if not domains or set(domains)&(set(current)|openDomains) or len(set(current)|openDomains|set(domains))>4:
                raise ValueError('EXPERT_SELECTION_CONFLICT')
            if not _utc(subscription.get('current_period_end')) or _utc(subscription['current_period_end']) <= now:
                raise ValueError('EXPERT_PAID_PERIOD_ENDED')
            pending.extend({'domain':domain,'attemptId':attemptId,'state':'awaiting_payment'} for domain in domains)
            cursor.execute('update public.subscriptions set pending_additions=%s where id=%s',(Json(pending),subscription['id']))
        cursor.execute(
            """
            insert into public.billing_events (
                id, user_id, subscription_id, invoice_id,
                event_category, event_type, event_status,
                payment_attempt_type, payment_status,
                provider, amount, currency,
                idempotency_key, metadata_json,
                period_start, period_end,
                attempted_at, occurred_at
            )
            values (
                %s, %s, %s, %s,
                'payment_attempt', 'payment.attempt', 'created',
                'authenticated_checkout', 'created',
                'razorpay', %s, %s,
                %s, %s,
                %s, %s,
                now(), now()
            )
            """,
            (
                attemptId,
                userId,
                subscriptionId,
                invoiceId,
                snapshot.get("amount"),
                snapshot.get("currency", "INR"),
                namespaceKey,
                Json({"manualBilling": manualBilling}),
                snapshot.get("periodStart"),
                snapshot.get("periodEnd"),
            ),
        )
        cursor.execute(
            """
            select id, user_id, subscription_id, invoice_id,
                   payment_status, provider_order_id,
                   metadata_json
            from public.billing_events
            where id = %s
            limit 1
            """,
            (attemptId,),
        )
        row = cursor.fetchone()
        return self._intentFromAttemptRow(row, {"manualBilling": manualBilling}, userId, purpose)


    def _intentFromAttemptRow(
        self, row: dict, metadata: dict, userId: str, purpose: str
    ) -> CheckoutIntent:
        billing = (metadata or {}).get("manualBilling") or {}
        expiresAt = _utc(billing.get("expiresAt")) or _now()
        state = str(row.get('payment_status') or 'created')
        observation = billing.get('providerObservation', {})
        if state == 'failed' and billing.get('closedReason') == 'ORDER_NOT_CREATED' and not row.get('provider_order_id'):
            # Release the review key while retaining the failed financial history.
            state = 'expired'
        elif state in ('created', 'pending_provider_ack', 'authorized') and observation.get('outcome') == 'captured':
            state = 'requires_reconciliation'
        elif state in ('created', 'pending_provider_ack'):
            if observation.get('outcome') == 'processing':
                state = 'provider_processing'
            elif observation.get('hasPayments') and expiresAt <= _now():
                state = 'requires_reconciliation'
        return CheckoutIntent(
            attemptId=str(row["id"]),
            invoiceId=str(row.get("invoice_id") or "") or "",
            userId=str(row.get("user_id") or userId),
            lifecycleId=str(billing.get("lifecycleId") or ""),
            purpose=purpose,
            billingMode=str(billing.get("billingMode") or ""),
            payloadHash=str(billing.get("payloadHash") or ""),
            currency=str(billing.get("currency") or "INR"),
            state=state,
            revision=int(billing.get("revision") or 1),
            amount=int(billing.get("frozenAmount") or 0),
            expiresAt=expiresAt,
            razorpayOrderId=row.get("provider_order_id"),
            snapshot=dict(billing),
        )

    def bindProviderOrder(self, attemptId: str, order: dict) -> CheckoutIntent:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("select user_id,invoice_id,provider_order_id from public.billing_events where id=%s and event_category='payment_attempt'",(attemptId,))
                previous=cursor.fetchone()
                if not previous: raise ValueError('ATTEMPT_MISSING')
                self._lockUser(cursor,previous['user_id'])
                previous = self._refreshFinancialOwnerLocked(cursor,attemptId,previous)
                subscription = self._canonical(cursor, previous['user_id']) if previous['user_id'] is not None else None
                if subscription:
                    cursor.execute('select id from public."Invoices" where id=%s and "userId"=%s for update',
                        (previous['invoice_id'], previous['user_id']))
                    if not cursor.fetchone():
                        raise ValueError('OWNED_INVOICE_NOT_FOUND')
                cursor.execute('select * from public.billing_events where id=%s for update', (attemptId,))
                previous = cursor.fetchone()
                if not order.get('id'):
                    raise ValueError('PROVIDER_ORDER_ID_MISSING')
                if ('amount' in order and int(order['amount']) != int(previous['amount'])) or (
                        'currency' in order and order['currency'] != previous['currency']):
                    raise ValueError('PROVIDER_ORDER_EVIDENCE_MISMATCH')
                if previous.get('provider_order_id') and previous['provider_order_id'] != order.get('id'):
                    raise ValueError('PROVIDER_ORDER_ALREADY_BOUND')
                cursor.execute(
                    """
                    update public.billing_events
                    set provider_order_id = %s,
                        payment_status = case when payment_status='created' then 'pending_provider_ack' else payment_status end,
                        event_status = case when payment_status='created' then 'pending_provider_ack' else event_status end
                    where id = %s
                      and (provider_order_id is null or provider_order_id = %s)
                    returning id, user_id, subscription_id, invoice_id,
                              payment_status, provider_order_id, metadata_json
                    """,
                    (order.get("id"), attemptId, order.get("id")),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        f"Attempt {attemptId} is not bindable (closed or missing)"
                    )
                from api.services.billing.purchaseCheckoutRepository import bindControlledOrderLocked
                bindControlledOrderLocked(cursor, attemptId, order['id'])
                if row['user_id'] is None:
                    return self._intentFromAttemptRow(row,self._json(row.get('metadata_json')),None,'erased_checkout')
                cursor.execute('update public."Invoices" set razorpay_order_id=%s where id=%s and "userId"=%s',
                               (order.get('id'),row['invoice_id'],row['user_id']))
                pending=subscription.get('pending_additions') or []
                pending=json.loads(pending) if isinstance(pending,str) else list(pending)
                changed=False
                for item in pending:
                    if item.get('attemptId') == attemptId:
                        item['orderId']=order.get('id')
                        changed=True
                if changed:
                    cursor.execute('update public.subscriptions set pending_additions=%s where id=%s',(Json(pending),subscription['id']))
                metadata = row.get("metadata_json") or {}
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except (ValueError, TypeError):
                        metadata = {}
                purpose = (
                    (metadata.get("manualBilling") or {}).get("purpose")
                    or "unknown"
                )
                return self._intentFromAttemptRow(row, metadata, row.get("user_id"), purpose)

        return self._run(operation)

    def claimProviderOrderCreation(self, attemptId: str) -> bool:
        """Persist an unknown outcome before the network call; retries must reconcile."""
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('select user_id,invoice_id from public.billing_events where id=%s', (attemptId,))
                owner = cursor.fetchone()
                if not owner or owner['user_id'] is None:
                    return False
                self._lockUser(cursor, owner['user_id'])
                owner = self._refreshFinancialOwnerLocked(cursor,attemptId,owner)
                if owner['user_id'] is None:
                    return False
                subscription = self._canonical(cursor, owner['user_id'])
                if subscription.get('erasure_pending'):
                    return False
                cursor.execute('select id from public."Invoices" where id=%s for update', (owner['invoice_id'],))
                cursor.fetchone()
                cursor.execute('select metadata_json from public.billing_events where id=%s for update', (attemptId,))
                metadata = self._json(cursor.fetchone()['metadata_json'])
                cursor.execute('select clock_timestamp() as current_time')
                metadata.setdefault('manualBilling', {})['orderCreationClaimedAt'] = _utc(cursor.fetchone()['current_time']).isoformat()
                cursor.execute("update public.billing_events set payment_status='pending_provider_ack',event_status='pending_provider_ack',metadata_json=%s where id=%s and payment_status='created' returning id",(Json(metadata), attemptId))
                return cursor.fetchone() is not None
        return self._run(operation)

    def attemptForOrder(self, orderId: str) -> dict:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("select * from public.billing_events where event_category='payment_attempt' and provider_order_id=%s",(orderId,))
                row=cursor.fetchone()
                if not row: raise ValueError('PAYMENT_ATTEMPT_MISSING')
                return row
        return self._run(operation)

    def attemptById(self, userId: str, attemptId: str) -> dict:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("select * from public.billing_events where id=%s and user_id=%s and event_category='payment_attempt'",
                    (attemptId, userId))
                row = cursor.fetchone()
                if not row:
                    raise ValueError('OWNED_PAYMENT_ATTEMPT_MISSING')
                return row
        return self._run(operation)

    def mayResumeInitialCheckout(self, userId: str, attemptId: str) -> bool:
        """Advisory UI capability; submission still rechecks owner and money."""
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                subscription = self._canonical(cursor, userId)
                now = _now()
                coverage = self._coverageSnapshotLocked(cursor, subscription, now, materialize=False)
                if coverage.currentPeriod is not None or coverage.denialReason not in (
                        None, 'no_paid_coverage', 'no_subscription', 'expired'):
                    return False
                cursor.execute("select * from public.billing_events where id=%s and user_id=%s and event_category='payment_attempt'",
                    (attemptId, userId))
                attempt = cursor.fetchone()
                if not attempt or str(attempt.get('subscription_id')) != str(subscription['id']):
                    return False
                frozen = self._json(attempt.get('metadata_json')).get('manualBilling', {})
                observation = frozen.get('providerObservation', {})
                checked = _utc(observation.get('checkedAt'))
                expiry = _utc(frozen.get('expiresAt'))
                return bool(frozen.get('purpose') == 'initial_purchase' and not frozen.get('closedAt')
                    and attempt.get('payment_status') in ('created', 'pending_provider_ack')
                    and attempt.get('provider_order_id') == observation.get('orderId')
                    and observation.get('complete') and observation.get('outcome') == 'unpaid'
                    and checked and timedelta(0) <= now - checked <= timedelta(seconds=60)
                    and expiry and expiry > now)
        return self._run(operation)

    def recordUnmappedPayment(self, payment: dict):
        """Unknown historical money remains visible without guessing its owner or grant."""
        def operation(connection):
            with connection.cursor() as cursor:
                cursor.execute('''insert into public.billing_events(id,event_category,event_type,event_status,
                    provider,amount,currency,idempotency_key,metadata_json,occurred_at)
                    values(%s,'reconciliation','payment.unmapped','REQUIRES_RECONCILIATION','razorpay',%s,%s,%s,%s,%s)
                    on conflict(idempotency_key) where idempotency_key is not null do nothing''', (str(uuid.uuid4()),int(payment.get('amount') or 0),
                    payment.get('currency') or 'INR','unmapped:'+str(payment.get('id'))+':'+str(payment.get('status')),
                    Json({'paymentId':payment.get('id'),'orderId':payment.get('order_id'),
                          'financialStatus':payment.get('status'),'reason':'PAYMENT_ATTEMPT_MISSING'}),_now()))
        return self._run(operation)

    # -- finalization ---------------------------------------------------------

    def finalizeCapturedPayment(
        self, evidence: VerifiedPaymentEvidence
    ) -> FinalizationResult:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, evidence.userId)
                cursor.execute('select user_id from public.billing_events where id=%s',(evidence.attemptId,))
                owner = cursor.fetchone()
                if owner and owner['user_id'] is None:
                    self._lockUser(cursor,None)
                    cursor.execute('select * from public.billing_events where id=%s',(evidence.attemptId,))
                    erasedAttempt = cursor.fetchone()
                    if evidence.userId is not None:
                        requestId = self._json(erasedAttempt.get('metadata_json')).get('erasureRequestId')
                        cursor.execute('SELECT request_id FROM private.retained_user_subjects WHERE request_id=%s AND original_user_id=%s',
                            (requestId,evidence.userId))
                        if not cursor.fetchone():
                            raise ValueError('ERASED_CAPTURE_OWNER_MISMATCH')
                    return self._recordErasedCapture(cursor,erasedAttempt,evidence)
                subscription = self._canonical(cursor, evidence.userId)
                existingCoverage = self._paidInvoicesLocked(cursor, subscription) if evidence.purpose == 'initial_purchase' else []
                cursor.execute('select * from public."Invoices" where id = %s for update', (evidence.invoiceId,))
                invoice = cursor.fetchone()
                cursor.execute('select * from public.billing_events where id = %s for update', (evidence.attemptId,))
                attempt = cursor.fetchone()
                if not invoice or not attempt or invoice['userId'] != evidence.userId or attempt['user_id'] != evidence.userId:
                    raise ValueError('OWNERSHIP_MISMATCH')
                frozen = self._json(attempt.get('metadata_json')).get('manualBilling', {})
                if (str(attempt.get('invoice_id')) != evidence.invoiceId or
                    str(attempt.get('subscription_id')) != str(subscription['id']) or
                    attempt.get('provider_order_id') != evidence.providerOrderId or
                    frozen.get('purpose') != evidence.purpose or
                    int(attempt.get('amount') or 0) != evidence.amount or
                    str(attempt.get('currency')).upper() != evidence.currency.upper()):
                    raise ValueError('PAYMENT_EVIDENCE_MISMATCH')
                if evidence.financialStatus != 'captured':
                    if evidence.financialStatus == 'authorized' and attempt['payment_status'] in ('created', 'pending_provider_ack', 'authorized', 'cancelled'):
                        previous = frozen.get('providerObservation', {})
                        if previous.get('outcome') != 'captured' and (not frozen.get('closedAt') or frozen.get('purchaseCancellationAcceptedAt')):
                            attemptMetadata = self._json(attempt.get('metadata_json'))
                            attemptBilling = attemptMetadata.setdefault('manualBilling', {})
                            attemptBilling.setdefault('authorizedPaymentId', evidence.providerPaymentId)
                            if frozen.get('purchaseCancellationAcceptedAt'):
                                attemptBilling.pop('closureReconciledAt', None)
                                invoiceMetadata = self._json(invoice.get('metadata_json'))
                                invoiceMetadata.setdefault('manualBilling', {}).pop('closureReconciledAt', None)
                                cursor.execute('UPDATE public."Invoices" SET metadata_json=%s WHERE id=%s', (Json(invoiceMetadata), invoice['id']))
                            # A validated fetched payment proves authorization,
                            # but is not a complete listing of all Order payments.
                            if previous.get('outcome') != 'captured' and (
                                    not previous.get('checkedAt') or _utc(previous['checkedAt']) <= evidence.observedAt):
                                attemptBilling['providerObservation'] = {'orderId': evidence.providerOrderId,
                                    'outcome': 'authorized', 'checkedAt': evidence.observedAt.isoformat(),
                                    'complete': False, 'hasPayments': True}
                            cursor.execute('update public.billing_events set payment_status=%s,metadata_json=%s where id=%s',
                                ('authorized', Json(attemptMetadata), evidence.attemptId))
                    return self._result(cursor,invoice, subscription, 'awaiting_capture', evidence.attemptId)
                # A payment identity is global; a separate grant identity is per invoice.
                cursor.execute('select * from public.billing_events where provider_payment_id = %s for update', (evidence.providerPaymentId,))
                capture = cursor.fetchone()
                if capture:
                    if str(capture.get('invoice_id')) != evidence.invoiceId or capture['user_id'] != evidence.userId:
                        raise ValueError('PAYMENT_ALREADY_BOUND')
                    state = 'already_finalized' if invoice['status'] == 'PAID' and invoice.get('razorpayPaymentId') == evidence.providerPaymentId else 'requires_reconciliation'
                    return self._result(cursor,invoice, subscription, state, evidence.attemptId, anomalyId=capture['id'] if state == 'requires_reconciliation' else None)
                captureId = str(uuid.uuid4())
                cursor.execute('''insert into public.billing_events
                    (id,user_id,subscription_id,invoice_id,event_category,event_type,event_status,provider,
                     provider_order_id,provider_payment_id,amount,currency,idempotency_key,metadata_json,occurred_at)
                    values (%s,%s,%s,%s,'reconciliation','payment.capture','OBSERVED','razorpay',%s,%s,%s,%s,%s,%s,%s)''',
                    (captureId,evidence.userId,subscription['id'],evidence.invoiceId,evidence.providerOrderId,
                     evidence.providerPaymentId,evidence.amount,evidence.currency,'capture:'+evidence.providerPaymentId,
                     Json({'timingKind':evidence.timingKind,'provenCaptureAt':evidence.provenCaptureAt.isoformat() if evidence.provenCaptureAt else None}), evidence.observedAt))
                return self._evaluateCaptureLocked(cursor, subscription, invoice, attempt, evidence, captureId, existingCoverage)
        return self._run(operation)

    def _evaluateCaptureLocked(self, cursor, subscription, invoice, attempt, evidence, captureId, existingCoverage):
        """Apply the original-purchase rules to one recorded capture under the owner lock.

        Shared by first observation and the audited staff recheck; either the
        capture finalizes its frozen invoice or it is left REQUIRES_RECONCILIATION.
        """
        frozen = self._json(attempt.get('metadata_json')).get('manualBilling', {})
        cutoff = _utc(frozen.get('expiresAt'))
        captureAt = evidence.provenCaptureAt if evidence.timingVerified else None
        # A server observation before the deadline proves captured funds existed then.
        captureAt = captureAt or evidence.observedAt
        reason = None
        if invoice['status'] == 'PAID': reason = 'EXCESS_CAPTURE'
        elif str(invoice['status']).upper() not in ('UPCOMING','PAYMENT_PENDING'): reason = 'CLOSED_INVOICE'
        elif frozen.get('closedAt'): reason = 'CLOSED_ATTEMPT'
        elif cutoff is None or captureAt >= cutoff: reason = 'CAPTURE_OUTSIDE_WINDOW'
        elif subscription.get('erasure_pending'): reason = 'ERASURE_PENDING'
        elif evidence.purpose not in ('initial_purchase','renewal','expert_addition','topup'): reason = 'UNSUPPORTED_PURPOSE'
        domains=frozen.get('domains') or []
        if evidence.purpose != 'topup' and (not 1 <= len(domains) <= 4 or len(set(domains)) != len(domains)):
            reason='INVALID_FROZEN_EXPERT_SELECTION'
        metadata = self._json(invoice.get('metadata_json'))
        billing = metadata.setdefault('manualBilling', {})
        if self._usesPurchaseAuthority() and (any(billing.get(k) != frozen.get(k) for k in ('purpose', 'billingMode', 'lifecycleId'))
                or set(billing.get('domains', [])) != set(frozen.get('domains', []))
                or int(billing.get('revision') or 1) != int(frozen.get('revision') or 1)
                or int(invoice.get('total_amount') or 0) != evidence.amount or invoice.get('currency') != evidence.currency):
            reason = 'FROZEN_INVOICE_MISMATCH'
        if evidence.purpose == 'initial_purchase' and frozen.get('purchaseCancellationAcceptedAt'):
            reason = 'PURCHASE_CANCELLED'
        closed = self._json(attempt.get('metadata_json')).get('manualBilling', {})
        closedAt = _utc(closed.get('closedAt') or billing.get('closedAt'))
        closedReason = closed.get('closedReason') or billing.get('closedReason')
        if (reason in ('CLOSED_INVOICE', 'CLOSED_ATTEMPT') and evidence.purpose == 'renewal'
                and (frozen.get('billingMode') == 'monthly_prepaid' or self._usesPurchaseAuthority())
                and closedReason in ('RENEWAL_DECLINED','UNPAID_PERIOD_ENDED') and closedAt
                and evidence.timingVerified and evidence.provenCaptureAt
                and evidence.provenCaptureAt < closedAt and cutoff and evidence.provenCaptureAt < cutoff
                and not subscription.get('erasure_pending') and not billing.get('revokedAt')):
            reason = None
        if evidence.purpose == 'renewal':
            if (reason is None and (frozen.get('billingMode') == 'annual_prepaid' or self._usesPurchaseAuthority())
                    and self._coverageSnapshotLocked(cursor, subscription, evidence.observedAt, materialize=False).nextPeriod):
                reason = 'FUTURE_PERIOD_ALREADY_PAID'
            lifecycle = self._json(subscription.get('billing_state')).get('manualBilling', {}).get('lifecycleId')
            cycleEnd = _utc(subscription.get('current_period_end'))
            if self._usesPurchaseAuthority():
                parent = self._coverageSnapshotLocked(cursor, subscription, captureAt, materialize=False).currentPeriod
                lifecycle, cycleEnd = (parent.lifecycleId, parent.end) if parent else (None, None)
            if frozen.get('lifecycleId') != lifecycle: reason = 'STALE_LIFECYCLE'
            if _utc(invoice.get('period_start')) != cycleEnd: reason = 'STALE_CYCLE'
        elif evidence.purpose == 'initial_purchase' and not self._usesPurchaseAuthority() and subscription.get('billing_mode') in ('monthly_prepaid','annual_prepaid') and (_utc(subscription.get('current_period_end')) or captureAt) > captureAt:
            reason = 'EXISTING_PAID_COVERAGE'
        if evidence.purpose == 'initial_purchase':
            from dateutil.relativedelta import relativedelta
            newEnd = captureAt + (relativedelta(years=1) if frozen['billingMode'] == 'annual_prepaid' else relativedelta(months=1))
            for paid in existingCoverage:
                paidBilling = self._json(paid.get('metadata_json')).get('manualBilling', {})
                paidStart, paidEnd = _utc(paid.get('period_start')), _utc(paid.get('period_end'))
                if (paidBilling.get('purpose') in ('initial_purchase','renewal')
                        and not paidBilling.get('revokedAt') and paidStart and paidEnd
                        and paidStart < newEnd and captureAt < paidEnd):
                    reason = 'EXISTING_PAID_COVERAGE'
        if evidence.purpose == 'expert_addition':
            if self._usesPurchaseAuthority():
                coverage = self._coverageSnapshotLocked(cursor, subscription, evidence.observedAt, materialize=False)
                period = coverage.currentPeriod
                if not period or frozen.get('servicePeriodId') != period.invoiceId or billing.get('servicePeriodId') != period.invoiceId:
                    reason = 'EXPERT_PERIOD_CLOSED'
                elif (frozen.get('lifecycleId') != period.lifecycleId or billing.get('lifecycleId') != period.lifecycleId
                        or _utc(invoice.get('period_end')) != period.end):
                    reason = 'STALE_LIFECYCLE_OR_CYCLE'
                elif set(frozen.get('domains', [])) != set(billing.get('domains', [])):
                    reason = 'EXPERT_SELECTION_MISMATCH'
            else:
                pending = subscription.get('pending_additions') or []
                pending = json.loads(pending) if isinstance(pending,str) else list(pending)
                matches = [item for item in pending if item.get('orderId') == evidence.providerOrderId]
                domains = frozen.get('domains') or []
                if not matches or any(item.get('state') != 'awaiting_payment' for item in matches): reason = 'CLOSED_EXPERT_ATTEMPT'
                elif set(domains) != {item.get('domain') for item in matches}: reason = 'EXPERT_SELECTION_MISMATCH'
                elif not _utc(subscription.get('current_period_end')) or _utc(subscription['current_period_end']) <= captureAt or _utc(invoice.get('period_end')) != _utc(subscription['current_period_end']): reason = 'EXPERT_PERIOD_CLOSED'
                elif frozen.get('lifecycleId') != self._json(subscription.get('billing_state')).get('manualBilling',{}).get('lifecycleId'): reason = 'STALE_LIFECYCLE'
        if reason:
            cursor.execute('update public.billing_events set event_status = %s, failure_reason = %s where id = %s', ('REQUIRES_RECONCILIATION',reason,captureId))
            return self._result(cursor,invoice,subscription,'requires_reconciliation',evidence.attemptId,anomalyId=captureId)
        if evidence.purpose == 'expert_addition':
            return self._finalizeExpertCapture(cursor,invoice,subscription,attempt,evidence,captureId,metadata)
        if evidence.purpose == 'topup':
            return self._finalizeTopupCapture(cursor,invoice,subscription,attempt,evidence,captureId)
        from dateutil.relativedelta import relativedelta
        start = captureAt if evidence.purpose == 'initial_purchase' else _utc(invoice['period_start'])
        end = start + (relativedelta(years=1) if frozen['billingMode'] == 'annual_prepaid' else relativedelta(months=1))
        if evidence.purpose == 'renewal' and _utc(invoice['period_end']) != end:
            raise ValueError('INVALID_FROZEN_CALENDAR_PERIOD')
        billing.update({'lifecycleId':frozen['lifecycleId'],'domains':frozen.get('domains',[]),
            'billingMode':frozen['billingMode'],'purpose':frozen['purpose'],
            'creditPeriodId':str(uuid.uuid4()),'coverageState':'scheduled','providerPaymentId':evidence.providerPaymentId})
        cursor.execute('''update public."Invoices" set status='PAID', "razorpayPaymentId"=%s,
            "paidAt"=%s, period_start=%s, period_end=%s, metadata_json=%s where id=%s''',
            (evidence.providerPaymentId,captureAt,start,end,Json(metadata),invoice['id']))
        invoice.update(status='PAID',razorpayPaymentId=evidence.providerPaymentId,period_start=start,period_end=end,metadata_json=metadata)
        previousCancellationRequestedAt = (_isoText(subscription.get('cancellation_requested_at')) if self._usesPurchaseAuthority() else
            self._json(subscription.get('billing_state')).get('manualBilling', {}).get('cancellationRequestedAt'))
        cursor.execute("update public.billing_events set payment_status='captured',event_status='captured',completed_at=%s where id=%s", (evidence.observedAt,attempt['id']))
        cursor.execute("update public.billing_events set event_status='FINALIZED' where id=%s",(captureId,))
        if self._usesPurchaseAuthority() and evidence.purpose == 'initial_purchase':
            cursor.execute('''UPDATE public.subscriptions SET plan_code='pro',lifecycle_id=%s,
                trial_closed_at=CASE WHEN trial_started_at IS NOT NULL THEN coalesce(trial_closed_at,%s) ELSE NULL END,
                cancellation_requested_at=NULL,version=version+1 WHERE id=%s RETURNING *''',
                (frozen['lifecycleId'], captureAt, subscription['id']))
            subscription.update(dict(cursor.fetchone()))
        self._recordNotification(cursor, subscription, 'payment_receipt',
            'receipt:'+evidence.providerPaymentId, {'paymentId':evidence.providerPaymentId,'invoiceId':invoice['id'], 'amount':evidence.amount,'currency':evidence.currency,'periodEnd':end.isoformat()})
        state = self._json(subscription.get('billing_state'))
        if evidence.purpose == 'initial_purchase':
            state.setdefault('manualBilling',{}).pop('cancellationRequestedAt',None)
        state.setdefault('manualBilling',{}).update(lifecycleId=frozen['lifecycleId'],paidFutureEnd=end.isoformat())
        cursor.execute('update public.subscriptions set billing_state=%s where id=%s', (Json(state),subscription['id']))
        subscription['billing_state'] = state
        if evidence.purpose == 'initial_purchase':
            # Preferences are lifecycle-scoped; retain their old values.
            cursor.execute('''insert into public.billing_events(id,user_id,subscription_id,invoice_id,
                event_category,event_type,event_status,idempotency_key,metadata_json,occurred_at)
                values(%s,%s,%s,%s,'audit','subscription.lifecycle.started','processed',%s,%s,%s)
                on conflict(idempotency_key) where idempotency_key is not null do nothing''',
                (str(uuid.uuid4()), evidence.userId, subscription['id'], invoice['id'],
                 'lifecycle-start:' + frozen['lifecycleId'], Json({'previousRenewalOptOut':bool(subscription.get('renewal_opt_out')),
                 'previousCancellationReason':subscription.get('cancellation_reason'),
                 'previousCancellationRequestedAt':previousCancellationRequestedAt,
                 'lifecycleId':frozen['lifecycleId']}), evidence.observedAt))
            cursor.execute('update public.subscriptions set renewal_opt_out=false,cancellation_reason=null where id=%s', (subscription['id'],))
            subscription.update(renewal_opt_out=False, cancellation_reason=None)
        if frozen['billingMode'] == 'annual_prepaid':
            if self._usesPurchaseAuthority() and subscription.get('renewal_opt_out'):
                self._refreshCancellationFactsLocked(cursor, subscription, end)
            return self._applyAnnualPayment(cursor, invoice, subscription, evidence.observedAt, evidence.attemptId)
        if subscription.get('renewal_opt_out'):
            self._refreshCancellationFactsLocked(cursor, subscription, end)
        return self._applyCoverage(cursor,invoice,subscription,evidence.observedAt,evidence.attemptId)

    def reconsiderCaptureLocked(self, cursor, capture, evidence):
        """Re-evaluate one unresolved capture once with provider-attested capture time.

        The caller holds the owner lock and the capture row. Returns
        (result or None, reasonCode); every original validation still applies,
        and staff-entered or recheck-time observations never count as timing.
        """
        stored = self._json(capture.get('metadata_json'))
        if stored.get('reconsideredAt') or stored.get('timingKind') == 'provider_captured_at':
            return None, 'RECHECK_ALREADY_APPLIED'
        if not (evidence.timingVerified and evidence.provenCaptureAt and evidence.timingKind == 'provider_captured_at'):
            return None, 'CAPTURE_TIMING_UNVERIFIED'
        subscription = self._canonical(cursor, evidence.userId)
        existingCoverage = self._paidInvoicesLocked(cursor, subscription) if evidence.purpose == 'initial_purchase' else []
        cursor.execute('select * from public."Invoices" where id = %s for update', (evidence.invoiceId,))
        invoice = cursor.fetchone()
        cursor.execute('select * from public.billing_events where id = %s for update', (evidence.attemptId,))
        attempt = cursor.fetchone()
        frozen = self._json((attempt or {}).get('metadata_json')).get('manualBilling', {})
        if (not invoice or not attempt or invoice['userId'] != evidence.userId or attempt['user_id'] != evidence.userId
                or capture['user_id'] != evidence.userId or str(capture.get('invoice_id')) != evidence.invoiceId
                or capture.get('provider_payment_id') != evidence.providerPaymentId
                or capture.get('provider_order_id') != evidence.providerOrderId
                or int(capture.get('amount') or 0) != evidence.amount
                or str(capture.get('currency')).upper() != evidence.currency.upper()
                or str(attempt.get('invoice_id')) != evidence.invoiceId
                or str(attempt.get('subscription_id')) != str(subscription['id'])
                or attempt.get('provider_order_id') != evidence.providerOrderId
                or frozen.get('purpose') != evidence.purpose
                or int(attempt.get('amount') or 0) != evidence.amount
                or str(attempt.get('currency')).upper() != evidence.currency.upper()
                or evidence.financialStatus != 'captured'):
            return None, 'PROVIDER_EVIDENCE_MISMATCH'
        # A historical interval never becomes a closed case: money for service the
        # customer can no longer receive stays open for the deferred refund policy.
        if evidence.purpose == 'initial_purchase':
            from dateutil.relativedelta import relativedelta
            intervalEnd = evidence.provenCaptureAt + (relativedelta(years=1) if frozen.get('billingMode') == 'annual_prepaid' else relativedelta(months=1))
        else:
            intervalEnd = _utc(invoice.get('period_end'))
        cursor.execute('select clock_timestamp() as current_time')
        if intervalEnd is not None and intervalEnd <= _utc(cursor.fetchone()['current_time']):
            return None, 'ORIGINAL_INTERVAL_ELAPSED'
        stored.update(timingKind=evidence.timingKind, provenCaptureAt=evidence.provenCaptureAt.isoformat(),
                      reconsideredAt=evidence.observedAt.isoformat())
        cursor.execute('update public.billing_events set metadata_json=%s where id=%s', (Json(stored), str(capture['id'])))
        result = self._evaluateCaptureLocked(cursor, subscription, invoice, attempt, evidence, str(capture['id']), existingCoverage)
        if result.state == 'requires_reconciliation':
            cursor.execute('select failure_reason from public.billing_events where id=%s', (str(capture['id']),))
            return result, cursor.fetchone()['failure_reason']
        return result, None

    def _recordErasedCapture(self,cursor,attempt,evidence):
        """Retain received-money evidence after anonymisation; grant no access."""
        cursor.execute('select * from public."Invoices" where id=%s for update',(evidence.invoiceId,))
        invoice=cursor.fetchone()
        if (not invoice or invoice['userId'] is not None or str(attempt['invoice_id'])!=evidence.invoiceId
            or attempt.get('provider_order_id')!=evidence.providerOrderId or int(attempt['amount'])!=evidence.amount
            or attempt['currency']!=evidence.currency):
            raise ValueError('ERASED_CAPTURE_EVIDENCE_MISMATCH')
        if evidence.financialStatus!='captured':
            return FinalizationResult(evidence.invoiceId,evidence.attemptId,'awaiting_capture','unchanged',
                False,False,True,None,None,None)
        cursor.execute('select * from public.billing_events where provider_payment_id=%s for update',(evidence.providerPaymentId,))
        existing=cursor.fetchone()
        if existing and (str(existing['invoice_id']) != evidence.invoiceId
                or existing.get('provider_order_id')!=evidence.providerOrderId
                or int(existing.get('amount') or 0)!=evidence.amount or existing.get('currency')!=evidence.currency):
            raise ValueError('PAYMENT_ALREADY_BOUND')
        anomalyId=str(existing['id']) if existing else str(uuid.uuid4())
        if not existing:
            cursor.execute('''insert into public.billing_events(id,invoice_id,event_category,event_type,event_status,
                provider_order_id,provider_payment_id,amount,currency,idempotency_key,failure_reason,metadata_json,occurred_at)
                values(%s,%s,'reconciliation','payment.capture','REQUIRES_RECONCILIATION',%s,%s,%s,%s,%s,'ERASED_ACCOUNT',%s,%s)''',
                (anomalyId,evidence.invoiceId,evidence.providerOrderId,evidence.providerPaymentId,evidence.amount,
                 evidence.currency,'capture:'+evidence.providerPaymentId,
                 Json({'erasureRequestId':self._json(attempt.get('metadata_json')).get('erasureRequestId')}),evidence.observedAt))
        return FinalizationResult(evidence.invoiceId,evidence.attemptId,'requires_reconciliation','unchanged',
            False,False,True,None,None,anomalyId)

    def activateDueCoverage(self, userId: str, now: datetime) -> FinalizationResult:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor,userId)
                subscription = self._canonical(cursor,userId)
                return self._activateDueCoverageLocked(cursor, subscription, _utc(now))
        return self._run(operation)

    def _unresolvedCycleCaptureLocked(self, cursor, subscription, cycleEnd):
        """One owned-cycle predicate shared by commit, enqueue, sweep and dispatch.

        Invoices frozen to another lifecycle do not hold this one; invoices
        without a frozen lifecycle are treated as owned (conservative).
        """
        lifecycle = self._json(subscription.get('billing_state')).get('manualBilling', {}).get('lifecycleId')
        lifecycleClause = " and coalesce(i.metadata_json->'manualBilling'->>'lifecycleId', %s)=%s" if lifecycle else ''
        cursor.execute('''select e.id from public.billing_events e
            join public."Invoices" i on i.id=e.invoice_id
            where e.user_id=%s and i."userId"=%s and i.subscription_id=%s
            and i.billing_reason='renewal' and i.period_start=%s''' + lifecycleClause + '''
            and ((e.event_type='payment.capture' and e.event_status in ('OBSERVED','REQUIRES_RECONCILIATION'))
              or (e.event_category='payment_attempt' and e.payment_status in ('authorized','captured') and upper(i.status)<>'PAID'))
            limit 1''', (subscription['user_id'], subscription['user_id'], subscription['id'], cycleEnd)
                + ((str(lifecycle), str(lifecycle)) if lifecycle else ()))
        return cursor.fetchone() is not None

    def hasUnresolvedCycleCapture(self, userId, cycleEnd):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                subscription = self._canonical(cursor, userId)
                return self._unresolvedCycleCaptureLocked(cursor, subscription, cycleEnd)
        return self._run(operation)

    def enqueueSubscriptionNotification(self, intent, now):
        """Recheck an owned cycle and accepted revision under the financial lock."""
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor,intent['userId'])
                row = self._canonical(cursor,intent['userId'])
                from api.services.subscriptions.subscriptionModel import evaluateSubscription
                owned = evaluateSubscription(self._modelRepository().loadRuntimeFacts(cursor,row,now),now)
                metadata = dict(intent.get('metadata') or {})
                invoice = None
                if metadata.get('invoiceId'):
                    cursor.execute('SELECT * FROM public."Invoices" WHERE id=%s AND "userId"=%s AND subscription_id=%s',
                        (metadata['invoiceId'],row['user_id'],row['id']))
                    invoice = cursor.fetchone()
                    if invoice:
                        metadata['invoiceRevision'] = self._json(invoice.get('metadata_json')).get('manualBilling',{}).get('revision')
                from api.services.notifications.billingNotificationService import isBillingNotificationEligible
                delivery = {'notification_type':intent['notificationType'],'period_end':intent['periodEnd'],'metadata_json':metadata}
                if not isBillingNotificationEligible(delivery,{'subscription':row,'invoice':invoice,'subscriptionSnapshot':owned},now):
                    return False
                metadata['periodEnd'] = intent['periodEnd']
                self._recordNotification(cursor,row,intent['notificationType'],intent['dedupeKey'],metadata)
                return True
        return self._run(operation)

    def _paidInvoicesLocked(self, cursor, subscription):
        cursor.execute('''select * from public."Invoices" where "userId"=%s and subscription_id=%s
            and status='PAID' order by id for update''', (subscription['user_id'], subscription['id']))
        return list(cursor.fetchall())

    def _activateDueCoverageLocked(self, cursor, subscription, now, invoices=None):
        if self._usesPurchaseAuthority():
            coverage = self._coverageSnapshotLocked(cursor, subscription, now, materialize=False)
            if coverage.accessAllowed and coverage.currentPeriod:
                from api.services.credits.manualCreditRepository import ManualCreditRepository
                ManualCreditRepository(self).ensureCurrentAllocation(cursor, coverage.currentPeriod, now)
            return self._result(cursor, {}, subscription, 'unchanged' if coverage.currentPeriod else 'expired', None, now=now)
        invoices = self._paidInvoicesLocked(cursor, subscription) if invoices is None else invoices
        result = self._result(cursor,{}, subscription, 'unchanged', None)
        lifecycle = self._json(subscription.get('billing_state')).get('manualBilling', {}).get('lifecycleId')
        if subscription.get('erasure_pending'):
            return result
        for invoice in sorted(invoices, key=lambda row: (_utc(row.get('period_start')) or now, str(row['id']))):
            billing = self._json(invoice.get('metadata_json')).get('manualBilling', {})
            start = _utc(invoice.get('period_start'))
            if (billing.get('lifecycleId') == lifecycle and lifecycle
                    and billing.get('coverageState') == 'scheduled' and not billing.get('revokedAt')
                    and start is not None and start <= now
                    and start == _utc(subscription.get('current_period_end'))):
                result = self._applyCoverage(cursor, invoice, subscription, now, None)
        end = _utc(subscription.get('current_period_end'))
        if subscription.get('billing_mode') == 'monthly_prepaid' and end and end <= now:
            cursor.execute('''select * from public."Invoices" where "userId"=%s and subscription_id=%s
                and billing_reason='renewal' and period_start=%s and status in ('UPCOMING','PAYMENT_PENDING') order by id for update''',
                (subscription['user_id'], subscription['id'], end))
            for unpaid in cursor.fetchall():
                self._closeInvoice(cursor, unpaid, 'UNPAID_PERIOD_ENDED')
                cursor.execute("update public.\"Invoices\" set status='EXPIRED' where id=%s", (unpaid['id'],))
            if subscription.get('status') == 'expired':
                return self._result(cursor,{}, subscription, 'expired', None)
            cursor.execute("""update public.subscriptions set status='expired',plan_type='none',
                subscribed_experts=%s,domain_count=0,pending_removals=%s,pending_additions=%s,
                updated_at=%s where id=%s""", (Json([]), Json([]), Json([]), now, subscription['id']))
            cursor.execute('''update public.credit_balances set plan_tier='none',domain_count=0,
                monthly_token_quota=0,remaining_tokens=0,balance_version=balance_version+1,
                updated_at=%s where user_id=%s''', (now, subscription['user_id']))
            if not subscription.get('renewal_opt_out') and not self._unresolvedCycleCaptureLocked(cursor, subscription, end):
                self._recordNotification(cursor, subscription, 'monthly_subscription_expired',
                    'monthly:' + str(lifecycle) + ':' + end.isoformat() + ':expired',
                    {'periodEnd': end.isoformat(), 'lifecycleId': lifecycle,
                     'cycleId': end.isoformat(), 'milestone': 'expired'})
            subscription.update(status='expired', plan_type='none', subscribed_experts=[], domain_count=0,
                                pending_removals=[], pending_additions=[])
            return self._result(cursor,{}, subscription, 'expired', None)
        return result

    def getCoverageSnapshot(self, userId: str, now: datetime | None = None) -> CoverageSnapshot:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                subscription = self._canonical(cursor, userId)
                if now is None:
                    cursor.execute('select clock_timestamp() as current_time')
                    evaluated = _utc(cursor.fetchone()['current_time'])
                else:
                    evaluated = _utc(now)
                return self._coverageSnapshotLocked(cursor, subscription, evaluated, materialize=True)
        return self._run(operation)

    def _coverageSnapshotLocked(self, cursor, subscription: dict, now: datetime,
                               materialize: bool) -> CoverageSnapshot:
        cursor.execute('select "isBanned" from public."Users" where "userId"=%s', (subscription['user_id'],))
        owner = cursor.fetchone()
        if not owner:
            raise ValueError('OWNERSHIP_OR_USER_MISSING')
        if self._usesPurchaseAuthority():
            return self._purchaseCoverageLocked(cursor, subscription, owner, now)
        invoices = self._paidInvoicesLocked(cursor, subscription)
        if materialize and subscription.get('billing_mode') == 'monthly_prepaid':
            self._activateDueCoverageLocked(cursor, subscription, now, invoices)
        mode = subscription.get('billing_mode') or 'none'
        billing_state = self._json(subscription.get('billing_state')).get('manualBilling', {})
        lifecycle = str(billing_state.get('lifecycleId') or subscription['id'])
        periods = []
        for invoice in invoices:
            billing = self._json(invoice.get('metadata_json')).get('manualBilling', {})
            start, end = _utc(invoice.get('period_start')), _utc(invoice.get('period_end'))
            if not start or not end or end <= start or billing.get('revokedAt') or billing.get('coverageState') == 'revoked':
                continue
            if mode == 'monthly_prepaid' and (billing.get('lifecycleId') != lifecycle
                    or billing.get('purpose') not in ('initial_purchase', 'renewal')
                    or billing.get('coverageState') not in ('active', 'scheduled', 'elapsed')):
                continue
            if mode == 'annual_prepaid' and billing.get('purpose') not in ('initial_purchase', 'renewal'):
                # Existing annual payments predate manualBilling metadata. Only
                # owned paid service invoices with provider evidence qualify.
                if (billing.get('purpose') is not None or invoice.get('billing_reason') not in ('initial_purchase','renewal')
                        or not invoice.get('razorpayPaymentId')):
                    continue
            domains = (subscription.get('subscribed_experts') if start <= now < end
                       else billing.get('domains')) or billing.get('domains') or []
            if isinstance(domains, str):
                domains = json.loads(domains)
            periods.append(CoveragePeriod(subscription['user_id'], str(subscription['id']), lifecycle,
                str(billing.get('creditPeriodId') or ''), str(invoice['id']), start, end,
                tuple(domains), mode, None))
        current = next((period for period in periods if period.start <= now < period.end), None)
        future = sorted((period for period in periods if period.start > now), key=lambda period: period.start)
        next_period = next((period for period in future if current and period.start == current.end), None)
        final_end = next_period.end if next_period else current.end if current else None
        status = str(subscription.get('status') or 'none').lower()
        denied = 'erasure_pending' if subscription.get('erasure_pending') else 'account_banned' if owner.get('isBanned') else None
        if denied is None and status in ('suspended', 'paused'):
            denied = 'restricted_subscription'
        if denied is None and (current is None or mode not in ('monthly_prepaid', 'annual_prepaid')):
            denied = 'no_paid_coverage'
        return CoverageSnapshot(subscription['user_id'], str(subscription['id']), lifecycle, mode, now,
            current, next_period, final_end, bool(subscription.get('renewal_opt_out')), denied is None, denied)

    @staticmethod
    def _json(value):
        return json.loads(value) if isinstance(value,str) else dict(value or {})

    @staticmethod
    def _jsonList(value):
        return json.loads(value) if isinstance(value,str) else list(value or [])

    def _canonical(self,cursor,userId):
        cursor.execute('select * from public.subscriptions where user_id=%s and is_canonical=true for update',(userId,))
        row = cursor.fetchone()
        if not row: raise ValueError('OWNERSHIP_OR_CANONICAL_SUBSCRIPTION_MISSING')
        if cursor.fetchone(): raise ValueError('AMBIGUOUS_CANONICAL_SUBSCRIPTION')
        return row

    def _coverage(self,invoice):
        if not invoice.get('id'): return None
        billing = self._json(invoice.get('metadata_json')).get('manualBilling',{})
        if not billing.get('creditPeriodId'): return None
        return CoveragePeriod(invoice['userId'],str(invoice['subscription_id']),billing['lifecycleId'],
            billing['creditPeriodId'],str(invoice['id']),_utc(invoice['period_start']),_utc(invoice['period_end']),
            tuple(billing.get('domains',[])),billing.get('billingMode') or invoice.get('billing_mode') or 'monthly_prepaid',_utc(billing.get('revokedAt')))

    def _applyAnnualPayment(self, cursor, invoice, subscription, now, attemptId):
        """Annual service duration and monthly credit allocation have separate clocks."""
        if self._usesPurchaseAuthority():
            return self._applyPurchasedCoverage(cursor, invoice, subscription, now, attemptId)
        from api.services.credits.creditConfig import getTokenQuotaForPlan
        from dateutil.relativedelta import relativedelta
        cursor.execute('update public.subscriptions set default_currency=%s where id=%s',
                       (invoice['currency'], subscription['id']))
        subscription['default_currency'] = invoice['currency']
        metadata = self._json(invoice['metadata_json'])
        billing = metadata['manualBilling']
        period = self._coverage(invoice)
        preserveCurrent = period.start > now and self._coverageSnapshotLocked(cursor,subscription,now,materialize=False).currentPeriod is not None
        domains = (list(self._coverageSnapshotLocked(cursor, subscription, now, materialize=False).currentPeriod.domains)
            if preserveCurrent and self._usesPurchaseAuthority() else
            self._jsonList(subscription.get('subscribed_experts')) if preserveCurrent else list(period.domains))
        removals = self._jsonList(subscription.get('pending_removals')) if preserveCurrent else []
        quota = getTokenQuotaForPlan('annual', len(domains))
        cursor.execute('''insert into public.credit_balances (user_id,subscription_id,plan_tier,domain_count,
            monthly_token_quota,used_tokens,remaining_tokens,period_start,period_end,lifecycle_id,
            credit_period_id,balance_version,last_reset_at,updated_at)
            values (%s,%s,'annual',%s,%s,0,%s,%s,%s,%s,%s,1,%s,%s)
            on conflict(user_id) do update set subscription_id=excluded.subscription_id,plan_tier=excluded.plan_tier,
            domain_count=excluded.domain_count,monthly_token_quota=excluded.monthly_token_quota,used_tokens=0,
            remaining_tokens=excluded.remaining_tokens,period_start=excluded.period_start,period_end=excluded.period_end,
            lifecycle_id=excluded.lifecycle_id,credit_period_id=excluded.credit_period_id,
            balance_version=credit_balances.balance_version+1,last_reset_at=excluded.last_reset_at,updated_at=excluded.updated_at''',
            (period.userId,period.subscriptionId,len(domains),quota,quota,now,now+relativedelta(months=1),
             period.lifecycleId,period.creditPeriodId,now,now))
        billing['coverageState'] = 'scheduled' if period.start > now else 'active'
        billing['creditsAllocatedAt'] = now.isoformat()
        cursor.execute('update public."Invoices" set metadata_json=%s where id=%s', (Json(metadata),invoice['id']))
        cursor.execute('''update public.subscriptions set status='active',plan_type='annual',billing_mode='annual_prepaid',
            current_period_start=%s,current_period_end=%s,renewal_due_at=%s,subscribed_experts=%s,
            domain_count=%s,pending_removals=%s,auto_renew_enabled=false,version=version+1,updated_at=%s where id=%s''',
            (period.start,period.end,period.end,Json(domains),len(domains),Json(removals),now,subscription['id']))
        subscription.update(status='active',plan_type='annual',billing_mode='annual_prepaid',
            current_period_start=period.start,current_period_end=period.end,renewal_due_at=period.end,
            subscribed_experts=domains,domain_count=len(domains),pending_removals=removals)
        invoice['metadata_json'] = metadata
        return self._result(cursor,invoice,subscription,'paid_scheduled' if period.start > now else 'activated',attemptId,refilled=True)

    def _result(self,cursor,invoice,subscription,state,attemptId,refilled=False,anomalyId=None,now=None):
        subscription=self._canonical(cursor,subscription['user_id'])
        evaluated=now or _now()
        snapshot = self._coverageSnapshotLocked(cursor,subscription,evaluated,materialize=False)
        cursor.execute('select * from public.credit_balances where user_id=%s',(subscription['user_id'],))
        balance = cursor.fetchone()
        ready = (balance is not None and bool(balance.get('credit_period_id'))
            and str(balance.get('subscription_id'))==str(subscription['id'])
            and str(balance.get('lifecycle_id'))==snapshot.lifecycleId
            and balance.get('plan_tier')==('annual' if snapshot.billingMode=='annual_prepaid' else 'pro')
            and snapshot.accessAllowed and _utc(balance.get('period_start')) is not None
            and _utc(balance['period_start'])<=evaluated<_utc(balance['period_end']))
        if self._usesPurchaseAuthority():
            from api.services.credits.paidAllowancePolicy import paidCreditWindow, matchesPaidAllocation
            window = paidCreditWindow(snapshot.currentPeriod, evaluated) if snapshot.accessAllowed and snapshot.currentPeriod else None
            ready = bool(matchesPaidAllocation(balance, window) and balance.get('plan_tier') == 'pro'
                and int(balance.get('monthly_token_quota') or 0) > 0
                and int(balance.get('domain_count') or 0) == len(window.domains))
        if invoice.get('billing_reason')=='add_on' and balance is not None:
            ready=True  # Stored purchased credits do not imply subscription access.
        return FinalizationResult(str(invoice.get('id') or ''),attemptId,state,
            'ready' if ready else 'pending_materialization',
            state in ('activated','paid_scheduled','already_finalized','elapsed','expert_activated','topup_granted'),
            refilled,snapshot.renewalOptOut,snapshot.currentPeriod if snapshot.accessAllowed else None,
            snapshot.nextPeriod,anomalyId,str(invoice.get('status') or '').upper() or None)

    def _applyPurchasedCoverage(self, cursor, invoice, subscription, now, attemptId):
        """Payment schedules purchased time; only its current window can allocate."""
        period = self._coverage(invoice)
        if period.start > now:
            return self._result(cursor, invoice, subscription, 'paid_scheduled', attemptId, now=now)
        if period.end <= now:
            return self._result(cursor, invoice, subscription, 'elapsed', attemptId, now=now)
        from api.services.credits.manualCreditRepository import ManualCreditRepository
        coverage = self._coverageSnapshotLocked(cursor, subscription, now, materialize=False)
        cursor.execute('select credit_period_id from public.credit_balances where user_id=%s for update', (period.userId,))
        before = cursor.fetchone()
        allocated = None
        if coverage.accessAllowed and coverage.currentPeriod:
            allocated = ManualCreditRepository(self).ensureCurrentAllocation(cursor, coverage.currentPeriod, now)
        metadata = self._json(invoice['metadata_json'])
        metadata['manualBilling']['coverageState'] = 'active'
        cursor.execute('update public."Invoices" set metadata_json=%s where id=%s', (Json(metadata), invoice['id']))
        # V1 projection only; future payments leave current dates/experts intact.
        plan = 'annual' if period.billingMode == 'annual_prepaid' else 'pro'
        cursor.execute('''update public.subscriptions set status='active',plan_type=%s,billing_mode=%s,
            current_period_start=%s,current_period_end=%s,renewal_due_at=%s,subscribed_experts=%s,domain_count=%s,
            pending_removals=%s,auto_renew_enabled=false,default_currency=%s,version=version+1,updated_at=%s where id=%s''',
            (plan, period.billingMode, period.start, period.end, period.end, Json(list(period.domains)),
             len(period.domains), Json([]), invoice['currency'], now, subscription['id']))
        invoice['metadata_json'] = metadata
        refilled = bool(allocated and (not before or str(before['credit_period_id']) != allocated))
        return self._result(cursor, invoice, subscription, 'activated', attemptId, refilled=refilled, now=now)

    def _applyCoverage(self,cursor,invoice,subscription,now,attemptId):
        if self._usesPurchaseAuthority():
            return self._applyPurchasedCoverage(cursor, invoice, subscription, now, attemptId)
        cursor.execute('update public.subscriptions set default_currency=%s where id=%s',
                       (invoice['currency'], subscription['id']))
        subscription['default_currency'] = invoice['currency']
        metadata = self._json(invoice.get('metadata_json'))
        billing = metadata['manualBilling']
        period = self._coverage(invoice)
        if period.start > now: return self._result(cursor,invoice,subscription,'paid_scheduled',attemptId,now=now)
        if period.end <= now:
            billing['coverageState']='elapsed'
            cursor.execute('update public."Invoices" set metadata_json=%s where id=%s',(Json(metadata),invoice['id']))
            cursor.execute('update public.subscriptions set current_period_start=%s,current_period_end=%s,renewal_due_at=%s where id=%s',
                (period.start,period.end,period.end,subscription['id']))
            subscription.update(current_period_start=period.start,current_period_end=period.end)
            return self._result(cursor,invoice,subscription,'elapsed',attemptId,now=now)
        from api.services.credits.creditConfig import getTokenQuotaForPlan
        quota = getTokenQuotaForPlan('pro',len(period.domains))
        cursor.execute('''insert into public.credit_balances (user_id,subscription_id,plan_tier,domain_count,
            monthly_token_quota,used_tokens,remaining_tokens,period_start,period_end,lifecycle_id,
            credit_period_id,balance_version,last_reset_at,updated_at)
            values (%s,%s,'pro',%s,%s,0,%s,%s,%s,%s,%s,1,%s,%s)
            on conflict(user_id) do update set subscription_id=excluded.subscription_id,plan_tier=excluded.plan_tier,
            domain_count=excluded.domain_count,monthly_token_quota=excluded.monthly_token_quota,used_tokens=0,
            remaining_tokens=excluded.remaining_tokens,period_start=excluded.period_start,period_end=excluded.period_end,
            lifecycle_id=excluded.lifecycle_id,credit_period_id=excluded.credit_period_id,
            balance_version=credit_balances.balance_version+1,last_reset_at=excluded.last_reset_at,updated_at=excluded.updated_at''',
            (period.userId,period.subscriptionId,len(period.domains),quota,quota,period.start,period.end,
             period.lifecycleId,period.creditPeriodId,now,now))
        billing['coverageState']='active'
        cursor.execute('update public."Invoices" set metadata_json=%s where id=%s',(Json(metadata),invoice['id']))
        cursor.execute('''update public.subscriptions set status='active',plan_type='pro',billing_mode='monthly_prepaid',
            current_period_start=%s,current_period_end=%s,renewal_due_at=%s,subscribed_experts=%s,
            domain_count=%s,pending_removals=%s,auto_renew_enabled=false,version=version+1,updated_at=%s where id=%s''',
            (period.start,period.end,period.end,Json(list(period.domains)),len(period.domains),Json([]),now,subscription['id']))
        subscription.update(current_period_start=period.start,current_period_end=period.end,
            renewal_due_at=period.end,billing_mode='monthly_prepaid',status='active',plan_type='pro',
            subscribed_experts=list(period.domains),domain_count=len(period.domains),pending_removals=[],
            auto_renew_enabled=False,version=int(subscription.get('version') or 0)+1)
        invoice['metadata_json']=metadata
        return self._result(cursor,invoice,subscription,'activated',attemptId,refilled=True,now=now)

    def _finalizeTopupCapture(self,cursor,invoice,subscription,attempt,evidence,captureId):
        tokens=int(self._json(attempt['metadata_json'])['manualBilling'].get('tokens') or 0)
        invoiceTokens=int(self._json(invoice.get('metadata_json')).get('tokens') or 0)
        if invoice.get('billing_reason')!='add_on' or tokens<=0 or tokens!=invoiceTokens:
            raise ValueError('TOPUP_SNAPSHOT_MISMATCH')
        cursor.execute('select user_id from public.credit_balances where user_id=%s for update',(evidence.userId,))
        if cursor.fetchone() is None: raise ValueError('TOPUP_BALANCE_MISSING')
        cursor.execute('update public.credit_balances set topup_tokens=topup_tokens+%s,balance_version=balance_version+1,updated_at=%s where user_id=%s',
            (tokens,evidence.observedAt,evidence.userId))
        cursor.execute('update public."Invoices" set status=\'PAID\',"razorpayPaymentId"=%s,"paidAt"=%s where id=%s',
            (evidence.providerPaymentId,evidence.observedAt,invoice['id']))
        cursor.execute("update public.billing_events set payment_status='captured',event_status='captured',completed_at=%s where id=%s",
            (evidence.observedAt,attempt['id']))
        cursor.execute("update public.billing_events set event_status='FINALIZED' where id=%s",(captureId,))
        invoice['status']='PAID'
        self._recordNotification(cursor,subscription,'payment_receipt','receipt:'+evidence.providerPaymentId,
            {'paymentId':evidence.providerPaymentId,'invoiceId':invoice['id'],
                'amount':evidence.amount,'currency':evidence.currency,'purpose':'topup','tokens':tokens,
                'paidAccessGranted':False})
        return self._result(cursor,invoice,subscription,'topup_granted',evidence.attemptId)

    def _finalizeExpertCapture(self,cursor,invoice,subscription,attempt,evidence,captureId,metadata):
        from api.services.credits.creditConfig import getTokenQuotaForPlan
        current=subscription.get('subscribed_experts') or []
        current=json.loads(current) if isinstance(current,str) else list(current)
        if self._usesPurchaseAuthority():
            coverage = self._coverageSnapshotLocked(cursor, subscription, evidence.observedAt, materialize=False)
            current = list(coverage.currentPeriod.domains)
            from api.services.credits.manualCreditRepository import ManualCreditRepository
            ManualCreditRepository(self).ensureCurrentAllocation(cursor, coverage.currentPeriod, evidence.observedAt)
        added=self._json(attempt.get('metadata_json'))['manualBilling']['domains']
        combined=list(dict.fromkeys(current+added))
        if len(combined)>4 or len(combined)!=len(current)+len(added):
            cursor.execute("update public.billing_events set event_status='REQUIRES_RECONCILIATION',failure_reason='EXPERT_LIMIT_OR_ALREADY_PRESENT' where id=%s",(captureId,))
            return self._result(cursor,invoice,subscription,'requires_reconciliation',evidence.attemptId,anomalyId=captureId)
        cursor.execute('select * from public.credit_balances where user_id=%s for update',(evidence.userId,))
        credit=cursor.fetchone()
        if not credit or not credit.get('credit_period_id'): raise ValueError('CREDIT_PERIOD_MISSING')
        if self._usesPurchaseAuthority() and (str(credit.get('subscription_id')) != str(subscription['id'])
                or str(credit.get('lifecycle_id')) != coverage.lifecycleId
                or not _utc(credit.get('period_start')) <= evidence.observedAt < _utc(credit.get('period_end'))):
            raise ValueError('CREDIT_PERIOD_MISMATCH')
        pending=subscription.get('pending_additions') or []
        pending=json.loads(pending) if isinstance(pending,str) else list(pending)
        for item in pending:
            if item.get('orderId') == evidence.providerOrderId: item['state']='activated'
        if self._usesPurchaseAuthority():
            from api.services.credits.creditConfig import getAllowanceQuota
            quota = getAllowanceQuota('paid', len(combined))
            delta = quota - getAllowanceQuota('paid', len(current))
        else:
            quota=getTokenQuotaForPlan('annual' if subscription.get('billing_mode') == 'annual_prepaid' else 'pro',len(combined))
            delta = quota-int(credit['monthly_token_quota'])
        remaining=max(0,int(credit['remaining_tokens'])+delta)
        cursor.execute('''update public.credit_balances set monthly_token_quota=%s,remaining_tokens=%s,
            domain_count=%s,balance_version=balance_version+1,updated_at=%s where user_id=%s''',
            (quota,remaining,len(combined),evidence.observedAt,evidence.userId))
        from api.services.credits.manualCreditRepository import ManualCreditRepository
        credits=ManualCreditRepository(self)
        credits.reviseAllocation(cursor,evidence.userId,str(credit['credit_period_id']),
            remaining+credits._monthlySettled(cursor,evidence.userId,str(credit['credit_period_id'])))
        cursor.execute('update public.subscriptions set subscribed_experts=%s,domain_count=%s,pending_additions=%s,version=version+1 where id=%s',
            (Json(combined),len(combined),Json(pending),subscription['id']))
        billing=metadata.setdefault('manualBilling',{})
        billing.update(coverageState='active',creditPeriodId=str(credit['credit_period_id']),providerPaymentId=evidence.providerPaymentId)
        cursor.execute('update public."Invoices" set status=\'PAID\',"razorpayPaymentId"=%s,"paidAt"=%s,metadata_json=%s where id=%s',
            (evidence.providerPaymentId,evidence.observedAt,Json(metadata),invoice['id']))
        invoice.update(status='PAID',metadata_json=metadata)
        cursor.execute("update public.billing_events set payment_status='captured',event_status='captured',completed_at=%s where id=%s",(evidence.observedAt,attempt['id']))
        cursor.execute("update public.billing_events set event_status='FINALIZED' where id=%s",(captureId,))
        if self._usesPurchaseAuthority():
            self._modelRepository().recordExpertGrant(cursor, subscription, invoice, attempt, evidence, coverage.currentPeriod.invoiceId)
            model = self._modelRepository()
            selected = model.loadNextSelection(cursor, str(subscription['id']), coverage.lifecycleId, coverage.currentPeriod.end)
            if selected and not selected['paid']:
                currentRow = self._canonical(cursor, subscription['user_id'])
                model.saveNextSelection(cursor, str(subscription['id']), coverage.lifecycleId, coverage.currentPeriod.end,
                    currentRow['version'], list(dict.fromkeys(selected['experts'] + added)), now=evidence.observedAt)
        # The next unpaid price must be re-frozen using the updated experts.
        # Paid future invoices remain immutable.
        cursor.execute('''select * from public."Invoices" where "userId"=%s and subscription_id=%s
            and billing_reason='renewal' and status in ('UPCOMING','PAYMENT_PENDING') order by id for update''',(evidence.userId,subscription['id']))
        for unpaid in cursor.fetchall():
            self._closeInvoice(cursor, unpaid, 'EXPERT_SELECTION_CHANGED')
            if subscription.get('billing_mode') == 'annual_prepaid':
                # Annual preparation reuses EXPIRED estimates for repricing.
                old = self._json(unpaid.get('metadata_json'))
                old.update(repricingRequired=True, repricingReason='expert_selection_changed')
                cursor.execute('update public."Invoices" set status=\'EXPIRED\',metadata_json=%s where id=%s', (Json(old), unpaid['id']))
        self._recordNotification(cursor,subscription,'payment_receipt','receipt:'+evidence.providerPaymentId,
            {'paymentId':evidence.providerPaymentId,'invoiceId':invoice['id'],'amount':evidence.amount,'currency':evidence.currency})
        return self._result(cursor,invoice,subscription,'expert_activated',evidence.attemptId)

    def _recordNotification(self, cursor, subscription, notificationType, dedupeKey, metadata):
        metadata = dict(metadata)
        if self._usesPurchaseAuthority():
            metadata.update(lifecycleId=str(subscription.get('lifecycle_id') or ''),
                cancellationRequestedAt=_isoText(subscription.get('cancellation_requested_at')))
        invoiceId = metadata.get('invoiceId')
        if invoiceId:
            cursor.execute('select * from public."Invoices" where id=%s and "userId"=%s',(invoiceId,subscription['user_id']))
            invoice = cursor.fetchone()
            if invoice:
                billing = self._json(invoice.get('metadata_json')).get('manualBilling',{})
                metadata.update(amount=int(invoice.get('total_amount') or invoice.get('amount') or 0),
                    currency=invoice.get('currency'), domains=billing.get('domains',[]),
                    purpose=billing.get('purpose') or invoice.get('billing_reason'),
                    periodStart=str(invoice.get('period_start') or ''),serviceEnd=str(invoice.get('period_end') or ''),
                    serviceRevoked=bool(billing.get('revokedAt')))
                if self._usesPurchaseAuthority():
                    metadata.update(invoiceRevision=billing.get('revision'),
                        billingInterval='year' if billing.get('billingMode')=='annual_prepaid' else 'month')
                if notificationType in ('monthly_renewal_ready','monthly_renewal_reminder'):
                    metadata.update(nextStart=str(invoice.get('period_start') or ''),nextEnd=str(invoice.get('period_end') or ''),
                        renewalDeadline=str(subscription.get('current_period_end') or ''))
        current = self._canonical(cursor,subscription['user_id'])
        snapshot = self._coverageSnapshotLocked(cursor,current,_now(),materialize=False)
        if self._usesPurchaseAuthority():
            period = snapshot.currentPeriod or snapshot.nextPeriod
            if period:
                metadata['billingInterval'] = 'year' if period.billingMode=='annual_prepaid' else 'month'
            if snapshot.currentPeriod and notificationType in ('annual_renewal_upcoming','monthly_renewal_ready','monthly_renewal_reminder'):
                metadata['renewalDeadline'] = snapshot.currentPeriod.end.isoformat()
        if snapshot.currentPeriod:
            metadata.update(currentStart=snapshot.currentPeriod.start.isoformat(),currentEnd=snapshot.currentPeriod.end.isoformat())
        if snapshot.nextPeriod:
            metadata.update(nextStart=snapshot.nextPeriod.start.isoformat(),nextEnd=snapshot.nextPeriod.end.isoformat())
        if snapshot.finalPaidEnd:
            metadata.setdefault('finalPaidEnd',snapshot.finalPaidEnd.isoformat())
        intent = {'userId':subscription['user_id'], 'subscriptionId':str(subscription['id']),
            'notificationType':notificationType, 'dedupeKey':dedupeKey,
            'periodEnd':str(metadata.get('periodEnd') or subscription.get('current_period_end') or ''), 'metadata':metadata}
        cursor.execute('''insert into public.billing_events(id,user_id,subscription_id,event_category,
            event_type,event_status,idempotency_key,metadata_json,occurred_at)
            values(%s,%s,%s,'notification','email.billing_intent.committed','COMMITTED',%s,%s,%s)
            on conflict(idempotency_key) where idempotency_key is not null do nothing''',
            (str(uuid.uuid4()),subscription['user_id'],subscription['id'],'notification:'+dedupeKey,Json(intent),_now()))
        cursor.execute('select id,metadata_json from public.billing_events where idempotency_key=%s for update',('notification:'+dedupeKey,))
        existing = cursor.fetchone()
        old = self._json(existing['metadata_json'])
        if {key:value for key,value in old.items() if key != 'payloadVersion'} != intent:
            intent['payloadVersion'] = int(old.get('payloadVersion',1))+1
            cursor.execute("update public.billing_events set metadata_json=%s,event_status='COMMITTED' where id=%s",(Json(intent),existing['id']))

    def _refreshCancellationFactsLocked(self, cursor, subscription, finalEnd):
        cursor.execute("select id,metadata_json from public.billing_events where user_id=%s and event_type='email.billing_intent.committed' and event_status in ('COMMITTED','BRIDGED') order by id for update",(subscription['user_id'],))
        for row in cursor.fetchall():
            intent = self._json(row['metadata_json'])
            if intent.get('notificationType') == 'monthly_cancellation_confirmation':
                intent['periodEnd'] = finalEnd.isoformat()
                intent['metadata'].update(finalPaidEnd=finalEnd.isoformat(), periodEnd=finalEnd.isoformat())
                intent['payloadVersion'] = int(intent.get('payloadVersion',1))+1
                cursor.execute("update public.billing_events set metadata_json=%s,event_status='COMMITTED' where id=%s", (Json(intent),row['id']))

    def bridgeNotificationIntents(self, limit=100):
        def load(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("select id,metadata_json from public.billing_events where event_type='email.billing_intent.committed' and event_status='COMMITTED' and user_id is not null order by occurred_at,id limit %s",(limit,))
                return cursor.fetchall()
        rows = self._run(load)
        from api.services.notifications.billingNotificationService import enqueueBillingIntent
        for row in rows:
            intent = self._json(row['metadata_json'])
            if intent.get('metadata',{}).get('holdForRenewalEvidence'):
                def unresolved(connection):
                    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                        cursor.execute("select payment_status,metadata_json from public.billing_events where user_id=%s and event_category='payment_attempt'",(intent['userId'],))
                        for attempt in cursor.fetchall():
                            frozen = self._json(attempt['metadata_json']).get('manualBilling',{})
                            if (frozen.get('closedReason') == 'RENEWAL_DECLINED' and attempt['payment_status'] != 'captured'
                                    and not frozen.get('closureReconciledAt')):
                                return True
                        return False
                if self._run(unresolved):
                    continue
            enqueueBillingIntent(intent)
            def mark(connection):
                with connection.cursor() as cursor:
                    cursor.execute("update public.billing_events set event_status='BRIDGED' where id=%s and metadata_json=%s",(row['id'],Json(intent)))
            self._run(mark)
        return len(rows)

    # -- renewal opt-out ------------------------------------------------------

    def _renewalPreferenceFactsLocked(self, cursor, subscription, finalEnd):
        from api.services.billing.manualBillingPresentation import serializePeriod
        snapshot = self._coverageSnapshotLocked(cursor, subscription, _now(), materialize=False)
        cursor.execute('''select id from public."Invoices" where "userId"=%s and subscription_id=%s
            and billing_reason='renewal' and status in ('UPCOMING','PAYMENT_PENDING') order by id limit 1''',
            (subscription['user_id'], subscription['id']))
        payable = cursor.fetchone()
        optOut = bool(subscription.get('renewal_opt_out'))
        facts = {'renewalOptOut':optOut,'effectiveAt':finalEnd.isoformat(),
            'currentPeriod':serializePeriod(snapshot.currentPeriod),'nextPeriod':serializePeriod(snapshot.nextPeriod),
            'invoiceId':snapshot.nextPeriod.invoiceId if snapshot.nextPeriod else str(payable['id']) if payable else None,
            'state':'cancelled' if optOut else 'activated','refundInitiated':False,
            'cancellationReason':subscription.get('cancellation_reason'),
            'cancellationRequestedAt':(_isoText(subscription.get('cancellation_requested_at')) if self._usesPurchaseAuthority() else
                self._json(subscription.get('billing_state')).get('manualBilling',{}).get('cancellationRequestedAt'))}
        if self._usesPurchaseAuthority():
            from api.services.subscriptions.subscriptionModel import evaluateSubscription
            from api.services.billing.manualBillingPresentation import serializeSubscriptionSnapshot
            model = self._modelRepository()
            evaluated = model._now(cursor)
            verified = evaluateSubscription(model.loadRuntimeFacts(cursor,subscription,evaluated),evaluated)
            facts['subscription'] = serializeSubscriptionSnapshot(verified)
        return {**subscription,'finalPaidEnd':finalEnd.isoformat(),'preferenceResult':facts}

    def setRenewalOptOut(
        self,
        userId: str,
        optOut: bool,
        reason: str | None,
        requestKey: str,
    ) -> dict:
        if reason is not None:
            if not isinstance(reason,str) or len(reason.strip()) > 1000:
                raise ValueError('INVALID_CANCELLATION_REASON')
            reason = reason.strip() or None
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                previous = self._canonical(cursor, userId)
                state = self._json(previous.get('billing_state')).get('manualBilling', {})
                finalEnd = max(filter(None, [_utc(previous.get('current_period_end')), _utc(state.get('paidFutureEnd'))]), default=None)
                if self._usesPurchaseAuthority():
                    coverage = self._coverageSnapshotLocked(cursor, previous, _now(), materialize=False)
                    finalEnd = coverage.finalPaidEnd if coverage.accessAllowed else None
                if finalEnd is None or finalEnd <= _now(): raise ValueError('NO_PAID_ACCESS_TO_CANCEL_OR_RESUME')
                if bool(previous.get('renewal_opt_out')) == optOut:
                    return self._renewalPreferenceFactsLocked(cursor, previous, finalEnd)
                cursor.execute(
                    """
                    update public.subscriptions
                    set renewal_opt_out = %s,
                        cancellation_reason = case
                            when %s then coalesce(cancellation_reason, %s)
                            else null
                        end,
                        auto_renew_enabled = false, version = version + 1
                    where user_id = %s and is_canonical = true
                    returning *
                    """,
                    (optOut, optOut, reason, userId),
                )
                row = cursor.fetchone()
                if row is None:
                    raise ValueError(
                        f"No canonical subscription row for user {userId}"
                    )
                billingState = self._json(row.get('billing_state'))
                if self._usesPurchaseAuthority():
                    cursor.execute('UPDATE public.subscriptions SET cancellation_requested_at=%s WHERE id=%s RETURNING cancellation_requested_at',
                        (_now() if optOut else None, row['id']))
                    row['cancellation_requested_at'] = cursor.fetchone()['cancellation_requested_at']
                preference = billingState.setdefault('manualBilling',{})
                if optOut:
                    preference['cancellationRequestedAt'] = _now().isoformat()
                else:
                    preference.pop('cancellationRequestedAt',None)
                cursor.execute('update public.subscriptions set billing_state=%s where id=%s', (Json(billingState), row['id']))
                row['billing_state'] = billingState
                if optOut:
                    cursor.execute('''select id,metadata_json from public."Invoices" where "userId"=%s
                        and subscription_id=%s and billing_reason='renewal' and status in ('UPCOMING','PAYMENT_PENDING') order by id for update''',(userId,row['id']))
                    for invoice in cursor.fetchall():
                        metadata = self._json(invoice.get('metadata_json'))
                        metadata.setdefault('manualBilling',{}).update(closedAt=_now().isoformat(),closedReason='RENEWAL_DECLINED')
                        cursor.execute('update public."Invoices" set status=\'VOID\',metadata_json=%s where id=%s',(Json(metadata),invoice['id']))
                        cursor.execute("select id,metadata_json from public.billing_events where invoice_id=%s and event_category='payment_attempt' and payment_status in ('created','pending_provider_ack','authorized') order by id for update",(invoice['id'],))
                        for attempt in cursor.fetchall():
                            attemptMetadata = self._json(attempt['metadata_json'])
                            attemptMetadata.setdefault('manualBilling',{}).update(closedAt=_now().isoformat(),closedReason='RENEWAL_DECLINED')
                            cursor.execute("update public.billing_events set payment_status='cancelled',event_status='cancelled',metadata_json=%s where id=%s",(Json(attemptMetadata),attempt['id']))
                    self._recordNotification(cursor,row,'monthly_cancellation_confirmation','cancel:'+str(row['id'])+':'+str(previous.get('version',0)),
                        {'finalPaidEnd':finalEnd.isoformat(),'periodEnd':finalEnd.isoformat(),'holdForRenewalEvidence':True})
                return self._renewalPreferenceFactsLocked(cursor, row, finalEnd)

        return self._run(operation)

    # -- staff refunds --------------------------------------------------------

    def findRefundQuote(self, quoteId):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('select metadata_json from public.billing_events where idempotency_key=%s',('refund-quote:'+quoteId,))
                row = cursor.fetchone()
                return self._json(row['metadata_json']) if row else None
        return self._run(operation)

    def findRefundReservation(self,userId,requestKey,quoteId,expectedAmount,caseReference=None,reason=None):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('select metadata_json from public.billing_events where idempotency_key=%s',
                    ('refund-reserve:'+userId+':'+requestKey,))
                row=cursor.fetchone()
                if not row: return None
                metadata=self._json(row['metadata_json'])
                if (metadata['quoteId'] != quoteId or int(metadata['amount']) != int(expectedAmount)
                        or (caseReference is not None and metadata.get('caseReference') != caseReference)
                        or (reason is not None and metadata.get('reason') != reason)):
                    raise ValueError('REFUND_IDEMPOTENCY_CONFLICT')
                return self._refundIntent(metadata)
        return self._run(operation)

    def saveRefundQuote(self, staffId: str, quote: RefundQuote, reason: str) -> RefundQuote:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, quote.userId)
                cursor.execute(
                    """
                    insert into public.billing_events (
                        user_id, event_category, event_type, event_status,
                        amount, currency, idempotency_key, metadata_json,
                        occurred_at
                    )
                    values (
                        %s, 'reconciliation', 'refund.quote', 'QUOTED',
                        %s, %s, %s, %s, now()
                    )
                    """,
                    (
                        quote.userId,
                        quote.amount,
                        quote.currency,
                        f"refund-quote:{quote.quoteId}",
                        Json(
                            {
                                "staffId": staffId,
                                "userId": quote.userId,
                                "amount": quote.amount,
                                "currency": quote.currency,
                                "reason": reason,
                                "caseReference": quote.caseReference,
                                "quoteId": quote.quoteId,
                                "cutoff": quote.cutoff.isoformat(),
                                "expiresAt": quote.expiresAt.isoformat(),
                                "items": list(quote.items),
                                "accessExpired": quote.accessExpired,
                                "currentAccessPreserved": quote.currentAccessPreserved,
                            }
                        ),
                    ),
                )
            return quote

        return self._run(operation)

    def reserveUnusedTimeRefund(
        self,
        quoteId: str,
        staffId: str,
        caseReference: str,
        reason: str,
        expectedAmount: int,
        requestKey: str,
    ) -> RefundIntent:
        if (not all((quoteId,staffId,caseReference.strip(),reason.strip(),requestKey.strip()))
                or len(caseReference.strip()) > 200 or len(reason.strip()) > 2000 or len(requestKey.strip()) > 128
                or isinstance(expectedAmount,bool) or int(expectedAmount) < 0):
            raise ValueError('REFUND_APPROVAL_REQUIRED')
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('select * from public.billing_events where idempotency_key=%s',('refund-quote:'+quoteId,))
                row=cursor.fetchone()
                if not row: raise ValueError('REFUND_QUOTE_MISSING')
                userId=row['user_id']
                self._lockUser(cursor,userId)
                subscription=self._canonical(cursor,userId)
                if subscription.get('erasure_pending'): raise ValueError('ERASURE_PENDING')
                quote=self._json(row['metadata_json'])
                key='refund-reserve:'+userId+':'+requestKey
                cursor.execute('select metadata_json from public.billing_events where idempotency_key=%s',(key,))
                existing=cursor.fetchone()
                if existing:
                    metadata=self._json(existing['metadata_json'])
                    if (metadata.get('quoteId') != quoteId or metadata.get('caseReference') != caseReference
                            or metadata.get('reason') != reason): raise ValueError('REFUND_IDEMPOTENCY_CONFLICT')
                    if int(metadata['amount']) != int(expectedAmount): raise ValueError('REFUND_IDEMPOTENCY_CONFLICT')
                    return self._refundIntent(metadata)
                now=_now()
                expired = _utc(quote['expiresAt']) <= now
                if quote.get('caseReference') != caseReference: raise ValueError('REFUND_CASE_MISMATCH')
                cursor.execute('''select * from public."Invoices" where "userId"=%s and subscription_id=%s
                    and status='PAID' order by id for update''',(userId,subscription['id']))
                invoices=cursor.fetchall()
                selected={str(item['invoiceId']) for item in quote['items']}
                cursor.execute("select metadata_json from public.billing_events where user_id=%s and event_type='refund.intent' for update",(userId,))
                previous=[self._json(item['metadata_json']) for item in cursor.fetchall()]
                reserved={}
                for old in previous:
                    for item in old.get('items',[]):
                        reserved[item['paymentId']]=reserved.get(item['paymentId'],0)+int(item['amount'])
                items=[]
                closesCurrent=False
                for invoice in invoices:
                    if str(invoice['id']) not in selected: continue
                    if invoice.get('billing_reason') not in ('initial_purchase','renewal','proration'):
                        raise ValueError('SUBSCRIPTION_REFUND_ONLY')
                    metadata=self._json(invoice.get('metadata_json'))
                    billing=metadata.get('manualBilling',{})
                    if billing.get('billingMode') != 'monthly_prepaid' or billing.get('revokedAt'):
                        raise ValueError('REFUND_COVERAGE_NOT_OPEN')
                    start,end=_utc(invoice['period_start']),_utc(invoice['period_end'])
                    if not start or not end or end <= start: raise ValueError('INVALID_PAID_INTERVAL')
                    paymentId=invoice.get('razorpayPaymentId')
                    if not paymentId: raise ValueError('CAPTURE_IDENTITY_MISSING')
                    original=int(invoice.get('total_amount') or invoice.get('amount') or 0)
                    duration=(end-start)//timedelta(microseconds=1)
                    unused=max(0,(end-max(start,now))//timedelta(microseconds=1))
                    amount=original*unused//duration
                    if amount <= 0: continue
                    if reserved.get(paymentId,0)+amount > original: raise ValueError('REFUND_FUNDS_ALREADY_RESERVED')
                    current=start <= now < end
                    closesCurrent=closesCurrent or current
                    items.append({'invoiceId':str(invoice['id']),'paymentId':paymentId,'amount':amount,
                        'originalAmount':original,'currency':invoice['currency'],'intervalStart':start.isoformat(),
                        'intervalEnd':end.isoformat(),'kind':'current' if current else 'future'})
                if len(items) != len(selected) or not items: raise ValueError('REFUND_SELECTION_STALE')
                if len({item['currency'] for item in items}) != 1: raise ValueError('REFUND_CURRENCY_MISMATCH')
                if closesCurrent:
                    unpaidSelection=[row for row in invoices if _utc(row['period_end']) and _utc(row['period_end']) > now and
                        self._json(row.get('metadata_json')).get('manualBilling',{}).get('coverageState') in ('scheduled','active') and str(row['id']) not in selected]
                    if unpaidSelection: raise ValueError('CURRENT_TERMINATION_REQUIRES_FUTURE_SETTLEMENT')
                amount=sum(item['amount'] for item in items)
                if expired or amount != int(expectedAmount):
                    from api.services.billing.manualBillingContracts import RefundQuoteConflict
                    refreshed = RefundQuote('rq_'+str(uuid.uuid4()),userId,caseReference,items[0]['currency'],
                        now,now+timedelta(minutes=5),amount,tuple(items),closesCurrent,not closesCurrent)
                    raise RefundQuoteConflict('REFUND_QUOTE_EXPIRED' if expired else 'REFUND_AMOUNT_CHANGED',refreshed)
                intentId=str(uuid.uuid4())
                metadata={'refundIntentId':intentId,'userId':userId,'quoteId':quoteId,'staffId':staffId,
                    'caseReference':caseReference,'reason':reason,'cutoff':now.isoformat(),'amount':amount,
                    'items':items,'accessExpired':closesCurrent,'currentAccessPreserved':not closesCurrent,
                    'refundState':'reserved','providerRefunds':{},'submissions':{}}
                for invoice in invoices:
                    if str(invoice['id']) not in selected: continue
                    data=self._json(invoice.get('metadata_json'))
                    data.setdefault('manualBilling',{}).update(revokedAt=now.isoformat(),coverageState='revoked',refundIntentId=intentId)
                    cursor.execute('update public."Invoices" set metadata_json=%s where id=%s',(Json(data),invoice['id']))
                cursor.execute('''insert into public.billing_events(id,user_id,subscription_id,event_category,event_type,event_status,
                    amount,currency,idempotency_key,metadata_json,occurred_at) values(%s,%s,%s,'reconciliation','refund.intent','reserved',%s,%s,%s,%s,%s)''',
                    (intentId,userId,subscription['id'],amount,items[0]['currency'],key,Json(metadata),now))
                cursor.execute('update public.subscriptions set renewal_opt_out=true,auto_renew_enabled=false where id=%s',(subscription['id'],))
                state=self._json(subscription.get('billing_state'))
                remainingEnds=[_utc(row['period_end']) for row in invoices if str(row['id']) not in selected and
                    self._json(row.get('metadata_json')).get('manualBilling',{}).get('coverageState') != 'revoked']
                finalEnd=now if closesCurrent else max(filter(None,remainingEnds),default=_utc(subscription.get('current_period_end')))
                state.setdefault('manualBilling',{}).update(paidFutureEnd=finalEnd.isoformat(),finalPaidEnd=finalEnd.isoformat())
                cursor.execute('update public.subscriptions set billing_state=%s where id=%s',(Json(state),subscription['id']))
                cursor.execute('''update public."Invoices" set status='VOID' where "userId"=%s and subscription_id=%s
                    and status in ('UPCOMING','PAYMENT_PENDING') and billing_reason in ('renewal','proration')''',(userId,subscription['id']))
                cursor.execute("update public.billing_events set payment_status='cancelled',event_status='cancelled' where user_id=%s and event_category='payment_attempt' and payment_status in ('created','pending_provider_ack','authorized')",(userId,))
                if closesCurrent:
                    cursor.execute("update public.subscriptions set status='expired',plan_type='none',current_period_end=%s,subscribed_experts=%s,domain_count=0 where id=%s",(now,Json([]),subscription['id']))
                    cursor.execute('update public.credit_balances set remaining_tokens=0,monthly_token_quota=0,balance_version=balance_version+1,updated_at=%s where user_id=%s',(now,userId))
                self._recordNotification(cursor,subscription,'subscription_refund_initiated','refund:'+intentId+':initiated',{'refundIntentId':intentId,'amount':amount,'currency':items[0]['currency'],'accessExpired':closesCurrent,'currentAccessPreserved':not closesCurrent,'cutoff':now.isoformat(),'finalPaidEnd':finalEnd.isoformat()})
                return self._refundIntent(metadata)
        return self._run(operation)

    def _refundIntent(self, metadata):
        return RefundIntent(metadata['refundIntentId'],metadata['userId'],metadata['refundState'],
            _utc(metadata['cutoff']),int(metadata['amount']),tuple(metadata['items']),bool(metadata['accessExpired']),
            bool(metadata['currentAccessPreserved']),False)

    def claimRefundSubmission(self, intentId, paymentId):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("select user_id from public.billing_events where id=%s and event_type='refund.intent'",(intentId,))
                row=cursor.fetchone()
                if not row: raise ValueError('REFUND_INTENT_MISSING')
                self._lockUser(cursor,row['user_id'])
                cursor.execute('select metadata_json from public.billing_events where id=%s for update',(intentId,))
                metadata=self._json(cursor.fetchone()['metadata_json'])
                if paymentId not in {item['paymentId'] for item in metadata['items']}:
                    raise ValueError('REFUND_PAYMENT_MISMATCH')
                if paymentId in metadata['submissions']: return False
                metadata['submissions'][paymentId]='unknown'
                metadata['refundState']='unknown'
                cursor.execute("update public.billing_events set metadata_json=%s,event_status='unknown' where id=%s",(Json(metadata),intentId))
                return True
        return self._run(operation)

    def settleRefundEvidence(self, refundIntentId: str, providerEvidence: dict) -> dict:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("select user_id from public.billing_events where id=%s and event_type='refund.intent'",(refundIntentId,))
                row=cursor.fetchone()
                if not row: raise ValueError('REFUND_INTENT_MISSING')
                self._lockUser(cursor,row['user_id'])
                row = self._refreshFinancialOwnerLocked(cursor,refundIntentId,row)
                cursor.execute('select metadata_json from public.billing_events where id=%s for update',(refundIntentId,))
                metadata=self._json(cursor.fetchone()['metadata_json'])
                expected={item['paymentId']:item for item in metadata['items']}
                for refund in providerEvidence.get('refunds',[]):
                    item=expected.get(refund.get('payment_id'))
                    if not item or int(refund.get('amount') or 0) != item['amount'] or not refund.get('id'):
                        raise ValueError('REFUND_EVIDENCE_MISMATCH')
                    previous=metadata['providerRefunds'].get(item['paymentId'])
                    if previous and previous.get('id') != refund['id']: raise ValueError('EXCESS_REFUND_IDENTITY')
                    if previous and previous.get('status') == 'processed': continue
                    metadata['providerRefunds'][item['paymentId']]=(
                        {key:refund[key] for key in ('id','payment_id','amount','currency','status') if key in refund}
                        if row['user_id'] is None else refund)
                refunds=list(metadata['providerRefunds'].values())
                state='processed' if len(refunds)==len(expected) and all(r['status']=='processed' for r in refunds) else 'failed' if any(r['status']=='failed' for r in refunds) else 'pending' if refunds else metadata['refundState']
                metadata['refundState']=state
                cursor.execute('update public.billing_events set metadata_json=%s,event_status=%s where id=%s',(Json(metadata),state,refundIntentId))
                if state == 'processed' and row['user_id'] is not None:
                    subscription=self._canonical(cursor,row['user_id'])
                    self._recordNotification(cursor,subscription,'subscription_refund_processed','refund:'+refundIntentId+':processed',{'refundIntentId':refundIntentId,'amount':metadata['amount'],'currency':metadata['items'][0]['currency'],'accessExpired':metadata['accessExpired'],'currentAccessPreserved':metadata['currentAccessPreserved'],'cutoff':metadata['cutoff']})
                return {'refundIntentId':refundIntentId,'refundState':state,'accessRestored':False}
        return self._run(operation)


_repository: ManualBillingRepository | None = None


def getManualBillingRepository() -> ManualBillingRepository:
    global _repository
    if _repository is None:
        _repository = ManualBillingRepository()
    return _repository
