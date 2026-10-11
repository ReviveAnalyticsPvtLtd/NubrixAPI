-- Additive: each provider order belongs to exactly one canonical attempt.
-- Financial capture/audit rows may reference that order without owning it.
-- Existing conflicting mappings must be investigated; never delete money
-- history to satisfy this constraint. Index creation deliberately fails on
-- duplicate attempt ownership. Run before the recurring-field contraction.
CREATE UNIQUE INDEX IF NOT EXISTS idx_billing_events_owned_provider_order
    ON public.billing_events (provider, provider_order_id)
    WHERE event_category = 'payment_attempt'
      AND provider_order_id IS NOT NULL;
