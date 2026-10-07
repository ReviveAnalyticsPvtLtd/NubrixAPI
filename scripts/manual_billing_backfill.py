"""Operator-reviewed backfill for the manual monthly billing cutover.

Dry-run by default: reports candidate canonical rows, promotion reasons,
conflicting paid rows, and old/new field values WITHOUT writing. Applying
the mapping requires an explicit reviewed input file; there is no --force.

Mapping policy (audited design §6.2 / §8):
  * Canonical rows are promoted by verified live coverage / trial state /
    invoice linkage — NEVER by latest updated_at alone.
  * Conflicting live paid rows are reported, not guessed.
  * Monthly rows migrate monthly_recurring -> monthly_prepaid; cancelled
    monthly rows backfill renewal_opt_out = true and retain their reason.
  * Already-paid starts/ends/experts/usage/top-ups are preserved untouched.

Usage:
    uv run python scripts/manual_billing_backfill.py --dry-run
    uv run python scripts/manual_billing_backfill.py --apply reviewed_mapping.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_ACTIVE_STATUSES = {"active", "renewal_upcoming", "payment_pending", "trial"}


def _paidWindow(row: dict) -> bool:
    end = row.get("current_period_end")
    if not end:
        return False
    try:
        parsed = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed > datetime.now(timezone.utc)


def _canonicalReason(row: dict, invoiceLinkages: dict) -> str | None:
    """Verified reason a row is the canonical row; None when not a candidate."""
    status = (row.get("status") or "").lower()
    if status in _ACTIVE_STATUSES and _paidWindow(row):
        return "active_paid_or_trial"
    if status == "cancelled" and _paidWindow(row):
        return "cancelled_with_paid_time"
    if row.get("id") in invoiceLinkages:
        return "invoice_linked"
    if status in ("expired", "none", "cancelled"):
        # Terminal/no-plan rows are fallback candidates only when no better
        # verified candidate exists for that user (handled by the caller).
        return "terminal_fallback"
    return None


def buildCanonicalBackfillMapping(
    subscriptionRows: list[dict],
    invoiceRows: list[dict],
) -> dict[str, str]:
    """Deterministic user -> subscriptionId canonical mapping.

    Preference order (verified evidence, not timestamps):
      1. rows with an active paid/trial window
      2. rows with a paid invoice linkage
      3. terminal fallback rows (single-row users)
    Conflicts between same-preference paid rows are NOT resolved here; the
    dry-run report surfaces them for operator review.
    """
    linkages = {
        row.get("subscription_id")
        for row in invoiceRows or []
        if (row.get("status") or "").upper() == "PAID"
        and row.get("subscription_id")
    }
    byUser: dict[str, list[tuple[int, dict]]] = {}
    for row in subscriptionRows or []:
        reason = _canonicalReason(row, linkages)
        if reason is None:
            continue
        preference = {
            "active_paid_or_trial": 0,
            "cancelled_with_paid_time": 1,
            "invoice_linked": 2,
            "terminal_fallback": 3,
        }[reason]
        byUser.setdefault(row.get("user_id"), []).append((preference, row))

    mapping: dict[str, str] = {}
    for userId, candidates in byUser.items():
        candidates.sort(key=lambda pair: pair[0])
        best = candidates[0]
        conflicting = [
            row
            for preference, row in candidates
            if preference == best[0] and row["id"] != best[1]["id"]
        ]
        if conflicting:
            # Conflicting same-tier paid rows: DO NOT guess. Leave unmapped;
            # dryRunReport lists the conflict for operator resolution.
            continue
        mapping[userId] = best[1]["id"]
    return mapping


def _newFieldsFor(row: dict) -> dict:
    status = (row.get("status") or "").lower()
    billingMode = (row.get("billing_mode") or "none").lower()
    newFields: dict = {}
    if billingMode == "monthly_recurring":
        newFields["billing_mode"] = "monthly_prepaid"
    if status == "cancelled" and billingMode in (
        "monthly_recurring",
        "monthly_prepaid",
    ):
        newFields["renewal_opt_out"] = True
    return newFields


def dryRunReport(
    subscriptionRows: list[dict],
    invoiceRows: list[dict],
) -> dict:
    """Dry-run report: candidates, conflicts, old/new fields. No writes."""
    linkages = {
        row.get("subscription_id")
        for row in invoiceRows or []
        if (row.get("status") or "").upper() == "PAID"
        and row.get("subscription_id")
    }
    mapping = buildCanonicalBackfillMapping(subscriptionRows, invoiceRows)

    candidates = []
    conflicts = []
    byUser: dict[str, list[dict]] = {}
    for row in subscriptionRows or []:
        byUser.setdefault(row.get("user_id"), []).append(row)

    for userId, rows in byUser.items():
        paidRows = [
            row
            for row in rows
            if _canonicalReason(row, linkages)
            in ("active_paid_or_trial", "cancelled_with_paid_time")
        ]
        if len(paidRows) > 1:
            conflicts.append({
                "user_id": userId,
                "candidate_ids": [row.get("id") for row in paidRows],
                "resolution": "operator_review_required",
            })
        promotedId = mapping.get(userId)
        promotedRow = next(
            (row for row in rows if row.get("id") == promotedId), rows[0]
        )
        newFields = _newFieldsFor(promotedRow)
        candidates.append({
            "user_id": userId,
            "promote_id": promotedId,
            "reason": (_canonicalReason(promotedRow, linkages) or "terminal_fallback") if promotedId else "operator_review_required",
            "old_fields": {
                "billing_mode": promotedRow.get("billing_mode"),
                "status": promotedRow.get("status"),
                "current_period_start": promotedRow.get("current_period_start"),
                "current_period_end": promotedRow.get("current_period_end"),
            },
            "new_fields": newFields,
            "requires_paid_identity_mapping": _paidWindow(promotedRow) and (promotedRow.get('billing_mode') or '').lower() in ('monthly_recurring','monthly_prepaid'),
        })

    return {
        "dry_run": True,
        "would_promote": sum(item["promote_id"] is not None for item in candidates),
        "conflicts": conflicts,
        "candidates": candidates,
    }


def applyMapping(mappingFilePath: str) -> dict:
    """Apply an operator-reviewed mapping file. Writes require review sign-off."""
    with open(mappingFilePath, "r", encoding="utf-8") as handle:
        reviewed = json.load(handle)
    if not reviewed.get("reviewed") or not reviewed.get("approved_by"):
        raise SystemExit(
            "Refusing to apply: the mapping file must record "
            "reviewed=true and approved_by=<operator>."
        )
    databaseUrl = os.environ.get("DATABASE_URL")
    if not databaseUrl:
        raise SystemExit("DATABASE_URL is not configured.")
    import psycopg2

    connection = psycopg2.connect(
        databaseUrl, application_name="manual-billing-backfill"
    )
    promoted = 0
    try:
        from psycopg2.extras import RealDictCursor, Json
        from api.services.billing.manualBillingRepository import _advisoryKey, _utc
        import uuid
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            for entry in reviewed.get("mappings") or []:
                cursor.execute('select pg_advisory_xact_lock(%s)',(_advisoryKey(entry['user_id']),))
                cursor.execute('select * from public.subscriptions where user_id=%s order by id for update',(entry['user_id'],))
                owned=cursor.fetchall()
                canonical=next((row for row in owned if str(row['id'])==entry['promote_id']),None)
                if canonical is None: raise ValueError('BACKFILL_CANONICAL_OWNERSHIP_MISMATCH')
                if int(entry['expected_version']) != int(canonical['version']): raise ValueError('BACKFILL_STALE_VERSION')
                mode=(canonical.get('billing_mode') or 'none').lower()
                paidMonthly=mode in ('monthly_recurring','monthly_prepaid') and _paidWindow(canonical)
                if paidMonthly:
                    lifecycle=str(uuid.UUID(entry['lifecycle_id']))
                    period=str(uuid.UUID(entry['credit_period_id']))
                    cursor.execute('select * from public."Invoices" where id=%s for update',(entry['current_invoice_id'],))
                    invoice=cursor.fetchone()
                    if (not invoice or invoice['userId']!=entry['user_id'] or str(invoice['subscription_id'])!=entry['promote_id']
                        or invoice['status'].upper()!='PAID' or not invoice.get('razorpayPaymentId')
                        or invoice.get('billing_reason') not in ('initial_purchase','renewal')
                        or _utc(invoice['period_start'])!=_utc(canonical['current_period_start'])
                        or _utc(invoice['period_end'])!=_utc(canonical['current_period_end'])):
                        raise ValueError('BACKFILL_PAID_INTERVAL_EVIDENCE_REQUIRED')
                    metadata=invoice.get('metadata_json') or {}
                    metadata=json.loads(metadata) if isinstance(metadata,str) else dict(metadata)
                    metadata.setdefault('manualBilling',{}).update(lifecycleId=lifecycle,creditPeriodId=period,
                        billingMode='monthly_prepaid',purpose=invoice['billing_reason'],domains=canonical['subscribed_experts'],coverageState='active',
                        backfillApprovedBy=reviewed['approved_by'])
                    cursor.execute('update public."Invoices" set metadata_json=%s where id=%s',(Json(metadata),invoice['id']))
                    state=canonical.get('billing_state') or {}
                    state=json.loads(state) if isinstance(state,str) else dict(state)
                    existingLifecycle=state.get('manualBilling',{}).get('lifecycleId')
                    if existingLifecycle and existingLifecycle != lifecycle:
                        raise ValueError('BACKFILL_LIFECYCLE_REPLACEMENT_FORBIDDEN')
                    state.setdefault('manualBilling',{}).update(lifecycleId=lifecycle,paidFutureEnd=_utc(canonical['current_period_end']).isoformat())
                    cursor.execute('''select * from public."Invoices" where "userId"=%s and subscription_id=%s
                        and status='PAID' and billing_reason='renewal' and period_start >= %s order by period_start,id for update''',
                        (entry['user_id'],canonical['id'],canonical['current_period_end']))
                    future=[row for row in cursor.fetchall() if (row.get('metadata_json') or {}).get('manualBilling',{}).get('coverageState')!='revoked']
                    reviewedFuture=entry.get('future_coverage') or []
                    if len(future)>1 or {str(row['id']) for row in future} != {item['invoice_id'] for item in reviewedFuture}:
                        raise ValueError('BACKFILL_FUTURE_COVERAGE_REVIEW_REQUIRED')
                    from dateutil.relativedelta import relativedelta
                    for paid in future:
                        mapping=next(item for item in reviewedFuture if item['invoice_id']==str(paid['id']))
                        experts=mapping['experts']
                        if (not paid.get('razorpayPaymentId') or _utc(paid['period_start'])!=_utc(canonical['current_period_end'])
                            or _utc(paid['period_end'])!=_utc(paid['period_start'])+relativedelta(months=1)
                            or not 1<=len(experts)<=4 or len(set(experts))!=len(experts)):
                            raise ValueError('BACKFILL_FUTURE_INTERVAL_EVIDENCE_REQUIRED')
                        frozen=dict(paid.get('metadata_json') or {})
                        frozen.setdefault('manualBilling',{}).update(lifecycleId=lifecycle,
                            creditPeriodId=str(uuid.UUID(mapping['credit_period_id'])),billingMode='monthly_prepaid',
                            purpose='renewal',domains=experts,coverageState='scheduled',backfillApprovedBy=reviewed['approved_by'])
                        cursor.execute('update public."Invoices" set metadata_json=%s where id=%s',(Json(frozen),paid['id']))
                        state['manualBilling']['paidFutureEnd']=_utc(paid['period_end']).isoformat()
                    cursor.execute('update public.subscriptions set billing_state=%s where id=%s',(Json(state),canonical['id']))
                    cursor.execute('''update public.credit_balances set lifecycle_id=%s,credit_period_id=%s,
                        subscription_id=%s,period_start=%s,period_end=%s,balance_version=balance_version+1
                        where user_id=%s returning user_id''',
                        (lifecycle,period,canonical['id'],canonical['current_period_start'],canonical['current_period_end'],entry['user_id']))
                    if cursor.fetchone() is None: raise ValueError('BACKFILL_CREDIT_BALANCE_REQUIRED')
                # Demote first, avoiding partial-index collisions while swapping
                # authoritative rows. All history remains owned and readable.
                cursor.execute('update public.subscriptions set is_canonical=false where user_id=%s',(entry['user_id'],))
                cursor.execute(
                    """
                    update public.subscriptions
                    set is_canonical = (id = %s),
                        billing_mode = case
                            when billing_mode = 'monthly_recurring'
                                then 'monthly_prepaid'
                            else billing_mode
                        end,
                        renewal_opt_out = case
                            when id = %s and status = 'cancelled' and billing_mode in ('monthly_recurring','monthly_prepaid') then true
                            else renewal_opt_out
                        end,
                        auto_renew_enabled=false,
                        version=version+1
                    where user_id = %s
                    """,
                    (
                        entry["promote_id"],
                        entry["promote_id"],
                        entry["user_id"],
                    ),
                )
                promoted += cursor.rowcount
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"promoted": promoted}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only")
    parser.add_argument(
        "--apply",
        metavar="MAPPING_JSON",
        help="apply an operator-reviewed mapping file",
    )
    args = parser.parse_args(argv)

    if args.apply:
        result = applyMapping(args.apply)
        print(json.dumps(result, indent=2))
        return 0

    # Dry run (default): read rows and print the report without writing.
    databaseUrl = os.environ.get("DATABASE_URL")
    if not databaseUrl:
        print("DATABASE_URL not configured; dry-run read could not execute.")
        return 2
    import psycopg2
    from psycopg2.extras import RealDictCursor

    connection = psycopg2.connect(
        databaseUrl, application_name="manual-billing-backfill-dryrun"
    )
    try:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                select id, user_id, status, billing_mode,
                       current_period_start, current_period_end,
                       cancellation_reason
                from public.subscriptions
                """
            )
            subscriptionRows = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                select id, "userId", subscription_id, billing_reason, status
                from public."Invoices"
                """
            )
            invoiceRows = [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()

    report = dryRunReport(subscriptionRows, invoiceRows)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
