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
    NEW_COLUMNS, MODEL_EVENTS, buildBackfillProposal, evidenceHash, financialEvidenceHash,
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
                'authorizesCutover': False}

    def _event(self, cursor, row, eventType, key, payload):
        cursor.execute('''INSERT INTO public.billing_events
            (id,user_id,subscription_id,event_category,event_type,event_status,idempotency_key,metadata_json)
            VALUES(%s,%s,%s,'audit',%s,'COMMITTED',%s,%s)
            ON CONFLICT(idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING RETURNING id''',
            (str(uuid4()), row['user_id'], row['id'], eventType, key, Json(jsonValue(payload))))
        inserted = cursor.fetchone()
        if inserted:
            return str(inserted['id'])
        cursor.execute('SELECT * FROM public.billing_events WHERE idempotency_key=%s', (key,))
        existing = cursor.fetchone()
        if (not existing or existing['event_type'] != eventType or existing['event_status'] != 'COMMITTED'
                or existing['user_id'] != row['user_id'] or str(existing['subscription_id']) != str(row['id'])
                or metadata(existing) != jsonValue(payload)):
            raise ValueError('REVIEW_ARCHIVE_CONFLICT')
        return str(existing['id'])

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
