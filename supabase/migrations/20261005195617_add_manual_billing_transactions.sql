-- add_manual_billing_transactions
--
-- Transaction migration for manual monthly billing: durable uniqueness for
-- the identities that make checkout, finalization, activation, refund and
-- notification intents exactly-once. Follows expand_manual_monthly_billing.
--
-- Additive only (indexes/constraints); no columns dropped. Real
-- mutation/concurrency evidence against a disposable database belongs to the
-- opt-in integration suite (task 11).

-- =============================================================================
-- 1. One canonical subscription row per application user
-- =============================================================================
-- Backfill must have promoted exactly one row per user (operator-reviewed
-- dry-run in scripts/manual_billing_backfill.py). Conflicting live rows
-- abort with an actionable error instead of silently picking one.

DO $$
DECLARE
    conflict_count INTEGER;
BEGIN
    SELECT count(*) INTO conflict_count
    FROM (
        SELECT user_id
        FROM public.subscriptions
        WHERE is_canonical = TRUE
        GROUP BY user_id
        HAVING count(*) > 1
    ) conflicts;

    IF conflict_count > 0 THEN
        RAISE EXCEPTION
            'MANUAL_BILLING_CANONICAL_CONFLICT: % user(s) have multiple canonical subscription rows. Resolve them with the reviewed backfill mapping before applying this migration.',
            conflict_count;
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_subscriptions_one_canonical
    ON public.subscriptions (user_id)
    WHERE is_canonical = TRUE;

-- =============================================================================
-- 2. One live attempt per invoice revision
-- =============================================================================
-- A stored, deterministic cycle/revision key on the attempt row (kept inside
-- metadata_json.manualBilling by the repository) is surfaced through an
-- immutable expression so the partial unique index can enforce "one live
-- attempt per revision". Attempts are transactionally closed (payment_status
-- moved out of the live set) before a replacement is created — the predicate
-- uses stored state, never now().

DROP INDEX IF EXISTS idx_billing_events_live_attempt_revision;

CREATE UNIQUE INDEX idx_billing_events_live_attempt_revision
    ON public.billing_events (
        user_id,
        invoice_id,
        (COALESCE(metadata_json -> 'manualBilling' ->> 'purpose', '')),
        (COALESCE(metadata_json -> 'manualBilling' ->> 'lifecycleId', '')),
        (COALESCE(metadata_json -> 'manualBilling' ->> 'cycleId', '')),
        (COALESCE(metadata_json -> 'manualBilling' ->> 'revision', '0'))
    )
    WHERE event_category = 'payment_attempt'
      AND payment_attempt_type = 'authenticated_checkout'
      AND payment_status IN ('created', 'pending_provider_ack', 'authorized');

-- =============================================================================
-- 3. One live initial-purchase intent per user
-- =============================================================================
-- Partial unique owner identity for open initial intents; a replacement
-- closes the old revision durably first.

DROP INDEX IF EXISTS idx_billing_events_live_initial_intent;

CREATE UNIQUE INDEX idx_billing_events_live_initial_intent
    ON public.billing_events (user_id)
    WHERE event_category = 'payment_attempt'
      AND payment_attempt_type = 'authenticated_checkout'
      AND payment_status IN ('created', 'pending_provider_ack', 'authorized')
      AND COALESCE(metadata_json -> 'manualBilling' ->> 'purpose', '') = 'initial_purchase';

-- =============================================================================
-- 4. Exactly-once financial/lifecycle operations
-- =============================================================================
-- operation_key is the durable identity for grant/activation/refund-closure/
-- notification-milestone operations. The Python repository inserts one row
-- per operation under a per-user advisory lock; the unique index is the
-- replay guard that survives concurrent workers.

DROP INDEX IF EXISTS idx_billing_events_operation_key;

CREATE UNIQUE INDEX idx_billing_events_operation_key
    ON public.billing_events (
        (COALESCE(metadata_json -> 'manualBilling' ->> 'operationKey', ''))
    )
    WHERE COALESCE(metadata_json -> 'manualBilling' ->> 'operationKey', '') <> '';

-- Canonical operation keys (documentation of the contract; see
-- manualBillingRepository):
--   finalize:{invoiceId}                                  one grant per invoice
--   activate:{lifecycleId}:{invoiceId}:{periodStart}      one activation
--   creditop:{operationId}                                one credit mutation
--   refund-intent:{userId}:{invoiceId}:{intervalStart}   one closing refund
--   notify:{logicalNotificationKey}                       one email milestone

-- =============================================================================
-- 5. Notification milestone lookup support
-- =============================================================================
-- Renewal email identity is lifecycle + target cycle + milestone (independent
-- of invoice revision or sweep date); transactional receipts use the provider
-- payment id. The notification_deliveries table itself is extended in
-- extend_monthly_notifications.sql (task 9); this index lets the bridge find
-- the committed intent idempotently.

CREATE INDEX IF NOT EXISTS idx_billing_events_notification_intent
    ON public.billing_events (
        (COALESCE(metadata_json -> 'manualBilling' ->> 'operationKey', ''))
    )
    WHERE event_category = 'notification'
      AND COALESCE(metadata_json -> 'manualBilling' ->> 'operationKey', '') LIKE 'notify:%';
