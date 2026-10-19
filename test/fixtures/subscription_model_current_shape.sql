-- Synthetic current 24-column shape. No live rows or schema-history claims.
CREATE TABLE public."Users" ("userId" text PRIMARY KEY, "isBanned" boolean NOT NULL DEFAULT false);
CREATE TABLE public.subscriptions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id text NOT NULL REFERENCES public."Users"("userId") ON DELETE CASCADE,
 billing_mode text NOT NULL DEFAULT 'none' CHECK(billing_mode IN ('none','monthly_prepaid','annual_prepaid')),
 current_period_start timestamptz, current_period_end timestamptz, renewal_due_at timestamptz,
 auto_renew_enabled boolean NOT NULL DEFAULT false,
 payment_collection_mode text NOT NULL DEFAULT 'authenticated_checkout' CHECK(payment_collection_mode IN ('silent_token','authenticated_checkout')),
 status text NOT NULL DEFAULT 'none' CHECK(status IN ('none','trial','active','renewal_upcoming','payment_pending','past_due','suspended','cancelled','expired')),
 default_currency text NOT NULL DEFAULT 'INR', subscribed_experts jsonb NOT NULL DEFAULT '[]',
 domain_count integer NOT NULL DEFAULT 0 CHECK(domain_count BETWEEN 0 AND 4),
 pending_removals jsonb NOT NULL DEFAULT '[]', pending_additions jsonb NOT NULL DEFAULT '[]', billing_state jsonb NOT NULL DEFAULT '{}',
 cancellation_reason text, version integer NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 plan_type text NOT NULL DEFAULT 'none' CHECK(plan_type IN ('none','free','pro','annual')),
 erasure_pending boolean NOT NULL DEFAULT false, admin_credit_generation bigint NOT NULL DEFAULT 0,
 is_canonical boolean NOT NULL DEFAULT false, renewal_opt_out boolean NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX idx_subscriptions_one_canonical ON public.subscriptions(user_id) WHERE is_canonical;
CREATE INDEX idx_subscriptions_canonical_lookup ON public.subscriptions(user_id) WHERE is_canonical;
CREATE INDEX idx_subscriptions_user_id ON public.subscriptions(user_id);
CREATE INDEX idx_subscriptions_status ON public.subscriptions(status);
CREATE INDEX idx_subscriptions_plan_type ON public.subscriptions(plan_type);
CREATE INDEX idx_subscriptions_domain_count ON public.subscriptions(domain_count);
ALTER TABLE public.subscriptions ENABLE ROW LEVEL SECURITY;
CREATE FUNCTION public.update_subscriptions_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END $$;
CREATE TRIGGER trg_subscriptions_updated_at BEFORE UPDATE ON public.subscriptions
 FOR EACH ROW EXECUTE FUNCTION public.update_subscriptions_updated_at();
CREATE TABLE public."Invoices" (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "userId" text REFERENCES public."Users"("userId"),
 subscription_id uuid REFERENCES public.subscriptions(id), status text, billing_reason text,
 period_start timestamptz, period_end timestamptz, total_amount bigint, currency text,
 razorpay_order_id text, "razorpayPaymentId" text, "paidAt" timestamptz, metadata_json jsonb DEFAULT '{}'
);
CREATE TABLE public.billing_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id text REFERENCES public."Users"("userId"),
 subscription_id uuid REFERENCES public.subscriptions(id), invoice_id uuid REFERENCES public."Invoices"(id),
 event_category text NOT NULL, event_type text NOT NULL, event_status text, payment_status text,
 provider_order_id text, provider_payment_id text UNIQUE, amount bigint, currency text,
 idempotency_key text, metadata_json jsonb NOT NULL DEFAULT '{}', occurred_at timestamptz DEFAULT now()
);
CREATE UNIQUE INDEX billing_events_idempotency ON public.billing_events(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE TABLE public.credit_balances (
 user_id text PRIMARY KEY REFERENCES public."Users"("userId"), subscription_id uuid REFERENCES public.subscriptions(id),
 used_tokens bigint NOT NULL DEFAULT 0, remaining_tokens bigint NOT NULL DEFAULT 0,
 topup_tokens bigint NOT NULL DEFAULT 0, monthly_token_quota bigint NOT NULL DEFAULT 0
);
CREATE TABLE public.admin_free_trial_reductions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), subscription_id uuid REFERENCES public.subscriptions(id)
);
