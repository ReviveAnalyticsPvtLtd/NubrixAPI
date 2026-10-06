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

from api.services.billing.manualBillingContracts import (
    CheckoutIntent,
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


def _payloadHash(payload: dict) -> str:
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _advisoryKey(userId: str) -> int:
    digest = hashlib.sha256(("manual-billing:" + str(userId)).encode("utf-8"))
    return int(digest.hexdigest()[:15], 16)


class ManualBillingRepository:
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
                    ((event_category='payment_attempt'
                      and payment_status <> 'captured') or (event_type='refund.intent' and event_status <> 'processed'))
                    and coalesce((metadata_json->>'lastRecoveryAt')::timestamptz,created_at) < now()-interval '15 minutes'
                    order by coalesce((metadata_json->>'lastRecoveryAt')::timestamptz,created_at),id limit %s''',(limit,))
                return list(cursor.fetchall())
        return self._run(operation)

    def recordRecoveryCheck(self, eventId):
        def operation(connection):
            with connection.cursor() as cursor:
                cursor.execute("update public.billing_events set metadata_json=coalesce(metadata_json,'{}'::jsonb) || %s where id=%s",
                    (Json({'lastRecoveryAt':_now().isoformat()}),eventId))
        return self._run(operation)

    def markClosedAttemptReconciled(self, attemptId):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('select user_id from public.billing_events where id=%s',(attemptId,))
                owner = cursor.fetchone()
                if not owner:
                    raise ValueError('ATTEMPT_MISSING')
                self._lockUser(cursor,owner['user_id'])
                if owner['user_id'] is not None:
                    self._canonical(cursor,owner['user_id'])
                cursor.execute('select metadata_json from public.billing_events where id=%s for update',(attemptId,))
                row = cursor.fetchone()
                metadata = self._json(row['metadata_json'])
                if metadata.get('manualBilling',{}).get('closedAt'):
                    metadata['manualBilling']['closureReconciledAt'] = _now().isoformat()
                    cursor.execute('update public.billing_events set metadata_json=%s where id=%s',(Json(metadata),attemptId))
        return self._run(operation)

    def ensureCanonicalSubscription(self, userId: str) -> dict:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                cursor.execute(
                    """
                    select id, user_id, billing_mode, status, plan_type,
                           current_period_start, current_period_end,
                           renewal_due_at, auto_renew_enabled,
                           payment_collection_mode, default_currency,
                           version, erasure_pending, is_canonical
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
                return dict(created)

        return self._run(operation)

    def activateTrial(self, userId: str, domains: tuple[str, ...]) -> dict:
        """Consume the existing twelve-day trial once, under the owner lock."""
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, userId)
                subscription = self._canonical(cursor, userId)
                cursor.execute('select now() as current_time')
                now = _utc(cursor.fetchone()['current_time'])
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

    def reserveCheckout(self, request: CheckoutRequest) -> CheckoutIntent:
        """Freeze server pricing, invoice and attempt in one owner transaction.

        Reference-plan transport runs outside locks. A replay lookup precedes
        that fetch, so retries remain possible during a provider outage.
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
        payload_hash = _payloadHash(identity)

        def lookup(cursor, now):
            cursor.execute('''select * from public.billing_events where user_id=%s
                and event_category='payment_attempt' order by id''', (request.userId,))
            for row in cursor.fetchall():
                metadata = self._json(row.get('metadata_json'))
                frozen = metadata.get('manualBilling', {})
                if frozen.get('purpose') != request.purpose or frozen.get('billingMode') != request.billingMode:
                    continue
                exact = (request.requestKey is not None and row.get('idempotency_key') ==
                    f'{request.purpose}:{request.userId}:{request.requestKey}')
                expiry = _utc(frozen.get('expiresAt'))
                live = (row.get('payment_status') in ('created', 'pending_provider_ack', 'authorized')
                        and (expiry is not None and expiry > now or
                             row.get('payment_status') == 'pending_provider_ack' and not row.get('provider_order_id')))
                if exact:
                    if frozen.get('payloadHash') != payload_hash:
                        raise ValueError('IDEMPOTENCY_CONFLICT')
                    return self._intentFromAttemptRow(row, metadata, request.userId, request.purpose)
                if request.requestKey is None and live and frozen.get('payloadHash') == payload_hash:
                    return self._intentFromAttemptRow(row, metadata, request.userId, request.purpose)
                if request.purpose == 'initial_purchase' and live:
                    raise ValueError('LIVE_INITIAL_CHECKOUT_CONFLICT')
            return None

        def transaction(connection, reference=None, replayOnly=False):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor, request.userId)
                subscription = self._canonical(cursor, request.userId)
                cursor.execute('select now() as current_time')
                now = _utc(cursor.fetchone()['current_time'])
                existing = lookup(cursor, now)
                if existing is not None or replayOnly:
                    return existing
                coverage = self._coverageSnapshotLocked(cursor, subscription, now, materialize=True)
                if subscription.get('erasure_pending') or coverage.denialReason == 'account_banned':
                    raise ValueError('CHECKOUT_OWNER_NOT_ELIGIBLE')
                if request.purpose == 'initial_purchase':
                    if coverage.currentPeriod or coverage.nextPeriod or (
                            subscription.get('billing_mode') == 'annual_prepaid'
                            and _utc(subscription.get('current_period_end')) and _utc(subscription['current_period_end']) > now):
                        raise ValueError('EXISTING_PAID_COVERAGE')
                elif request.billingMode == 'monthly_prepaid':
                    if not coverage.accessAllowed:
                        raise ValueError('PAID_COVERAGE_REQUIRED')
                elif request.purpose in ('expert_addition', 'topup'):
                    from api.services.subscriptions.paymentValidationService import isAccessActive
                    if not isAccessActive(subscription, now=now):
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
                    if subscription.get('renewal_opt_out') or not end or end <= now:
                        raise ValueError('RENEWAL_NOT_ELIGIBLE')
                    invoiceId = request.payload.get('invoiceId')
                    cursor.execute('select * from public."Invoices" where id=%s and "userId"=%s for update', (invoiceId, request.userId))
                    invoice = cursor.fetchone()
                    if not invoice or str(invoice['subscription_id']) != str(subscription['id']):
                        raise ValueError('OWNED_INVOICE_NOT_FOUND')
                    frozen = self._json(invoice.get('metadata_json')).get('manualBilling', {})
                    if invoice.get('billing_reason') != 'renewal' or invoice['status'] not in ('UPCOMING', 'PAYMENT_PENDING') or _utc(invoice.get('period_start')) != end:
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
                    # The existing annual adapter has no finite session TTL.
                    # Keep it independent of the new monthly TTL policy.
                    deadline = datetime.max.replace(tzinfo=timezone.utc)
                    if request.purpose == 'expert_addition':
                        deadline = end
                if request.billingMode == 'monthly_prepaid' and request.purpose in ('renewal', 'expert_addition'):
                    deadline = min(deadline, end)
                if invoice is None:
                    if request.purpose == 'topup':
                        pricing = billingEngine.computeTopupSnapshot(request.payload.get('packId'), request.billingMode)
                    else:
                        reason = 'proration' if request.purpose == 'expert_addition' else request.purpose
                        pricing = billingEngine.computeInvoiceSnapshot(request.billingMode, reason, len(selected),
                            periodStart=start, priceReference=reference, evaluatedAt=now,
                            prorationAnchorStart=_utc(subscription.get('current_period_start')), prorationAnchorEnd=end)
                    billing = {'schemaVersion': 1, 'lifecycleId': lifecycle, 'purpose': request.purpose,
                        'billingMode': request.billingMode, 'domains': selected, 'revision': revision,
                        'coverageState': 'estimated', 'expiresAt': deadline.isoformat()}
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
                    cursor.execute('insert into public."Invoices" (' + ','.join('"' + key + '"' for key in invoice) +
                        ') values (' + ','.join('%s' for _ in invoice) + ')',
                        [Json(value) if isinstance(value, (dict, list)) else value for value in invoice.values()])
                frozen = self._json(invoice.get('metadata_json')).get('manualBilling', {})
                snapshot = {'subscriptionId': str(subscription['id']), 'invoiceId': str(invoice['id']),
                    'lifecycleId': lifecycle, 'cycleId': str(invoice.get('period_start')), 'revision': revision,
                    'billingMode': request.billingMode, 'domains': selected, 'amount': int(invoice['total_amount']),
                    'currency': invoice['currency'], 'periodStart': invoice.get('period_start'),
                    'periodEnd': invoice.get('period_end'), 'expiresAt': deadline.isoformat(),
                    'tokens': frozen.get('tokens'), 'packId': frozen.get('packId'), 'requestKey': request.requestKey or str(uuid.uuid4())}
                return self._reserveCheckoutIntentLocked(cursor, subscription, request.userId, request.purpose,
                    snapshot['requestKey'], payload_hash, snapshot, now)
        existing = self._run(lambda connection: transaction(connection, replayOnly=True))
        if existing is not None:
            return existing
        reference = None
        if request.purpose in ('initial_purchase', 'expert_addition'):
            reference = billingEngine._getMonthlyBasePrice() if request.billingMode == 'monthly_prepaid' else billingEngine._getAnnualBasePrice()
        return self._run(lambda connection: transaction(connection, reference))

    def createFrozenRenewalInvoice(self, payload, expectedVersion):
        """Pricing is fetched outside locks; its selection version is checked here."""
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor,payload['userId'])
                subscription=self._canonical(cursor,payload['userId'])
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
                allowed={'userId','subscription_id','billing_reason','payment_flow','requires_customer_auth',
                    'period_start','period_end','amount_before_tax','tax_amount','total_amount','amount','currency','status',
                    'tax_breakdown_json','tax_rule_version','place_of_supply_snapshot','pricing_version',
                    'pricing_reference_snapshot_json','metadata_json'}
                if set(payload)-allowed: raise ValueError('UNSUPPORTED_INVOICE_FIELDS')
                row={'id':str(uuid.uuid4()),**payload}
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
                    if invoice['status'] not in ('UPCOMING','PAYMENT_PENDING'): continue
                    self._closeInvoice(cursor,invoice,'EXPERT_SELECTION_CHANGED')
                pending.extend(domains)
                cursor.execute('update public.subscriptions set pending_removals=%s,version=version+1 where id=%s',(Json(pending),subscription['id']))
                return {'currentDomains':current,'pendingRemovals':pending,'effectiveAt':end.isoformat()}
        return self._run(operation)

    def _closeInvoice(self,cursor,invoice,reason):
        metadata=self._json(invoice.get('metadata_json'))
        metadata.setdefault('manualBilling',{}).update(closedAt=_now().isoformat(),closedReason=reason)
        cursor.execute('update public."Invoices" set status=\'VOID\',metadata_json=%s where id=%s',(Json(metadata),invoice['id']))
        cursor.execute("update public.billing_events set payment_status='cancelled',event_status='cancelled' where invoice_id=%s and event_category='payment_attempt' and payment_status in ('created','pending_provider_ack','authorized')",(invoice['id'],))

    def cancelExpertAddition(self,userId,domain):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor,userId)
                subscription=self._canonical(cursor,userId)
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
                cursor.execute('select now() as current_time')
                return self._reserveCheckoutIntentLocked(cursor, subscription, userId, purpose,
                    requestKey, payloadHash, snapshot, _utc(cursor.fetchone()['current_time']))
        return self._run(operation)

    def _reserveCheckoutIntentLocked(self, cursor, subscription, userId, purpose,
                                    requestKey, payloadHash, snapshot, now):
        manualBilling = {
            "schemaVersion": 1,
            "lifecycleId": snapshot.get("lifecycleId"),
            "cycleId": snapshot.get("cycleId"),
            "revision": snapshot.get("revision", 1),
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
            if not intent.razorpayOrderId:
                # Unknown provider creation must be recovered before a new opportunity.
                return intent
            if invoice['status'] == 'PAID': return intent
            storedBilling.update(closedAt=now.isoformat(),closedReason='CHECKOUT_EXPIRED')
            cursor.execute("update public.billing_events set payment_status='expired',event_status='expired',metadata_json=%s where id=%s",
                (Json(storedMeta),existing['id']))
            revision=int(storedBilling.get('revision') or 1)+1
            namespaceKey += ':revision:'+str(revision)
            manualBilling['revision']=revision

        if invoice['status'] not in ('UPCOMING','PAYMENT_PENDING') or _utc(snapshot.get('expiresAt')) <= now:
            raise ValueError('CHECKOUT_CLOSED')
        if purpose == 'renewal' and (subscription.get('renewal_opt_out') or not _utc(subscription.get('current_period_end')) or _utc(subscription.get('current_period_end')) <= now):
            raise ValueError('RENEWAL_NOT_ELIGIBLE')
        manualBilling['expiresAt']=(_utc(snapshot['expiresAt']) if snapshot.get('billingMode') == 'annual_prepaid'
            else min(_utc(snapshot['expiresAt']),now+timedelta(seconds=int(os.environ.get('MANUAL_CHECKOUT_TTL_SECONDS','1800'))))).isoformat()
        if purpose == 'initial_purchase' and subscription.get('billing_mode') in ('monthly_prepaid','annual_prepaid') and _utc(subscription.get('current_period_end')) and _utc(subscription['current_period_end']) > now:
            raise ValueError('EXISTING_PAID_COVERAGE')
        if purpose == 'topup' and (not _utc(subscription.get('current_period_end')) or _utc(subscription['current_period_end']) <= now):
            raise ValueError('TOPUP_NOT_ELIGIBLE')
        expiredAttemptIds=set()
        cursor.execute("select id,metadata_json from public.billing_events where user_id=%s and event_category='payment_attempt' and payment_status in ('created','pending_provider_ack','authorized') order by id for update",(userId,))
        for attempt in cursor.fetchall():
            metadata=self._json(attempt.get('metadata_json'))
            billing=metadata.get('manualBilling',{})
            expires=_utc(billing.get('expiresAt'))
            if expires and expires <= now:
                expiredAttemptIds.add(str(attempt['id']))
                billing.update(closedAt=now.isoformat(),closedReason='CHECKOUT_EXPIRED')
                cursor.execute("update public.billing_events set payment_status='expired',event_status='expired',metadata_json=%s where id=%s",(Json(metadata),attempt['id']))

        subscriptionId = snapshot.get("subscriptionId")
        invoiceId = snapshot.get("invoiceId")
        attemptId = snapshot.get("attemptId") or str(uuid.uuid4())
        if purpose == 'expert_addition':
            domains=list(snapshot.get('domains') or [])
            current=subscription.get('subscribed_experts') or []
            current=json.loads(current) if isinstance(current,str) else list(current)
            pending=subscription.get('pending_additions') or []
            pending=json.loads(pending) if isinstance(pending,str) else list(pending)
            for item in pending:
                if item.get('attemptId') in expiredAttemptIds and item.get('state')=='awaiting_payment':
                    item.update(state='expired',expiredAt=now.isoformat())
            openDomains={item.get('domain') for item in pending if item.get('state')=='awaiting_payment'}
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
        return CheckoutIntent(
            attemptId=str(row["id"]),
            invoiceId=str(row.get("invoice_id") or "") or "",
            userId=str(row.get("user_id") or userId),
            lifecycleId=str(billing.get("lifecycleId") or ""),
            purpose=purpose,
            billingMode=str(billing.get("billingMode") or ""),
            payloadHash=str(billing.get("payloadHash") or ""),
            currency=str(billing.get("currency") or "INR"),
            state=str(row.get("payment_status") or "created"),
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
                cursor.execute("update public.billing_events set payment_status='pending_provider_ack',event_status='pending_provider_ack' where id=%s and payment_status='created' returning id",(attemptId,))
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

    def recordUnmappedPayment(self, payment: dict):
        """Unknown historical money remains visible without guessing its owner or grant."""
        def operation(connection):
            with connection.cursor() as cursor:
                cursor.execute('''insert into public.billing_events(id,event_category,event_type,event_status,
                    provider,amount,currency,idempotency_key,metadata_json,occurred_at)
                    values(%s,'reconciliation','payment.unmapped','REQUIRES_RECONCILIATION','razorpay',%s,%s,%s,%s,%s)
                    on conflict(idempotency_key) do nothing''', (str(uuid.uuid4()),int(payment.get('amount') or 0),
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
                if evidence.userId is None:
                    cursor.execute('select * from public.billing_events where id=%s',(evidence.attemptId,))
                    erasedAttempt=cursor.fetchone()
                    if erasedAttempt and erasedAttempt['user_id'] is None:
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
                cutoff = _utc(frozen.get('expiresAt'))
                captureAt = evidence.provenCaptureAt if evidence.timingVerified else None
                # A server observation before the deadline proves captured funds existed then.
                captureAt = captureAt or evidence.observedAt
                reason = None
                if invoice['status'] == 'PAID': reason = 'EXCESS_CAPTURE'
                elif invoice['status'] not in ('UPCOMING','PAYMENT_PENDING'): reason = 'CLOSED_INVOICE'
                elif frozen.get('closedAt'): reason = 'CLOSED_ATTEMPT'
                elif cutoff is None or captureAt >= cutoff: reason = 'CAPTURE_OUTSIDE_WINDOW'
                elif subscription.get('erasure_pending'): reason = 'ERASURE_PENDING'
                elif evidence.purpose not in ('initial_purchase','renewal','expert_addition','topup'): reason = 'UNSUPPORTED_PURPOSE'
                domains=frozen.get('domains') or []
                if evidence.purpose != 'topup' and (not 1 <= len(domains) <= 4 or len(set(domains)) != len(domains)):
                    reason='INVALID_FROZEN_EXPERT_SELECTION'
                metadata = self._json(invoice.get('metadata_json'))
                billing = metadata.setdefault('manualBilling', {})
                closed = self._json(attempt.get('metadata_json')).get('manualBilling', {})
                closedAt = _utc(closed.get('closedAt') or billing.get('closedAt'))
                closedReason = closed.get('closedReason') or billing.get('closedReason')
                if (reason in ('CLOSED_INVOICE', 'CLOSED_ATTEMPT') and evidence.purpose == 'renewal'
                        and frozen.get('billingMode') == 'monthly_prepaid'
                        and closedReason == 'RENEWAL_DECLINED' and closedAt
                        and evidence.timingVerified and evidence.provenCaptureAt
                        and evidence.provenCaptureAt < closedAt and cutoff and evidence.provenCaptureAt < cutoff
                        and not subscription.get('erasure_pending') and not billing.get('revokedAt')):
                    reason = None
                if evidence.purpose == 'renewal':
                    lifecycle = self._json(subscription.get('billing_state')).get('manualBilling', {}).get('lifecycleId')
                    if frozen.get('lifecycleId') != lifecycle: reason = 'STALE_LIFECYCLE'
                    if _utc(invoice.get('period_start')) != _utc(subscription.get('current_period_end')): reason = 'STALE_CYCLE'
                elif evidence.purpose == 'initial_purchase' and subscription.get('billing_mode') in ('monthly_prepaid','annual_prepaid') and (_utc(subscription.get('current_period_end')) or captureAt) > captureAt:
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
                cursor.execute("update public.billing_events set payment_status='captured',event_status='captured',completed_at=%s where id=%s", (evidence.observedAt,attempt['id']))
                cursor.execute("update public.billing_events set event_status='FINALIZED' where id=%s",(captureId,))
                self._recordNotification(cursor, subscription, 'payment_receipt',
                    'receipt:'+evidence.providerPaymentId, {'paymentId':evidence.providerPaymentId,'invoiceId':invoice['id'], 'amount':evidence.amount,'currency':evidence.currency,'periodEnd':end.isoformat()})
                state = self._json(subscription.get('billing_state'))
                state.setdefault('manualBilling',{}).update(lifecycleId=frozen['lifecycleId'],paidFutureEnd=end.isoformat())
                cursor.execute('update public.subscriptions set billing_state=%s where id=%s', (Json(state),subscription['id']))
                subscription['billing_state'] = state
                if frozen['billingMode'] == 'annual_prepaid':
                    return self._applyAnnualPayment(cursor, invoice, subscription, evidence.observedAt, evidence.attemptId)
                if subscription.get('renewal_opt_out'):
                    self._refreshCancellationFactsLocked(cursor, subscription, end)
                return self._applyCoverage(cursor,invoice,subscription,evidence.observedAt,evidence.attemptId)
        return self._run(operation)

    def _recordErasedCapture(self,cursor,attempt,evidence):
        """Retain received-money evidence after anonymisation; grant no access."""
        cursor.execute('select * from public."Invoices" where id=%s for update',(evidence.invoiceId,))
        invoice=cursor.fetchone()
        if (not invoice or invoice['userId'] is not None or str(attempt['invoice_id'])!=evidence.invoiceId
            or attempt.get('provider_order_id')!=evidence.providerOrderId or int(attempt['amount'])!=evidence.amount
            or attempt['currency']!=evidence.currency or evidence.financialStatus!='captured'):
            raise ValueError('ERASED_CAPTURE_EVIDENCE_MISMATCH')
        cursor.execute('select id,invoice_id from public.billing_events where provider_payment_id=%s',(evidence.providerPaymentId,))
        existing=cursor.fetchone()
        if existing and str(existing['invoice_id']) != evidence.invoiceId:
            raise ValueError('PAYMENT_ALREADY_BOUND')
        anomalyId=str(existing['id']) if existing else str(uuid.uuid4())
        if not existing:
            cursor.execute('''insert into public.billing_events(id,invoice_id,event_category,event_type,event_status,
                provider_order_id,provider_payment_id,amount,currency,idempotency_key,failure_reason,metadata_json,occurred_at)
                values(%s,%s,'reconciliation','payment.capture','REQUIRES_RECONCILIATION',%s,%s,%s,%s,%s,'ERASED_ACCOUNT',%s,%s)''',
                (anomalyId,evidence.invoiceId,evidence.providerOrderId,evidence.providerPaymentId,evidence.amount,
                 evidence.currency,'capture:'+evidence.providerPaymentId,Json({}),evidence.observedAt))
        return FinalizationResult(evidence.invoiceId,evidence.attemptId,'requires_reconciliation','unchanged',
            False,False,True,None,None,anomalyId)

    def activateDueCoverage(self, userId: str, now: datetime) -> FinalizationResult:
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                self._lockUser(cursor,userId)
                subscription = self._canonical(cursor,userId)
                return self._activateDueCoverageLocked(cursor, subscription, _utc(now))
        return self._run(operation)

    def _paidInvoicesLocked(self, cursor, subscription):
        cursor.execute('''select * from public."Invoices" where "userId"=%s and subscription_id=%s
            and status='PAID' order by id for update''', (subscription['user_id'], subscription['id']))
        return list(cursor.fetchall())

    def _activateDueCoverageLocked(self, cursor, subscription, now, invoices=None):
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
            if subscription.get('status') == 'expired':
                return self._result(cursor,{}, subscription, 'expired', None)
            cursor.execute("""update public.subscriptions set status='expired',plan_type='none',
                subscribed_experts=%s,domain_count=0,pending_removals=%s,pending_additions=%s,
                updated_at=%s where id=%s""", (Json([]), Json([]), Json([]), now, subscription['id']))
            cursor.execute('''update public.credit_balances set plan_tier='none',domain_count=0,
                monthly_token_quota=0,remaining_tokens=0,balance_version=balance_version+1,
                updated_at=%s where user_id=%s''', (now, subscription['user_id']))
            if not subscription.get('renewal_opt_out'):
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
                    cursor.execute('select now() as current_time')
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
                continue
            domains = billing.get('domains') or subscription.get('subscribed_experts') or []
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
        from api.services.credits.creditConfig import getTokenQuotaForPlan
        from dateutil.relativedelta import relativedelta
        metadata = self._json(invoice['metadata_json'])
        billing = metadata['manualBilling']
        period = self._coverage(invoice)
        quota = getTokenQuotaForPlan('annual', len(period.domains))
        cursor.execute('''insert into public.credit_balances (user_id,subscription_id,plan_tier,domain_count,
            monthly_token_quota,used_tokens,remaining_tokens,period_start,period_end,lifecycle_id,
            credit_period_id,balance_version,last_reset_at,updated_at)
            values (%s,%s,'annual',%s,%s,0,%s,%s,%s,%s,%s,1,%s,%s)
            on conflict(user_id) do update set subscription_id=excluded.subscription_id,plan_tier=excluded.plan_tier,
            domain_count=excluded.domain_count,monthly_token_quota=excluded.monthly_token_quota,used_tokens=0,
            remaining_tokens=excluded.remaining_tokens,period_start=excluded.period_start,period_end=excluded.period_end,
            lifecycle_id=excluded.lifecycle_id,credit_period_id=excluded.credit_period_id,
            balance_version=credit_balances.balance_version+1,last_reset_at=excluded.last_reset_at,updated_at=excluded.updated_at''',
            (period.userId,period.subscriptionId,len(period.domains),quota,quota,now,now+relativedelta(months=1),
             period.lifecycleId,period.creditPeriodId,now,now))
        billing['coverageState'] = 'scheduled' if period.start > now else 'active'
        billing['creditsAllocatedAt'] = now.isoformat()
        cursor.execute('update public."Invoices" set metadata_json=%s where id=%s', (Json(metadata),invoice['id']))
        cursor.execute('''update public.subscriptions set status='active',plan_type='annual',billing_mode='annual_prepaid',
            current_period_start=%s,current_period_end=%s,renewal_due_at=%s,subscribed_experts=%s,
            domain_count=%s,pending_removals=%s,auto_renew_enabled=false,version=version+1,updated_at=%s where id=%s''',
            (period.start,period.end,period.end,Json(list(period.domains)),len(period.domains),Json([]),now,subscription['id']))
        subscription.update(status='active',plan_type='annual',billing_mode='annual_prepaid',
            current_period_start=period.start,current_period_end=period.end,renewal_due_at=period.end,
            subscribed_experts=list(period.domains),domain_count=len(period.domains))
        invoice['metadata_json'] = metadata
        return self._result(cursor,invoice,subscription,'paid_scheduled' if period.start > now else 'activated',attemptId,refilled=True)

    def _result(self,cursor,invoice,subscription,state,attemptId,refilled=False,anomalyId=None,now=None):
        snapshot = self._coverageSnapshotLocked(cursor,subscription,now or _now(),materialize=False)
        cursor.execute('select credit_period_id from public.credit_balances where user_id=%s',(subscription['user_id'],))
        balance = cursor.fetchone()
        ready = balance is not None and bool(balance.get('credit_period_id'))
        return FinalizationResult(str(invoice.get('id') or ''),attemptId,state,
            'ready' if ready else 'pending_materialization',
            state in ('activated','paid_scheduled','already_finalized','elapsed','expert_activated','topup_granted'),
            refilled,snapshot.renewalOptOut,snapshot.currentPeriod if snapshot.accessAllowed else None,
            snapshot.nextPeriod,anomalyId)

    def _applyCoverage(self,cursor,invoice,subscription,now,attemptId):
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
        added=self._json(attempt.get('metadata_json'))['manualBilling']['domains']
        combined=list(dict.fromkeys(current+added))
        if len(combined)>4 or len(combined)!=len(current)+len(added):
            cursor.execute("update public.billing_events set event_status='REQUIRES_RECONCILIATION',failure_reason='EXPERT_LIMIT_OR_ALREADY_PRESENT' where id=%s",(captureId,))
            return self._result(cursor,invoice,subscription,'requires_reconciliation',evidence.attemptId,anomalyId=captureId)
        cursor.execute('select * from public.credit_balances where user_id=%s for update',(evidence.userId,))
        credit=cursor.fetchone()
        if not credit or not credit.get('credit_period_id'): raise ValueError('CREDIT_PERIOD_MISSING')
        pending=subscription.get('pending_additions') or []
        pending=json.loads(pending) if isinstance(pending,str) else list(pending)
        for item in pending:
            if item.get('orderId') == evidence.providerOrderId: item['state']='activated'
        quota=getTokenQuotaForPlan('annual' if subscription.get('billing_mode') == 'annual_prepaid' else 'pro',len(combined))
        remaining=max(0,int(credit['remaining_tokens'])+quota-int(credit['monthly_token_quota']))
        cursor.execute('''update public.credit_balances set monthly_token_quota=%s,remaining_tokens=%s,
            domain_count=%s,balance_version=balance_version+1,updated_at=%s where user_id=%s''',
            (quota,remaining,len(combined),evidence.observedAt,evidence.userId))
        cursor.execute('update public.subscriptions set subscribed_experts=%s,domain_count=%s,pending_additions=%s,version=version+1 where id=%s',
            (Json(combined),len(combined),Json(pending),subscription['id']))
        billing=metadata.setdefault('manualBilling',{})
        billing.update(coverageState='active',creditPeriodId=str(credit['credit_period_id']),providerPaymentId=evidence.providerPaymentId)
        cursor.execute('update public."Invoices" set status=\'PAID\',"razorpayPaymentId"=%s,"paidAt"=%s,metadata_json=%s where id=%s',
            (evidence.providerPaymentId,evidence.observedAt,Json(metadata),invoice['id']))
        invoice.update(status='PAID',metadata_json=metadata)
        cursor.execute("update public.billing_events set payment_status='captured',event_status='captured',completed_at=%s where id=%s",(evidence.observedAt,attempt['id']))
        cursor.execute("update public.billing_events set event_status='FINALIZED' where id=%s",(captureId,))
        # The next unpaid price must be re-frozen using the updated experts.
        # Paid future invoices remain immutable.
        cursor.execute('''select * from public."Invoices" where "userId"=%s and subscription_id=%s
            and billing_reason='renewal' and status in ('UPCOMING','PAYMENT_PENDING') order by id for update''',(evidence.userId,subscription['id']))
        for unpaid in cursor.fetchall(): self._closeInvoice(cursor,unpaid,'EXPERT_SELECTION_CHANGED')
        self._recordNotification(cursor,subscription,'payment_receipt','receipt:'+evidence.providerPaymentId,
            {'paymentId':evidence.providerPaymentId,'invoiceId':invoice['id'],'amount':evidence.amount,'currency':evidence.currency})
        return self._result(cursor,invoice,subscription,'expert_activated',evidence.attemptId)

    def _recordNotification(self, cursor, subscription, notificationType, dedupeKey, metadata):
        intent = {'userId':subscription['user_id'], 'subscriptionId':str(subscription['id']),
            'notificationType':notificationType, 'dedupeKey':dedupeKey,
            'periodEnd':str(metadata.get('periodEnd') or subscription.get('current_period_end') or ''), 'metadata':metadata}
        cursor.execute('''insert into public.billing_events(id,user_id,subscription_id,event_category,
            event_type,event_status,idempotency_key,metadata_json,occurred_at)
            values(%s,%s,%s,'notification','email.billing_intent.committed','COMMITTED',%s,%s,%s)
            on conflict(idempotency_key) do nothing''',
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
                if finalEnd is None or finalEnd <= _now(): raise ValueError('NO_PAID_ACCESS_TO_CANCEL_OR_RESUME')
                if bool(previous.get('renewal_opt_out')) == optOut:
                    previous['finalPaidEnd'] = finalEnd.isoformat()
                    return previous
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
                row['finalPaidEnd'] = finalEnd.isoformat()
                return dict(row)

        return self._run(operation)

    # -- staff refunds --------------------------------------------------------

    def findRefundReservation(self,userId,requestKey,quoteId,expectedAmount):
        def operation(connection):
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute('select metadata_json from public.billing_events where idempotency_key=%s',
                    ('refund-reserve:'+userId+':'+requestKey,))
                row=cursor.fetchone()
                if not row: return None
                metadata=self._json(row['metadata_json'])
                if metadata['quoteId'] != quoteId or int(metadata['amount']) != int(expectedAmount):
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
        if not all((quoteId,staffId,caseReference,reason,requestKey)):
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
                    if metadata.get('quoteId') != quoteId: raise ValueError('REFUND_IDEMPOTENCY_CONFLICT')
                    if int(metadata['amount']) != int(expectedAmount): raise ValueError('REFUND_IDEMPOTENCY_CONFLICT')
                    return self._refundIntent(metadata)
                now=_now()
                if _utc(quote['expiresAt']) <= now: raise ValueError('REFUND_QUOTE_EXPIRED')
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
                if amount != int(expectedAmount): raise ValueError('REFUND_AMOUNT_CHANGED')
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
                self._recordNotification(cursor,subscription,'subscription_refund_initiated','refund:'+intentId+':initiated',{'refundIntentId':intentId,'amount':amount,'currency':items[0]['currency'],'accessExpired':closesCurrent,'currentAccessPreserved':not closesCurrent})
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
                    metadata['providerRefunds'][item['paymentId']]=refund
                refunds=list(metadata['providerRefunds'].values())
                state='processed' if len(refunds)==len(expected) and all(r['status']=='processed' for r in refunds) else 'failed' if any(r['status']=='failed' for r in refunds) else 'pending' if refunds else metadata['refundState']
                metadata['refundState']=state
                cursor.execute('update public.billing_events set metadata_json=%s,event_status=%s where id=%s',(Json(metadata),state,refundIntentId))
                if state == 'processed':
                    subscription=self._canonical(cursor,row['user_id'])
                    self._recordNotification(cursor,subscription,'subscription_refund_processed','refund:'+refundIntentId+':processed',{'refundIntentId':refundIntentId,'amount':metadata['amount']})
                return {'refundIntentId':refundIntentId,'refundState':state,'accessRestored':False}
        return self._run(operation)


_repository: ManualBillingRepository | None = None


def getManualBillingRepository() -> ManualBillingRepository:
    global _repository
    if _repository is None:
        _repository = ManualBillingRepository()
    return _repository
