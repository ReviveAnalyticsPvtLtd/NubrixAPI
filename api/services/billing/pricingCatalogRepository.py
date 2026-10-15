"""Consistent catalog reads and atomic, replayable administrator publication."""
from contextlib import contextmanager
from hashlib import sha256
import json
import os

import psycopg2
from psycopg2.extras import Json, RealDictCursor, execute_values
from loguru import logger

from api.services.billing.pricingCatalogContracts import (
    CatalogConflict, CatalogSnapshot, CatalogUnavailable, PublishResult, validatePrices,
)

_CATALOG_SELECT = """
    SELECT r.revision, r.published_at, r.admin_id, r.admin_email, r.reason,
           p.currency, p.sku, p.amount_minor
    FROM private.pricing_catalog_revisions r
    JOIN private.pricing_catalog_prices p ON p.revision = r.revision
"""


def _snapshot(rows):
    if len(rows) != 10:
        raise CatalogUnavailable('The complete pricing catalog is unavailable')
    first = rows[0]
    prices = {'INR': {}, 'USD': {}}
    for row in rows:
        prices[row['currency']][row['sku']] = row['amount_minor']
    return CatalogSnapshot(first['revision'], prices, first['published_at'],
                           str(first['admin_id']) if first['admin_id'] else None,
                           first['admin_email'], first['reason'])


class PricingCatalogRepository:
    def __init__(self, connectionFactory=None):
        self.connectionFactory = connectionFactory or self._connect

    @staticmethod
    def _connect():
        return psycopg2.connect(os.environ['DATABASE_URL'],
                               application_name='nubrix_pricing_catalog', connect_timeout=5)

    @contextmanager
    def _connection(self, connection=None):
        owned = connection is None
        active = connection
        try:
            if owned: active = self.connectionFactory()
            yield active
            if owned: active.commit()
        except (CatalogConflict, ValueError):
            if owned and active is not None: active.rollback()
            raise
        except Exception as exc:
            if owned and active is not None: active.rollback()
            raise CatalogUnavailable('Pricing is temporarily unavailable') from exc
        finally:
            if owned and active is not None: active.close()

    def loadCurrent(self, *, connection=None, forShare=False):
        with self._connection(connection) as active, active.cursor(cursor_factory=RealDictCursor) as cursor:
            if forShare:
                cursor.execute('SELECT revision FROM public.pricing_catalog_state WHERE id=1 FOR SHARE')
                state = cursor.fetchone()
                if not state: raise CatalogUnavailable('Pricing has not been initialized')
                return self.loadRevision(state['revision'], connection=active)
            cursor.execute(_CATALOG_SELECT + ' JOIN public.pricing_catalog_state s ON s.revision=r.revision AND s.id=1')
            return _snapshot(cursor.fetchall())

    def loadRevision(self, revision, *, connection=None):
        with self._connection(connection) as active, active.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(_CATALOG_SELECT + ' WHERE r.revision=%s', (revision,))
            return _snapshot(cursor.fetchall())

    def listHistory(self, *, beforeRevision=None, limit=20):
        limit = min(20, max(1, int(limit)))
        with self._connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute('SELECT revision FROM private.pricing_catalog_revisions '
                           'WHERE (%s IS NULL OR revision < %s) ORDER BY revision DESC LIMIT %s',
                           (beforeRevision, beforeRevision, limit + 1))
            revisions = [row['revision'] for row in cursor.fetchall()]
            selected = revisions[:limit]
            if not selected: return [], None
            cursor.execute(_CATALOG_SELECT + ' WHERE r.revision=ANY(%s) ORDER BY r.revision DESC', (selected,))
            groups = {revision: [] for revision in selected}
            for row in cursor.fetchall(): groups[row['revision']].append(row)
            return ([_snapshot(groups[revision]) for revision in selected],
                    selected[-1] if len(revisions) > limit else None)

    def publish(self, *, admin, expectedRevision, reason, prices, idempotencyKey):
        prices = validatePrices(prices)
        reason = reason.strip()
        if type(expectedRevision) is not int or not 1 <= expectedRevision <= 9007199254740991:
            raise ValueError('Invalid expected revision')
        if not 5 <= len(reason) <= 500:
            raise ValueError('Reason must contain 5 to 500 characters')
        if not isinstance(idempotencyKey, str) or not 1 <= len(idempotencyKey) <= 128 or any(c.isspace() for c in idempotencyKey):
            raise ValueError('Invalid Idempotency-Key')
        requestHash = sha256(json.dumps({'expectedRevision': expectedRevision, 'reason': reason, 'prices': prices},
                                        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self._connection() as connection, connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute('SELECT revision FROM public.pricing_catalog_state WHERE id=1 FOR UPDATE')
            state = cursor.fetchone()
            if not state: raise CatalogUnavailable('Pricing has not been initialized')
            currentRevision = state['revision']
            cursor.execute('SELECT request_hash, result_revision, no_op FROM private.pricing_catalog_publications '
                           'WHERE admin_id=%s AND idempotency_key=%s', (admin.adminId, idempotencyKey))
            replay = cursor.fetchone()
            if replay:
                if replay['request_hash'] != requestHash:
                    raise CatalogConflict('IDEMPOTENCY_CONFLICT', currentRevision)
                return PublishResult(self.loadRevision(replay['result_revision'], connection=connection), replay['no_op'])
            if currentRevision != expectedRevision:
                raise CatalogConflict('PRICING_CONFLICT', currentRevision)
            current = self.loadRevision(currentRevision, connection=connection)
            noOp = current.prices == prices
            revision = currentRevision
            changed = [f'{c}.{s}' for c, values in prices.items() for s, amount in values.items() if current.prices[c][s] != amount]
            if not noOp:
                cursor.execute('INSERT INTO private.pricing_catalog_revisions(admin_id,admin_email,reason) '
                               'VALUES(%s,%s,%s) RETURNING revision', (admin.adminId, admin.email, reason))
                revision = cursor.fetchone()['revision']
                execute_values(cursor, 'INSERT INTO private.pricing_catalog_prices(revision,currency,sku,amount_minor) VALUES %s',
                               [(revision,c,s,a) for c,v in prices.items() for s,a in v.items()])
                cursor.execute('UPDATE public.pricing_catalog_state SET revision=%s,updated_at=now() WHERE id=1', (revision,))
            cursor.execute('INSERT INTO private.pricing_catalog_publications(admin_id,idempotency_key,request_hash,result_revision,no_op) '
                           'VALUES(%s,%s,%s,%s,%s)', (admin.adminId,idempotencyKey,requestHash,revision,noOp))
            cursor.execute("INSERT INTO public.admin_audit_log(admin_id,admin_email,session_id,actor_type,action,target_type,target_id,changed_fields,details,outcome) "
                           "VALUES(%s,%s,%s,'admin','pricing.publish','pricing_catalog',%s,%s,%s,'success')",
                           (admin.adminId,admin.email,admin.sessionId,str(revision),Json(changed),
                            Json({'idempotencyKey':idempotencyKey,'requestHash':requestHash,'resultRevision':revision,'noOp':noOp})))
            result = PublishResult(self.loadRevision(revision, connection=connection), noOp)
        logger.bind(adminId=admin.adminId, action='pricing.publish', targetId=str(revision), outcome='success').info('admin_audit')
        return result


def getPricingCatalogRepository():
    return PricingCatalogRepository()
