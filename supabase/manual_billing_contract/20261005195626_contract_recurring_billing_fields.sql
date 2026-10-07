-- contract_recurring_billing_fields
--
-- SEPARATELY GATED contract migration. Applying all migrations blindly must
-- not destroy unknown active mandates: this migration checks live
-- compatibility preconditions FIRST and raises an actionable exception when
-- the system is unsafe to contract.
--
-- Preconditions (verified by the DO block below):
--   1. No subscription row still stores a Razorpay token mandate. Retiring
--      live mandates is a supported provider-side cutover operation that
--      must complete BEFORE this migration runs.
--   2. No monthly_recurring row remains un-migrated to monthly_prepaid.
--   3. No old worker can still read the dropped columns (deployment drain
--      is an operational precondition; the SQL guard covers data state).
--
-- This file is NOT auto-applied to production. Rollback cannot recreate
-- retired mandates: export precondition evidence before applying.

DO $$
DECLARE
    active_tokens INTEGER;
    unmigrated_monthly INTEGER;
BEGIN
    active_tokens := 0;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='public'
               AND table_name='subscriptions' AND column_name='razorpay_token_id') THEN
        EXECUTE 'SELECT count(*) FROM public.subscriptions WHERE razorpay_token_id IS NOT NULL'
        INTO active_tokens;
    END IF;

    IF active_tokens > 0 THEN
        RAISE EXCEPTION
            'CONTRACT_PRECONDITION_FAILED: % subscription row(s) still store Razorpay token mandates. Retire live mandates through the supported provider cutover before dropping recurring columns.',
            active_tokens;
    END IF;

    SELECT count(*) INTO unmigrated_monthly
    FROM public.subscriptions
    WHERE billing_mode = 'monthly_recurring';

    IF unmigrated_monthly > 0 THEN
        RAISE EXCEPTION
            'CONTRACT_PRECONDITION_FAILED: % subscription row(s) still use billing_mode=monthly_recurring. Run the reviewed backfill mapping to monthly_prepaid first.',
            unmigrated_monthly;
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.subscriptions s
        LEFT JOIN public.credit_balances c ON c.user_id=s.user_id
        WHERE s.is_canonical AND s.billing_mode='monthly_prepaid'
          AND s.current_period_end > now()
          AND (c.lifecycle_id IS NULL OR c.credit_period_id IS NULL
               OR c.subscription_id IS DISTINCT FROM s.id
               OR c.period_start IS DISTINCT FROM s.current_period_start
               OR c.period_end IS DISTINCT FROM s.current_period_end)
    ) THEN
        RAISE EXCEPTION 'CONTRACT_PRECONDITION_FAILED: paid monthly credit identities and dates require reviewed backfill';
    END IF;
END
$$;

-- =============================================================================
-- Contracted column drops (customer/token/anchor/failure escalation)
-- =============================================================================
-- Razorpay still stores historical transaction records; dropping local
-- columns deletes nothing at the provider and assumes no supported Customer
-- deletion endpoint. Historical token_debit billing_events rows remain
-- readable; the columns themselves are runtime-consumed nowhere.

DROP INDEX IF EXISTS idx_subscriptions_razorpay_customer;
DROP INDEX IF EXISTS idx_subscriptions_razorpay_token;

ALTER TABLE public.subscriptions
    DROP COLUMN IF EXISTS razorpay_customer_id,
    DROP COLUMN IF EXISTS razorpay_token_id,
    DROP COLUMN IF EXISTS subscription_anchor_day,
    DROP COLUMN IF EXISTS recurring_failures;

-- The billing-mode check contracts monthly_recurring out of the allowed set
-- for NEW/UPDATED rows; historical rows were migrated by precondition 2.

ALTER TABLE public.subscriptions
    DROP CONSTRAINT IF EXISTS subscriptions_billing_mode_check;

ALTER TABLE public.subscriptions
    ADD CONSTRAINT subscriptions_billing_mode_check
    CHECK (billing_mode IN ('none', 'monthly_prepaid', 'annual_prepaid'));
