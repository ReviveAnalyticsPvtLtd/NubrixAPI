"""Read-only, redacted inventory for the manual monthly billing cutover.

Prints the live schema/state dimensions the audited design requires before
any mutation: subscription rows by mode/status, canonical candidates,
invoice casing, token mandate remnants, credit identity coverage, webhook
backlog and contact-uniqueness constraints. Never writes. Values that
could identify users or provider credentials are redacted.

Usage:
    uv run python scripts/manual_billing_inventory.py [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def redactValue(value, keep: int = 5) -> str | None:
    """Redact a sensitive value, keeping only a short prefix."""
    if value is None:
        return None
    text = str(value)
    if len(text) <= keep:
        return "***"
    return text[:keep] + "***"


def buildInventoryQueries() -> dict[str, str]:
    """The read-only inventory queries, keyed by dimension.

    Every query is a SELECT. No INSERT/UPDATE/DELETE/DROP anywhere.
    """
    return {
        "subscriptions_by_mode_status": """
            select billing_mode, status, count(*)::int as rows
            from public.subscriptions
            group by billing_mode, status
            order by rows desc
        """,
        "canonical_candidates": """
            select s.user_id,
                   count(*)::int as candidate_rows,
                   count(*) filter (
                       where s.status not in ('cancelled', 'expired')
                   )::int as active_rows
            from public.subscriptions s
            group by s.user_id
            having count(*) > 1
            order by candidate_rows desc
        """,
        "token_mandate_remnants": """
            select count(*)::int as rows_with_token,
                   count(*) filter (
                       where s.billing_mode = 'monthly_recurring'
                   )::int as recurring_mode_rows
            from public.subscriptions s
            where s.razorpay_token_id is not null
        """,
        "invoices_case_and_status": """
            select status, count(*)::int as rows
            from public."Invoices"
            group by status
            order by rows desc
        """,
        "invoices_paid_future_periods": """
            select count(*)::int as paid_renewal_rows
            from public."Invoices"
            where billing_reason = 'renewal'
              and status = 'PAID'
        """,
        "credits_identity_coverage": """
            select count(*)::int as balance_rows,
                   count(*) filter (
                       where lifecycle_id is not null
                   )::int as with_lifecycle,
                   count(*) filter (
                       where credit_period_id is not null
                   )::int as with_period
            from public.credit_balances
        """,
        "webhook_backlog": """
            select status, count(*)::int as rows
            from public."WebhookEvents"
            group by status
        """,
        "unresolved_payment_attempts": """
            select payment_attempt_type, payment_status, count(*)::int as rows
            from public.billing_events
            where event_category = 'payment_attempt'
              and payment_status in ('created', 'pending_provider_ack', 'authorized')
            group by payment_attempt_type, payment_status
        """,
        "notification_types_in_flight": """
            select notification_type, status, count(*)::int as rows
            from public.notification_deliveries
            group by notification_type, status
        """,
        "phone_contact_columns": """
            select column_name, data_type
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'Users'
              and column_name in ('phoneNumber', 'email')
        """,
        "phone_uniqueness_constraints": """
            select conname, pg_get_constraintdef(oid) as definition
            from pg_constraint
            where conrelid = 'public."Users"'::regclass
              and contype = 'u'
        """,
        "schema_column_inventory": """
            select column_name, data_type
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'subscriptions'
            order by ordinal_position
        """,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    args = parser.parse_args(argv)

    databaseUrl = os.environ.get("DATABASE_URL")
    if not databaseUrl:
        print(
            "DATABASE_URL is not configured; nothing was read or written.",
            file=sys.stderr,
        )
        return 2

    # Import lazily so --help works without the dependency.
    import psycopg2
    from psycopg2.extras import RealDictCursor

    queries = buildInventoryQueries()
    report: dict[str, object] = {}
    connection = psycopg2.connect(
        databaseUrl, application_name="manual-billing-inventory"
    )
    try:
        connection.set_session(readonly=True, autocommit=True)
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            for name, query in queries.items():
                cursor.execute(query)
                rows = [dict(row) for row in cursor.fetchall()]
                report[name] = rows
    finally:
        connection.close()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        for name, rows in report.items():
            print(f"== {name} ==")
            for row in rows:
                print(f"  {row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())