-- Additive only. No backfill, phase activation, financial rewrite or column drop.
SET LOCAL lock_timeout = '5s';
ALTER TABLE public.subscriptions
 ADD COLUMN IF NOT EXISTS plan_code text,
 ADD COLUMN IF NOT EXISTS lifecycle_id uuid,
 ADD COLUMN IF NOT EXISTS trial_started_at timestamptz,
 ADD COLUMN IF NOT EXISTS trial_ends_at timestamptz,
 ADD COLUMN IF NOT EXISTS trial_closed_at timestamptz,
 ADD COLUMN IF NOT EXISTS cancellation_requested_at timestamptz;

DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.subscriptions'::regclass AND conname='subscriptions_model_product_check') THEN
  ALTER TABLE public.subscriptions ADD CONSTRAINT subscriptions_model_product_check CHECK(plan_code IS NULL OR plan_code='pro');
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='public.subscriptions'::regclass AND conname='subscriptions_model_trial_check') THEN
  ALTER TABLE public.subscriptions ADD CONSTRAINT subscriptions_model_trial_check CHECK(
   (trial_started_at IS NULL AND trial_ends_at IS NULL AND trial_closed_at IS NULL) OR
   (trial_started_at IS NOT NULL AND trial_ends_at IS NOT NULL AND trial_ends_at>trial_started_at
    AND (trial_closed_at IS NULL OR trial_closed_at>=trial_started_at)));
 END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_subscription_model_evidence ON public.billing_events(subscription_id,occurred_at DESC,id DESC)
 WHERE event_type IN ('subscription.legacy_snapshot','subscription.model_backfill');

-- Preserve complete evidence once committed, including unknown legacy JSON.
-- Invoker security; no new client grants, policies or SECURITY DEFINER paths.
CREATE OR REPLACE FUNCTION public.guard_subscription_model_evidence() RETURNS trigger
 LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
 IF TG_OP='TRUNCATE' THEN
  IF EXISTS(SELECT 1 FROM public.billing_events WHERE event_type IN ('subscription.legacy_snapshot','subscription.model_backfill')) THEN
   RAISE EXCEPTION 'subscription model evidence is immutable';
  END IF;
  RETURN NULL;
 END IF;
 IF OLD.event_type IN ('subscription.legacy_snapshot','subscription.model_backfill') THEN
  RAISE EXCEPTION 'subscription model evidence is immutable';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.guard_subscription_model_evidence() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.guard_subscription_model_evidence() TO service_role;
DROP TRIGGER IF EXISTS trg_subscription_model_evidence ON public.billing_events;
CREATE TRIGGER trg_subscription_model_evidence BEFORE UPDATE OR DELETE ON public.billing_events
 FOR EACH ROW EXECUTE FUNCTION public.guard_subscription_model_evidence();
DROP TRIGGER IF EXISTS trg_subscription_model_evidence_truncate ON public.billing_events;
CREATE TRIGGER trg_subscription_model_evidence_truncate BEFORE TRUNCATE ON public.billing_events
 FOR EACH STATEMENT EXECUTE FUNCTION public.guard_subscription_model_evidence();
