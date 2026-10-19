"""Reviewed migration transactions and read-only subscription snapshots."""
from contextlib import closing
from uuid import uuid4

from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor

from api.services.billing.manualBillingRepository import _advisoryKey
from api.services.billing.manualBillingPresentation import serializeSubscriptionSnapshot
from api.services.subscriptions.subscriptionModel import SubscriptionAuthorityUnavailable, evaluateSubscription
from api.services.subscriptions.subscriptionCompatibility import getSubscriptionModelPhase
from api.services.subscriptions.subscriptionModelBackfill import (
    NEW_COLUMNS, RETIRED_COLUMNS, MODEL_EVENTS, EXPERTS, acceptedCapture, acceptedPeriods, buildBackfillProposal, evidenceHash, financialEvidenceHash,
    factsFromRecords, jsonValue, legacyRow, legacyRowHash, metadata, stableLegacyHash, timestamp,
)


class SubscriptionModelRepository:
    def __init__(self, connectionFactory):
        self.connectionFactory = connectionFactory

    @staticmethod
    def _lock(cursor, userId):
        cursor.execute('SELECT pg_advisory_xact_lock(%s)', (_advisoryKey(userId),))

    def _records(self, cursor, userId):
        self._lock(cursor, userId)
        cursor.execute('SELECT "userId","isBanned" FROM public."Users" WHERE "userId"=%s', (userId,))
        owner = cursor.fetchone()
        cursor.execute('SELECT * FROM public.subscriptions WHERE user_id=%s ORDER BY id', (userId,))
        rows = list(cursor.fetchall())
        cursor.execute('SELECT * FROM public.credit_balances WHERE user_id=%s', (userId,))
        balance = cursor.fetchone()
        if owner:
            owner = dict(owner)
            owner['creditHistory'] = bool(balance and (balance.get('plan_tier') not in (None, 'none')
                or balance.get('lifecycle_id') or balance.get('credit_period_id')
                or any(balance.get(k) is not None for k in ('period_start', 'period_end', 'last_reset_at'))
                or any(int(balance.get(k) or 0) > 0 for k in ('used_tokens', 'remaining_tokens', 'topup_tokens', 'monthly_token_quota'))))
        ids = [str(r['id']) for r in rows]
        cursor.execute('SELECT * FROM public."Invoices" WHERE "userId"=%s OR subscription_id=ANY(%s::uuid[]) ORDER BY id', (userId, ids))
        invoices = list(cursor.fetchall())
        cursor.execute('SELECT * FROM public.billing_events WHERE user_id=%s OR subscription_id=ANY(%s::uuid[]) ORDER BY id', (userId, ids))
        return rows, dict(owner) if owner else None, invoices, list(cursor.fetchall())

    @staticmethod
    def _related(row, rows, invoices, events):
        ownInvoices = [i for i in invoices if str(i.get('subscription_id')) == str(row['id']) or i.get('subscription_id') is None]
        ownEvents = [e for e in events if str(e.get('subscription_id')) == str(row['id']) or e.get('subscription_id') is None]
        others = [r for r in rows if str(r['id']) != str(row['id'])]
        return ownInvoices, ownEvents, others

    @staticmethod
    def _now(cursor, requested=None):
        if requested is not None:
            return timestamp(requested)
        cursor.execute('SELECT clock_timestamp() AS evaluated_at')
        return cursor.fetchone()['evaluated_at']

    def inventory(self, *, now=None, trialReviews=None):
        # Release each owner's lock before inspecting the next account.
        with closing(self.connectionFactory()) as db:
            db.set_session(readonly=True)
            with db, db.cursor() as cursor:
                cursor.execute('SELECT DISTINCT user_id FROM public.subscriptions ORDER BY user_id')
                users = [r[0] for r in cursor.fetchall()]
                from api.services.userErasureRetention import retentionAvailable
                erasureReady = retentionAvailable(cursor)
        proposals = []
        for userId in users:
            with closing(self.connectionFactory()) as db:
                db.set_session(readonly=True)
                with db, db.cursor(cursor_factory=RealDictCursor) as cursor:
                    rows, owner, invoices, events = self._records(cursor, userId)
                    evaluated = self._now(cursor, now)
                    for row in rows:
                        ownInvoices, ownEvents, others = self._related(row, rows, invoices, events)
                        review = (trialReviews or {}).get(str(row['id']))
                        proposal = buildBackfillProposal(row, owner, ownInvoices, ownEvents, now=evaluated,
                            otherSubscriptions=others, trialReview=review)
                        if review is not None:
                            proposal['trialReview'] = review
                        proposals.append(proposal)
        counts = {key: sum(p['resolution'] == key for p in proposals) for key in ('verified', 'consumed_dates_unknown', 'needs_review')}
        return {'schemaVersion': 2, 'readOnly': True, 'counts': counts, 'proposals': proposals,
                'consumerBlockers': [] if erasureReady else ['SUBSCRIPTION_ERASURE_RETENTION_PENDING'],
                'authorizesCutover': False}

    def _event(self, cursor, row, eventType, key, payload, *, invoiceId=None):
        cursor.execute('''INSERT INTO public.billing_events
            (id,user_id,subscription_id,invoice_id,event_category,event_type,event_status,idempotency_key,metadata_json)
            VALUES(%s,%s,%s,%s,'audit',%s,'COMMITTED',%s,%s)
            ON CONFLICT(idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING RETURNING id''',
            (str(uuid4()), row['user_id'], row['id'], invoiceId, eventType, key, Json(jsonValue(payload))))
        inserted = cursor.fetchone()
        if inserted:
            return str(inserted['id'])
        cursor.execute('SELECT * FROM public.billing_events WHERE idempotency_key=%s', (key,))
        existing = cursor.fetchone()
        if (not existing or existing['event_type'] != eventType or existing['event_status'] != 'COMMITTED'
                or existing['user_id'] != row['user_id'] or str(existing['subscription_id']) != str(row['id'])
                or str(existing.get('invoice_id')) != str(invoiceId)
                or metadata(existing) != jsonValue(payload)):
            raise ValueError('REVIEW_ARCHIVE_CONFLICT')
        return str(existing['id'])

    def _purchaseRecords(self, cursor, subscriptionId):
        cursor.execute('SELECT user_id FROM public.subscriptions WHERE id=%s', (subscriptionId,))
        record = cursor.fetchone()
        if not record:
            raise SubscriptionAuthorityUnavailable('Purchased subscription missing')
        rows, owner, invoices, events = self._records(cursor, record['user_id'])
        row = next(r for r in rows if str(r['id']) == str(subscriptionId))
        invoices, events, _ = self._related(row, rows, invoices, events)
        if not owner or any(i.get('userId') != row['user_id'] or str(i.get('subscription_id')) != str(row['id']) for i in invoices):
            raise SubscriptionAuthorityUnavailable('Purchase owner mismatch')
        if any(e.get('user_id') != row['user_id'] or str(e.get('subscription_id')) != str(row['id']) for e in events):
            raise SubscriptionAuthorityUnavailable('Purchase event owner mismatch')
        return row, invoices, events

    def _acceptedPurchases(self, cursor, subscriptionId):
        try:
            row, invoices, events = self._purchaseRecords(cursor, subscriptionId)
            periods = acceptedPeriods(row, invoices, events, allowExpertAdditions=True)
            for period in periods:
                if (period.start is None or period.end is None or period.start >= period.end
                        or len(set(period.experts)) != len(period.experts)):
                    raise ValueError('INVALID_ACCEPTED_PERIOD')
            grants = []
            for invoice in invoices:
                billing = metadata(invoice).get('manualBilling', {})
                if str(invoice.get('status')).upper() != 'PAID' or billing.get('purpose') != 'expert_addition':
                    continue
                acceptedCapture(row, invoice, events)
                matches = [e for e in events if e.get('event_type') == 'subscription.expert_grant'
                           and e.get('event_status') == 'COMMITTED' and str(e.get('invoice_id')) == str(invoice['id'])]
                if len(matches) != 1:
                    raise ValueError('EXPERT_GRANT_REQUIRES_REVIEW')
                grant = metadata(matches[0])['expertGrant']
                attempt = next((e for e in events if str(e.get('id')) == grant.get('attemptId')
                    and e.get('event_category') == 'payment_attempt' and str(e.get('invoice_id')) == str(invoice['id'])), None)
                frozen = metadata(attempt).get('manualBilling', {}) if attempt else {}
                if (not attempt or attempt.get('payment_status') != 'captured'
                        or attempt.get('provider_order_id') != invoice.get('razorpay_order_id')
                        or attempt.get('amount') != invoice.get('total_amount') or attempt.get('currency') != invoice.get('currency')
                        or any(frozen.get(k) != billing.get(k) for k in ('purpose', 'billingMode', 'lifecycleId', 'servicePeriodId'))
                        or set(frozen.get('domains', [])) != set(billing.get('domains', []))):
                    raise ValueError('EXPERT_GRANT_ATTEMPT_MISMATCH')
                period = next((p for p in periods if p.id == grant.get('periodId')), None)
                domains = grant.get('experts')
                acceptedAt = timestamp(grant.get('acceptedAt'))
                if (not period or not isinstance(domains, list) or not domains or len(domains) != len(set(domains))
                        or not set(domains) <= set(EXPERTS) or set(domains) != set(billing.get('domains', []))
                        or grant.get('lifecycleId') != period.lifecycleId or billing.get('lifecycleId') != period.lifecycleId
                        or billing.get('servicePeriodId') != period.id or str(invoice['id']) != grant.get('invoiceId')
                        or not acceptedAt or not period.start <= acceptedAt < period.end
                        or timestamp(invoice.get('period_end')) != period.end):
                    raise ValueError('EXPERT_GRANT_PERIOD_MISMATCH')
                revokedAt = timestamp(billing.get('revokedAt'))
                if billing.get('coverageState') == 'revoked' and revokedAt is None:
                    raise ValueError('EXPERT_REVOCATION_REQUIRES_REVIEW')
                grants.append(grant | {'revokedAt': revokedAt})
            return row, periods, grants
        except (ValueError, KeyError, TypeError) as exc:
            raise SubscriptionAuthorityUnavailable('Owned purchase evidence unavailable') from exc

    def loadAcceptedPeriods(self, cursor, subscriptionId, lifecycleId=None):
        _, periods, _ = self._acceptedPurchases(cursor, subscriptionId)
        return tuple(p for p in periods if lifecycleId is None or p.lifecycleId == lifecycleId)

    def loadEffectiveExperts(self, cursor, periodId, now):
        cursor.execute('SELECT subscription_id FROM public."Invoices" WHERE id=%s', (periodId,))
        invoice = cursor.fetchone()
        if not invoice or not invoice['subscription_id']:
            raise SubscriptionAuthorityUnavailable('Purchased period missing')
        _, periods, grants = self._acceptedPurchases(cursor, str(invoice['subscription_id']))
        period = next((p for p in periods if p.id == str(periodId)), None)
        if not period:
            raise SubscriptionAuthorityUnavailable('Accepted period missing')
        experts = list(period.experts)
        for grant in sorted(grants, key=lambda g: (timestamp(g['acceptedAt']), g['invoiceId'])):
            if (grant['periodId'] == period.id and timestamp(grant['acceptedAt']) <= timestamp(now)
                    and (grant['revokedAt'] is None or timestamp(now) < grant['revokedAt'])):
                if set(experts) & set(grant['experts']):
                    raise SubscriptionAuthorityUnavailable('Overlapping expert grants')
                experts.extend(grant['experts'])
        if len(experts) > 4:
            raise SubscriptionAuthorityUnavailable('Expert capacity exceeded')
        return tuple(experts)

    def loadNextSelection(self, cursor, subscriptionId, lifecycleId, cycleStart):
        row, periods, _ = self._acceptedPurchases(cursor, subscriptionId)
        cycleStart = timestamp(cycleStart)
        for period in periods:
            if period.lifecycleId == lifecycleId and period.start == cycleStart and (period.revokedAt is None or period.revokedAt > period.start):
                return {'id': None, 'experts': list(period.experts), 'paid': True, 'invoiceId': period.invoiceId}
        cursor.execute("""SELECT * FROM public.billing_events WHERE user_id=%s AND subscription_id=%s
            AND event_type='subscription.next_selection' AND event_status='COMMITTED'
            ORDER BY occurred_at DESC,id DESC""", (row['user_id'], row['id']))
        selections = []
        for event in cursor.fetchall():
            value = metadata(event)['nextSelection']
            if value.get('lifecycleId') != lifecycleId or timestamp(value.get('cycleStart')) != cycleStart:
                continue
            experts = value.get('experts')
            if (not isinstance(experts, list) or not experts or len(set(experts)) != len(experts)
                    or not set(experts) <= set(EXPERTS) or type(value.get('version')) is not int):
                raise SubscriptionAuthorityUnavailable('Invalid next selection')
            selections.append({'id': str(event['id']), **value, 'paid': False})
        return max(selections, key=lambda s: s['version']) if selections else None

    def saveNextSelection(self, cursor, subscriptionId, lifecycleId, cycleStart, expectedVersion, experts, *, now=None):
        if getSubscriptionModelPhase() not in ('dual', 'v2', 'contracted'):
            raise ValueError('SELECTION_PHASE_NOT_ALLOWED')
        row, periods, _ = self._acceptedPurchases(cursor, subscriptionId)
        cursor.execute('SELECT "isBanned" FROM public."Users" WHERE "userId"=%s', (row['user_id'],))
        owner = cursor.fetchone()
        if not owner or owner['isBanned']:
            raise ValueError('SELECTION_OWNER_NOT_ELIGIBLE')
        evaluated, cycleStart = self._now(cursor, now), timestamp(cycleStart)
        current = next((p for p in periods if p.lifecycleId == lifecycleId and p.start <= evaluated < p.end and p.revokedAt is None), None)
        if not current or current.end != cycleStart or row.get('erasure_pending') or not row['is_canonical']:
            raise ValueError('SELECTION_CYCLE_MISMATCH')
        effective = self.loadEffectiveExperts(cursor, current.id, evaluated)
        if not isinstance(experts, (list, tuple)) or not experts or len(experts) != len(set(experts)) or not set(experts) <= set(effective):
            raise ValueError('INVALID_EXPERT_REMOVAL')
        previous = self.loadNextSelection(cursor, subscriptionId, lifecycleId, cycleStart)
        if previous and previous.get('paid'):
            raise ValueError('PAID_FUTURE_SELECTION_IMMUTABLE')
        if type(expectedVersion) is not int or row['version'] != expectedVersion:
            raise ValueError('STALE_SUBSCRIPTION_VERSION')
        if previous and set(previous['experts']) == set(experts):
            return previous
        cursor.execute('UPDATE public.subscriptions SET version=version+1 WHERE id=%s AND version=%s RETURNING version', (subscriptionId, expectedVersion))
        version = cursor.fetchone()['version']
        value = jsonValue(dict(lifecycleId=lifecycleId, cycleStart=cycleStart, experts=list(experts), version=version))
        eventId = self._event(cursor, row, 'subscription.next_selection',
            f'subscription-selection:{subscriptionId}:{lifecycleId}:{cycleStart.isoformat()}:{version}', {'nextSelection': value})
        # A changed unpaid selection closes its old quote/attempt atomically.
        from api.services.billing.manualBillingRepository import ManualBillingRepository
        cursor.execute('''SELECT * FROM public."Invoices" WHERE "userId"=%s AND subscription_id=%s
            AND billing_reason='renewal' AND period_start=%s AND status IN ('UPCOMING','PAYMENT_PENDING') ORDER BY id FOR UPDATE''',
            (row['user_id'], row['id'], cycleStart))
        for invoice in cursor.fetchall():
            ManualBillingRepository()._closeInvoice(cursor, invoice, 'EXPERT_SELECTION_CHANGED', now=evaluated)
        return {'id': eventId, **value, 'paid': False}

    def recordExpertGrant(self, cursor, row, invoice, attempt, evidence, periodId):
        payload = jsonValue(dict(periodId=periodId, invoiceId=str(invoice['id']), attemptId=str(attempt['id']),
            lifecycleId=metadata(attempt)['manualBilling']['lifecycleId'],
            experts=metadata(attempt)['manualBilling']['domains'], acceptedAt=evidence.observedAt))
        return self._event(cursor, row, 'subscription.expert_grant', 'expert-grant:' + str(invoice['id']),
            {'expertGrant': payload}, invoiceId=str(invoice['id']))

    def archiveLegacySnapshot(self, cursor, row):
        self._lock(cursor, row['user_id'])
        cursor.execute('SELECT * FROM public.subscriptions WHERE id=%s AND user_id=%s FOR UPDATE', (row['id'], row['user_id']))
        current = cursor.fetchone()
        if not current or legacyRowHash(current) != legacyRowHash(row):
            raise ValueError('REVIEW_ARCHIVE_SOURCE_CHANGED')
        key = f"subscription-archive:{row['id']}:{row['version']}:{legacyRowHash(row)}"
        return self._event(cursor, row, MODEL_EVENTS[0], key, {'archive': {
            'schemaVersion': 2, 'originalVersion': row['version'], 'legacyHash': legacyRowHash(row),
            'originalRow': legacyRow(row)}})

    def writeCoreFacts(self, cursor, subscriptionId, expectedVersion, patch, phase):
        """Expansion-only writes. Runtime/dual projection belongs to later tasks."""
        if phase not in ('legacy', 'shadow') or not patch or not set(patch) <= NEW_COLUMNS or type(expectedVersion) is not int or expectedVersion < 1:
            raise ValueError('REVIEW_CORE_PATCH_OR_PHASE_INVALID')
        cursor.execute('SELECT user_id FROM public.subscriptions WHERE id=%s', (subscriptionId,))
        owner = cursor.fetchone()
        if not owner:
            raise ValueError('REVIEW_SUBSCRIPTION_MISSING')
        self._lock(cursor, owner['user_id'])
        cursor.execute(sql.SQL('UPDATE public.subscriptions SET {},version=version+1 WHERE id=%s AND version=%s RETURNING *').format(
            sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(k)) for k in patch)),
            list(patch.values()) + [subscriptionId, expectedVersion])
        result = cursor.fetchone()
        if not result:
            raise ValueError('REVIEW_VERSION_CHANGED')
        return dict(result)

    def applyReviewedDecision(self, decision, *, now=None):
        if getSubscriptionModelPhase() not in ('legacy', 'shadow'):
            raise ValueError('REVIEW_PHASE_NOT_ALLOWED')
        if any(not isinstance(decision.get(k), str) or not decision[k].strip() for k in ('reviewedBy', 'reviewReference')):
            raise ValueError('REVIEW_REQUIRED')
        with closing(self.connectionFactory()) as db, db, db.cursor(cursor_factory=RealDictCursor) as cursor:
            rows, owner, invoices, events = self._records(cursor, decision['userId'])
            row = next((r for r in rows if str(r['id']) == decision['subscriptionId']), None)
            if row is None:
                raise ValueError('REVIEW_OWNER_OR_SUBSCRIPTION_CHANGED')
            ownInvoices, ownEvents, others = self._related(row, rows, invoices, events)
            archiveKey = f"subscription-archive:{row['id']}:{decision['expectedVersion']}:{decision['legacyHash']}"
            cursor.execute('SELECT * FROM public.billing_events WHERE idempotency_key=%s', (archiveKey,))
            archived = cursor.fetchone()
            if archived:
                cursor.execute('SELECT * FROM public.billing_events WHERE idempotency_key=%s', (f"subscription-backfill:{archived['id']}",))
                stamp = cursor.fetchone()
                if stamp:
                    payload = metadata(stamp)['backfill']
                    if (payload['approvalHash'] != evidenceHash(decision) or not self._stampMatches(row, owner, ownInvoices, ownEvents, others, payload)):
                        raise ValueError('REVIEW_REPLAY_CHANGED')
                    return {'status': 'already_applied', 'subscriptionId': str(row['id'])}
            proposal = buildBackfillProposal(row, owner, ownInvoices, ownEvents, now=self._now(cursor, now),
                otherSubscriptions=others, trialReview=decision.get('trialReview'))
            if any(jsonValue(decision.get(k)) != jsonValue(v) for k, v in proposal.items()):
                raise ValueError('REVIEW_EVIDENCE_CHANGED')
            archiveId = self.archiveLegacySnapshot(cursor, row)
            if proposal['resolution'] == 'needs_review':
                return {'status': 'needs_review', 'subscriptionId': str(row['id']), 'archiveId': archiveId}
            updated = self.writeCoreFacts(cursor, str(row['id']), row['version'], proposal['patch'], 'shadow')
            payload = dict(approvalHash=evidenceHash(decision), archiveId=archiveId,
                sourceStableHash=stableLegacyHash(row), evidenceHash=proposal['evidenceHash'],
                appliedVersion=updated['version'], patch=proposal['patch'], trialConsumed=proposal['trialConsumed'],
                trialReview=decision.get('trialReview'),
                resolution=proposal['resolution'], reviewedBy=decision['reviewedBy'], reviewReference=decision['reviewReference'])
            self._event(cursor, row, MODEL_EVENTS[1], f'subscription-backfill:{archiveId}', {'backfill': payload})
            return {'status': 'applied', 'subscriptionId': str(row['id']), 'archiveId': archiveId}

    @staticmethod
    def _stampMatches(row, owner, invoices, events, others, stamp):
        return (row['version'] == stamp['appliedVersion'] and stableLegacyHash(row) == stamp['sourceStableHash']
                and financialEvidenceHash(owner, invoices, events, others) == stamp['evidenceHash']
                and jsonValue({k: row.get(k) for k in NEW_COLUMNS}) == stamp['patch'])

    def loadFacts(self, userId, cursor):
        rows, owner, invoices, events = self._records(cursor, userId)
        canonical = [r for r in rows if r['is_canonical'] is True]
        if len(canonical) != 1:
            raise SubscriptionAuthorityUnavailable('Canonical subscription missing or ambiguous')
        row = canonical[0]
        ownInvoices, ownEvents, others = self._related(row, rows, invoices, events)
        stamps = [e for e in ownEvents if e.get('event_type') == MODEL_EVENTS[1] and e.get('event_status') == 'COMMITTED']
        for stamp in sorted(stamps, key=lambda e: (e['occurred_at'], str(e['id'])), reverse=True):
            payload = metadata(stamp).get('backfill', {})
            archive = next((e for e in ownEvents if str(e['id']) == payload.get('archiveId')
                            and e.get('event_type') == MODEL_EVENTS[0] and e.get('event_status') == 'COMMITTED'), None)
            archived = metadata(archive).get('archive', {}) if archive else {}
            original = archived.get('originalRow')
            archiveValid = (isinstance(original, dict) and str(original.get('id')) == str(row['id'])
                            and original.get('user_id') == userId
                            and legacyRowHash(original) == archived.get('legacyHash')
                            and stableLegacyHash(original) == payload.get('sourceStableHash'))
            if archiveValid and payload.get('resolution') in ('verified', 'consumed_dates_unknown') and self._stampMatches(row, owner, ownInvoices, ownEvents, others, payload):
                return factsFromRecords(row, owner, ownInvoices, ownEvents, consumed=payload['trialConsumed'])
        raise SubscriptionAuthorityUnavailable('Reviewed backfill is missing or stale')

    def loadRuntimeFacts(self, cursor, row, now):
        """Read explicit core facts and current owned money, never a cached label.

        The migration verifier above remains frozen to its reviewed source. A
        runtime write need not rewrite that immutable approval or pretend its
        old financial fingerprint is still current. Trial history must have
        an owned initialization/trial event or a valid reviewed archive.
        """
        from dataclasses import replace
        from api.services.billing.manualBillingRepository import ManualBillingRepository
        from api.services.subscriptions.subscriptionModel import TrialFacts
        billing = ManualBillingRepository(self.connectionFactory)
        cursor.execute('SELECT "userId","isBanned" FROM public."Users" WHERE "userId"=%s', (row['user_id'],))
        owner = cursor.fetchone()
        if not owner:
            raise SubscriptionAuthorityUnavailable('Subscription owner missing')
        facts = billing._purchaseFactsLocked(cursor, row, owner, now)
        _, _, events = self._purchaseRecords(cursor, str(row['id']))
        events = [e for e in events if str(e.get('subscription_id')) == str(row['id']) and e.get('event_status') == 'COMMITTED']
        trialEvent = next((e for e in events if e['event_type'] == 'subscription.trial_started'), None)
        initializers = [e for e in events if e['event_type'] == 'subscription.model_initialized']
        initialized = bool(initializers)
        originals = {}
        for event in (e for e in events if e['event_type'] == MODEL_EVENTS[0]):
            archive = metadata(event).get('archive', {})
            source = archive.get('originalRow') if isinstance(archive,dict) else None
            if (isinstance(source,dict) and archive.get('schemaVersion')==2
                    and source.get('user_id')==row['user_id'] and str(source.get('id'))==str(row['id'])
                    and legacyRowHash(source)==archive.get('legacyHash')):
                originals[str(event['id'])]=source
        reviewed, consumed, experts = False, False, ()
        for stamp in (e for e in events if e['event_type'] == MODEL_EVENTS[1]):
            payload = metadata(stamp).get('backfill', {})
            archive = next((e for e in events if str(e['id']) == payload.get('archiveId') and e['event_type'] == MODEL_EVENTS[0]), None)
            original = metadata(archive).get('archive', {}) if archive else {}
            source = original.get('originalRow')
            if (isinstance(source, dict) and source.get('user_id') == row['user_id'] and str(source.get('id')) == str(row['id'])
                    and legacyRowHash(source) == original.get('legacyHash')
                    and payload.get('resolution') in ('verified', 'consumed_dates_unknown')):
                reviewed, consumed = True, bool(payload.get('trialConsumed'))
                experts = tuple(billing._jsonList(source.get('subscribed_experts')))
        if facts.periods:
            if row.get('plan_code') != 'pro' or not row.get('lifecycle_id'):
                raise SubscriptionAuthorityUnavailable('Paid core identity requires review')
        elif not (initialized or reviewed):
            raise SubscriptionAuthorityUnavailable('Trial history requires reviewed initialization')
        if initialized and not reviewed and not trialEvent and not facts.periods:
            retainedInitializer = any(metadata(e).get('schemaVersion')==2
                and metadata(e).get('storageShape')=='retained' for e in initializers)
            historicalProof = any(RETIRED_COLUMNS <= source.keys() for source in originals.values())
            if not RETIRED_COLUMNS <= row.keys() and not historicalProof and not retainedInitializer:
                raise SubscriptionAuthorityUnavailable('Retained legacy history requires owned evidence')
            # The current row no longer contains history after contraction.
            # Original owned audits still constrain once-only trial eligibility.
            history = [row,*originals.values()]
            legacyConsumed = any(source.get('current_period_start') or source.get('current_period_end')
                or billing._json(source.get('billing_state')).get('trialConsumed')
                or billing._json(source.get('billing_state')).get('trialConsumedAt')
                or billing._json(source.get('billing_state')).get('churn_snapshot')
                or source.get('plan_type') in ('free','pro','annual')
                or source.get('billing_mode') not in ('none',None)
                or ('status' in source and source.get('status')!='none')
                or source.get('admin_credit_generation') for source in history)
            cursor.execute('SELECT * FROM public.credit_balances WHERE user_id=%s', (row['user_id'],))
            balance = cursor.fetchone()
            if (legacyConsumed or (balance and (balance.get('plan_tier') not in ('none', None)
                    or balance.get('credit_period_id') or balance.get('period_start')))):
                raise SubscriptionAuthorityUnavailable('Legacy entitlement history requires review')
        trial = TrialFacts(consumed=consumed or bool(facts.periods))
        if row.get('trial_started_at') is not None and (trialEvent or reviewed):
            experts = tuple(metadata(trialEvent).get('experts', ())) if trialEvent else experts
            if not set(experts) <= set(EXPERTS):
                raise SubscriptionAuthorityUnavailable('Unknown trial experts')
            trial = TrialFacts(timestamp(row['trial_started_at']), timestamp(row.get('trial_ends_at')),
                timestamp(row.get('trial_closed_at')), experts, True)
        elif row.get('trial_started_at') is not None and not facts.periods:
            raise SubscriptionAuthorityUnavailable('Trial experts require owned evidence')
        elif not facts.periods and row.get('plan_code') == 'pro' and not consumed:
            raise SubscriptionAuthorityUnavailable('Trial history is incomplete')
        return replace(facts, trial=trial, planCode=row.get('plan_code'))

    def snapshotFactsLoader(self, userId, now=None):
        try:
            with closing(self.connectionFactory()) as db:
                db.set_session(readonly=True)
                with db, db.cursor(cursor_factory=RealDictCursor) as cursor:
                    facts = self.loadFacts(userId, cursor)
                    return facts, self._now(cursor, now)
        except SubscriptionAuthorityUnavailable:
            raise
        except Exception as exc:
            raise SubscriptionAuthorityUnavailable('Subscription facts unavailable') from exc

    def shadow(self, userId, now=None):
        try:
            with closing(self.connectionFactory()) as db:
                db.set_session(readonly=True)
                with db, db.cursor(cursor_factory=RealDictCursor) as cursor:
                    facts = self.loadFacts(userId, cursor)
                    snapshot = evaluateSubscription(facts, self._now(cursor, now))
                    cursor.execute('SELECT status,subscribed_experts FROM public.subscriptions WHERE id=%s', (facts.subscriptionId,))
                    legacy = cursor.fetchone()
                    differences = []
                    if legacy['status'] != snapshot.status:
                        differences.append('status')
                    if legacy['subscribed_experts'] != list(snapshot.subscribedExperts):
                        differences.append('subscribedExperts')
                    return {'available': True, 'v2': serializeSubscriptionSnapshot(snapshot),
                            'legacy': dict(legacy), 'differences': differences, 'authorizesCutover': False}
        except Exception:
            # Never leak a connection string through a diagnostic exception.
            return {'available': False, 'reason': 'SUBSCRIPTION_AUTHORITY_UNAVAILABLE', 'authorizesCutover': False}
