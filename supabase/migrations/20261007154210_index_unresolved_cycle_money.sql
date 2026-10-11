-- Renewal solicitation hold: index the received-but-unresolved money lookup.
--
-- _unresolvedCycleCaptureLocked runs under the per-user owner lock at intent
-- commit, enqueue and every ready/reminder/expiry submission. Without this
-- partial index it scans billing_events history. Forward-only expansion; no
-- data, provider, entitlement or financial mutation.
CREATE INDEX IF NOT EXISTS idx_billing_events_unresolved_cycle_money
ON public.billing_events (user_id, invoice_id)
WHERE (event_type = 'payment.capture' AND event_status IN ('OBSERVED', 'REQUIRES_RECONCILIATION'))
   OR (event_category = 'payment_attempt' AND payment_status IN ('authorized', 'captured'));
