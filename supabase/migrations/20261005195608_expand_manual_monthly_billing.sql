-- expand_manual_monthly_billing
--
-- Expansion migration for the manual monthly billing cutover.
--
-- Additive only: new billing mode, canonical flag, renewal opt-out, credit
-- identity columns, manual checkout attempt type, and invoice status case
-- normalization. NO columns are dropped here; the separately gated contract
-- migration (contract_recurring_billing_fields) performs the drops after
-- consumers are drained. Applying this migration does NOT change existing
-- paid time, credits, or top-ups.
--
-- Preconditions:
--   * subscriptions, "Invoices", billing_events, credit_balances,
--     notification_deliveries exist (docs/annualPlan/sql baseline or live
--     equivalent — verify live schema before applying).
--
-- Backfill notes:
--   * is_canonical is added NULL-defaulted to false and is NOT given a
--     unique index here; the operator-reviewed backfill (task 11 script)
--     promotes verified canonical rows first. The unique index is created
--     in add_manual_billing_transactions.sql with a guard that tolerates
--     conflicts during rollout by raising an actionable error.
--   * credit identity columns are added NULLable; NOT NULL is enforced only
--     in the contract phase after auditable backfill for all modes.

-- =============================================================================
-- 1. subscriptions: monthly_prepaid billing mode
-- =============================================================================
-- The live constraint name is subscriptions_billing_mode_check (see
-- docs/annualPlan/sql/008_add_none_trial_subscription_states.sql). It is
-- recreated with monthly_prepaid added; monthly_recurring stays allowed as a
-- legacy value until the contract migration retires it from active use.

ALTER TABLE public.subscriptions
    DROP CONSTRAINT IF EXISTS subscriptions_billing_mode_check;

ALTER TABLE public.subscriptions
    ADD CONSTRAINT subscriptions_billing_mode_check
    CHECK (billing_mode IN (
        'none',
        'monthly_recurring',   -- legacy, kept readable during rollout
        'monthly_prepaid',
        'annual_prepaid'
    ));

-- =============================================================================
-- 2. subscriptions: canonical flag + renewal opt-out
-- =============================================================================

ALTER TABLE public.subscriptions
    ADD COLUMN IF NOT EXISTS is_canonical BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE public.subscriptions
    ADD COLUMN IF NOT EXISTS renewal_opt_out BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN public.subscriptions.is_canonical IS
    'Marks the one authoritative subscription row per user; promoted by '
    'operator-reviewed backfill, never inferred from updated_at alone.';

COMMENT ON COLUMN public.subscriptions.renewal_opt_out IS
    'True when the user explicitly declined future renewal invoices and '
    'reminders. Independent of auto_renew_enabled and paid access.';

-- =============================================================================
-- 3. credit_balances: durable lifecycle/period/version identity
-- =============================================================================

ALTER TABLE public.credit_balances
    ADD COLUMN IF NOT EXISTS lifecycle_id UUID;

ALTER TABLE public.credit_balances
    ADD COLUMN IF NOT EXISTS credit_period_id UUID;

ALTER TABLE public.credit_balances
    ADD COLUMN IF NOT EXISTS balance_version BIGINT NOT NULL DEFAULT 0;

ALTER TABLE public.credit_balances
    DROP CONSTRAINT IF EXISTS credit_balances_balance_version_chk;

ALTER TABLE public.credit_balances
    ADD CONSTRAINT credit_balances_balance_version_chk
    CHECK (balance_version >= 0);

-- =============================================================================
-- 4. billing_events: authenticated_checkout attempt type
-- =============================================================================
-- token_debit history stays readable; manual checkout is a new type.

ALTER TABLE public.billing_events
    DROP CONSTRAINT IF EXISTS billing_events_payment_attempt_type_chk;

ALTER TABLE public.billing_events
    ADD CONSTRAINT billing_events_payment_attempt_type_chk
    CHECK (
        payment_attempt_type IS NULL OR payment_attempt_type IN (
            'token_debit',           -- historical recurring debits, read-only
            'checkout',              -- pre-manual era checkout attempts
            'authenticated_checkout',-- manual one-time checkout attempts
            'reconciliation_update'
        )
    );

-- =============================================================================
-- 5. Invoices: normalize status storage to the uppercase contract
-- =============================================================================
-- Existing code writes PAYMENT_PENDING (uppercase) at creation while some
-- scheduler paths wrote lowercase. Storage normalizes to the uppercase
-- contract: UPCOMING, PAYMENT_PENDING, PAID, VOID, EXPIRED, plus the
-- historically separate FAILED rows which are mapped to EXPIRED (failed
-- manual checkout is not a distinct financial state; retry uses a new
-- order/attempt). Update every exact-match consumer together (done in code
-- tasks); this migration fixes stored rows.

UPDATE public."Invoices"
   SET status = UPPER(status)
 WHERE status IS NOT NULL
   AND status <> UPPER(status);

UPDATE public."Invoices"
   SET status = 'EXPIRED'
 WHERE status IN ('FAILED');

-- =============================================================================
-- 6. Invoices: renewal uniqueness supports replacement after revoked coverage
-- =============================================================================
-- The old index excluded only VOID. After a full future refund (coverage
-- revoked), a replacement revision for that cycle must be allowed while a
-- valid PAID future invoice still blocks another payable one. Drop and
-- recreate with a predicate that keeps history but scopes the block to live
-- payable/valid-paid revisions. Coverage revocation is recorded in
-- metadata_json.manualBilling.coverageState = 'revoked' by the refund flow.

DROP INDEX IF EXISTS idx_invoices_renewal_period_unique;

CREATE UNIQUE INDEX idx_invoices_renewal_period_unique
    ON public."Invoices" (subscription_id, period_start, period_end, billing_reason)
    WHERE billing_reason = 'renewal'
      AND status IN ('UPCOMING', 'PAYMENT_PENDING', 'PAID')
      AND COALESCE(
            (metadata_json -> 'manualBilling' ->> 'coverageState') <> 'revoked',
            TRUE
          );

-- =============================================================================
-- 7. Deterministic identity lookups for lifecycle/cycle/revision/attempt
-- =============================================================================
-- Persisted queryable expression indexes. JSON metadata alone does not
-- enforce uniqueness; these do. Stored in the transactions migration when
-- they need function immutability; the simple lookups live here.

CREATE INDEX IF NOT EXISTS idx_subscriptions_canonical_lookup
    ON public.subscriptions (user_id)
    WHERE is_canonical = TRUE;

CREATE INDEX IF NOT EXISTS idx_subscriptions_renewal_opt_out
    ON public.subscriptions (renewal_opt_out)
    WHERE renewal_opt_out = TRUE;

-- =============================================================================
-- 8. Manual billing metadata read helper (immutable JSONB extraction)
-- =============================================================================
-- Expression indexes below need IMMUTABLE functions; JSONB ->> is immutable
-- for our purposes but the index expressions must be literal. A stable SQL
-- function keeps the Python repository honest about the stored cycle key.

CREATE OR REPLACE FUNCTION public.manual_billing_cycle_key(
    p_lifecycle TEXT,
    p_cycle TEXT
)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT p_lifecycle || ':' || p_cycle;
$$;