"""Pure migration proposals; historical labels never prove paid entitlement."""
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from api.services.subscriptions.subscriptionModel import (
    PurchasedPeriod, RecoverableCheckout, SubscriptionAuthorityUnavailable,
    SubscriptionFacts, TrialFacts, evaluateSubscription,
)

NEW_COLUMNS = frozenset({'plan_code', 'lifecycle_id', 'trial_started_at', 'trial_ends_at',
                         'trial_closed_at', 'cancellation_requested_at'})
MODEL_EVENTS = ('subscription.legacy_snapshot', 'subscription.model_backfill')
EXPERTS = ('banking', 'manufacturing', 'supplychain', 'telecom')


def jsonValue(value):
    if isinstance(value, datetime):
        return timestamp(value).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonValue(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonValue(v) for v in value]
    return value


def evidenceHash(value) -> str:
    encoded = json.dumps(jsonValue(value), sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def legacyRow(row):
    return {k: v for k, v in row.items() if k not in NEW_COLUMNS}


def legacyRowHash(row) -> str:
    return evidenceHash(legacyRow(row))


def stableLegacyHash(row) -> str:
    return evidenceHash({k: v for k, v in legacyRow(row).items() if k not in ('version', 'updated_at')})


def financialEvidenceHash(owner, invoices, events, otherSubscriptions=()) -> str:
    return evidenceHash({'owner': owner,
        'invoices': sorted(invoices, key=lambda r: str(r['id'])),
        'events': sorted((e for e in events if e.get('event_type') not in MODEL_EVENTS), key=lambda r: str(r['id'])),
        'otherSubscriptions': sorted((stableLegacyHash(r) for r in otherSubscriptions))})


def timestamp(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('UNPROVEN_TIMESTAMP')
    return value.astimezone(timezone.utc)


def metadata(row, field='metadata_json') -> dict:
    value = row.get(field) or {}
    value = json.loads(value) if isinstance(value, str) else value
    if not isinstance(value, dict):
        raise ValueError('INVALID_METADATA')
    return value


def manualMetadata(row, field='metadata_json') -> dict:
    value = metadata(row, field).get('manualBilling', {})
    if not isinstance(value, dict):
        raise ValueError('INVALID_MANUAL_BILLING_METADATA')
    return value


def _ownedSubscriptionIds(row, others):
    if any(other.get('user_id') != row['user_id'] for other in others):
        raise ValueError('SUBSCRIPTION_OWNER_MISMATCH')
    return {str(row['id']), *(str(other['id']) for other in others)}


def acceptedCapture(row, invoice, events):
    """Match the committed capture to the complete owned invoice identity."""
    matches = [e for e in events if e.get('event_type') == 'payment.capture'
               and e.get('event_status') == 'FINALIZED'
               and str(e.get('invoice_id')) == str(invoice['id'])
               and e.get('provider_payment_id') == invoice.get('razorpayPaymentId')
               and e.get('provider_order_id') == invoice.get('razorpay_order_id')
               and e.get('amount') == invoice.get('total_amount')
               and e.get('currency') == invoice.get('currency')]
    if (len(matches) != 1 or not invoice.get('razorpayPaymentId')
            or not invoice.get('razorpay_order_id') or not timestamp(invoice.get('paidAt'))
            or type(invoice.get('total_amount')) is not int or invoice['total_amount'] <= 0):
        raise ValueError('CAPTURE_EVIDENCE_MISSING')
    if any(e.get('user_id') != row['user_id'] or str(e.get('subscription_id')) != str(row['id']) for e in matches):
        raise ValueError('CAPTURE_OWNER_MISMATCH')
    return matches[0]


def acceptedPeriods(row, invoices, events, *, otherSubscriptions=(), allowExpertAdditions=False) -> tuple[PurchasedPeriod, ...]:
    owned = _ownedSubscriptionIds(row, otherSubscriptions)
    periods = []
    for invoice in invoices:
        if invoice.get('userId') != row['user_id'] or str(invoice.get('subscription_id')) not in owned:
            raise ValueError('INVOICE_OWNER_MISMATCH')
        # Historical rows contribute account-level money guards and review
        # evidence, never service authority for this subscription identity.
        if str(invoice['subscription_id']) != str(row['id']):
            continue
        if str(invoice.get('status')).upper() != 'PAID':
            continue
        frozen = manualMetadata(invoice)
        if invoice.get('billing_reason') == 'add_on' and frozen.get('purpose') in (None, 'topup'):
            continue
        if allowExpertAdditions and frozen.get('purpose') == 'expert_addition':
            continue  # Runtime repository separately requires the owned grant.
        if frozen.get('purpose') not in ('initial_purchase', 'renewal'):
            raise ValueError('SERVICE_OR_EXPERT_HISTORY_REQUIRES_REVIEW')
        acceptedCapture(row, invoice, events)
        interval = {'monthly_prepaid': 'month', 'monthly_recurring': 'month', 'annual_prepaid': 'year'}.get(frozen.get('billingMode'))
        domains = frozen.get('domains')
        if not interval or not isinstance(domains, list) or not 1 <= len(domains) <= 4 or not set(domains) <= set(EXPERTS):
            raise ValueError('FROZEN_PERIOD_MISSING')
        lifecycle = str(UUID(frozen['lifecycleId']))
        revoked = timestamp(frozen.get('revokedAt'))
        if frozen.get('coverageState') not in ('active', 'scheduled', 'elapsed', 'revoked') or (frozen.get('coverageState') == 'revoked' and revoked is None):
            raise ValueError('COVERAGE_DECISION_MISSING')
        periods.append(PurchasedPeriod(str(invoice['id']), row['user_id'], str(row['id']), lifecycle,
            str(invoice['id']), timestamp(invoice.get('period_start')), timestamp(invoice.get('period_end')),
            tuple(domains), interval, str(frozen['creditPeriodId']) if frozen.get('creditPeriodId') else None, revoked))
    return tuple(periods)


def factsFromRecords(row, owner, invoices, events, *, consumed, otherSubscriptions=()):
    if not owner or owner.get('userId') != row['user_id'] or type(owner.get('isBanned')) is not bool:
        raise ValueError('OWNER_AUTHORITY_MISSING')
    owned = _ownedSubscriptionIds(row, otherSubscriptions)
    for event in events:
        if (event.get('user_id') != row['user_id'] or
                (event.get('subscription_id') is not None and str(event['subscription_id']) not in owned)):
            raise ValueError('EVENT_OWNER_MISMATCH')
    periods = acceptedPeriods(row, invoices, events, otherSubscriptions=otherSubscriptions)
    unresolved, attempts = unresolvedMoneyFacts(invoices, events)
    trial = TrialFacts(timestamp(row.get('trial_started_at')), timestamp(row.get('trial_ends_at')),
                       timestamp(row.get('trial_closed_at')), EXPERTS if row.get('trial_started_at') else (), consumed)
    return SubscriptionFacts(row['user_id'], str(row['id']), str(row['lifecycle_id']) if row.get('lifecycle_id') else None,
        row.get('plan_code'), True, trial, periods, row['renewal_opt_out'], owner['isBanned'], row['erasure_pending'],
        unresolved, attempts[0] if len(attempts) == 1 else None)


def unresolvedMoneyFacts(invoices, events):
    """Labels do not prove Order closure; share this policy with runtime admission."""
    unresolved, attempts = False, []
    invoiceStates = {str(i['id']): str(i.get('status')).upper() for i in invoices}
    for event in events:
        frozen = manualMetadata(event)
        status = event.get('payment_status')
        pending = (event.get('event_type') in ('payment.capture', 'payment.unmapped')
                   and event.get('event_status') in ('OBSERVED', 'REQUIRES_RECONCILIATION'))
        if event.get('event_category') == 'payment_attempt':
            finalized = status == 'captured' and invoiceStates.get(str(event.get('invoice_id'))) == 'PAID'
            closedAt = timestamp(frozen.get('closedAt'))
            reconciledAt = timestamp(frozen.get('closureReconciledAt'))
            provenClosed = (status in ('failed', 'cancelled', 'expired') and closedAt is not None
                            and reconciledAt is not None and reconciledAt >= closedAt)
            # One failed payment or an expired browser clock does not prove an
            # Order cannot still collect money. Only reconciled closure clears it.
            pending = not finalized and not provenClosed
        if event.get('event_type') == 'refund.intent' and event.get('event_status') != 'processed':
            pending = True
        unresolved |= pending
        if pending and event.get('event_category') == 'payment_attempt' and event.get('invoice_id'):
            attempts.append(RecoverableCheckout(str(event['invoice_id']), str(event['id']), str(status)))
    return unresolved, attempts


def _checkHistoricalService(row, others, invoices, events, now):
    # A trial-consumption review cannot resolve a different row's paid service.
    # Validate that service separately; never move its grants onto this row.
    for historical in others:
        siblings = (row, *(other for other in others if str(other['id']) != str(historical['id'])))
        periods = acceptedPeriods(historical, invoices, events, otherSubscriptions=siblings)
        if not periods and (historical.get('billing_mode') not in ('none', None)
                            or historical.get('plan_type') in ('pro', 'annual')):
            raise ValueError('OTHER_PAID_HISTORY_REQUIRES_REVIEW')
        for period in periods:
            if period.start is None or period.end is None or period.end <= period.start:
                raise ValueError('OTHER_PAID_HISTORY_REQUIRES_REVIEW')
            end = min(period.end, period.revokedAt) if period.revokedAt else period.end
            if end > now:
                raise ValueError('OTHER_PAID_SERVICE_REQUIRES_REVIEW')


def buildBackfillProposal(row, owner, invoices, events, *, now, otherSubscriptions=(), trialReview=None):
    """Return an inspectable proposal; only the transaction layer can apply it."""
    proposal = {'schemaVersion': 2, 'subscriptionId': str(row['id']), 'userId': row['user_id'],
        'expectedVersion': row['version'], 'legacyHash': legacyRowHash(row),
        'evidenceHash': financialEvidenceHash(owner, invoices, events, otherSubscriptions),
        'resolution': 'needs_review', 'patch': None, 'trialConsumed': False, 'reasons': []}
    try:
        now = timestamp(now)
        if trialReview is not None and (not isinstance(trialReview, dict) or trialReview.get('consumed') is not True
                or not isinstance(trialReview.get('reviewReference'), str) or not trialReview['reviewReference'].strip()):
            raise ValueError('TRIAL_REVIEW_REFERENCE_REQUIRED')
        state = metadata(row, 'billing_state')
        manual = manualMetadata(row, 'billing_state')
        paid = acceptedPeriods(row, invoices, events, otherSubscriptions=otherSubscriptions)
        removals = row.get('pending_removals') or []
        removals = json.loads(removals) if isinstance(removals, str) else removals
        if removals:
            lifecycle = manual.get('lifecycleId')
            destinations = [metadata(e).get('nextSelection', {}) for e in events
                if e.get('event_type') == 'subscription.next_selection' and e.get('event_status') == 'COMMITTED']
            current = set(row.get('subscribed_experts') or [])
            if not any(d.get('lifecycleId') == lifecycle and timestamp(d.get('cycleStart')) == timestamp(row.get('current_period_end'))
                       and set(d.get('experts', [])) == current - set(removals) for d in destinations):
                raise ValueError('LEGACY_REMOVAL_DESTINATION_REQUIRES_REVIEW')
        additions = row.get('pending_additions') or []
        additions = json.loads(additions) if isinstance(additions, str) else additions
        for addition in additions:
            attempt = next((e for e in events if str(e.get('id')) == str(addition.get('attemptId'))
                and e.get('event_category') == 'payment_attempt'), None)
            frozen = manualMetadata(attempt) if attempt else {}
            if (not frozen.get('servicePeriodId') or addition.get('domain') not in frozen.get('domains', [])
                    or frozen.get('purpose') != 'expert_addition'):
                raise ValueError('LEGACY_ADDITION_DESTINATION_REQUIRES_REVIEW')
        consumed = bool(state.get('trialConsumed') or state.get('trialConsumedAt') or row.get('plan_type') == 'free'
                        or (trialReview or {}).get('consumed'))
        if otherSubscriptions and not (trialReview or {}).get('consumed'):
            raise ValueError('OTHER_SUBSCRIPTION_HISTORY_REQUIRES_REVIEW')
        _checkHistoricalService(row, otherSubscriptions, invoices, events, now)
        started, ended, closed = None, None, None
        resolution = 'verified'
        if consumed:
            if (state.get('trialConsumedAt') and row.get('plan_type') == 'free'
                    and row.get('billing_mode') == 'none' and row.get('current_period_end')):
                started, ended = timestamp(state['trialConsumedAt']), timestamp(row['current_period_end'])
            elif (trialReview or {}).get('consumed') is True and isinstance(trialReview.get('reviewReference'), str) and trialReview['reviewReference'].strip():
                if row.get('status') == 'trial' and (not row.get('current_period_end') or timestamp(row['current_period_end']) > now):
                    raise ValueError('LIVE_TRIAL_DATES_REQUIRE_REVIEW')
                resolution = 'consumed_dates_unknown'
            else:
                raise ValueError('TRIAL_DATES_REQUIRE_REVIEW')
        if not paid and (row.get('billing_mode') not in ('none', None) or row.get('plan_type') in ('pro', 'annual')):
            raise ValueError('PAID_HISTORY_REQUIRES_REVIEW')
        if not paid and not consumed and (row.get('status') != 'none' or row.get('current_period_start') or row.get('current_period_end') or state.get('churn_snapshot')):
            raise ValueError('UNKNOWN_SERVICE_HISTORY')
        if not paid and not consumed and ((owner or {}).get('creditHistory') or row.get('admin_credit_generation')):
            raise ValueError('CREDIT_OR_ADMIN_HISTORY_REQUIRES_REVIEW')
        lifecycle = manual.get('lifecycleId')
        if paid:
            lifecycle = lifecycle or max(paid, key=lambda p: p.start).lifecycleId
        if lifecycle:
            lifecycle = str(UUID(lifecycle))
        if started and paid:
            closed = min(p.start for p in paid)
        patch = jsonValue(dict(plan_code='pro' if consumed or paid else None,
            lifecycle_id=lifecycle, trial_started_at=started, trial_ends_at=ended, trial_closed_at=closed,
            cancellation_requested_at=timestamp(manual.get('cancellationRequestedAt'))))
        facts = factsFromRecords(row | patch, owner, invoices, events, consumed=consumed, otherSubscriptions=otherSubscriptions)
        evaluateSubscription(facts, now)
        proposal.update(resolution=resolution, patch=patch, trialConsumed=consumed)
    except (ValueError, KeyError, TypeError, SubscriptionAuthorityUnavailable) as exc:
        proposal['reasons'] = [str(exc)]
    return proposal
