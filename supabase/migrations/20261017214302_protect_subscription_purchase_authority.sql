-- Extend append-only evidence protection to next selections and accepted grants.
-- Existing invoice quotes, amounts and provider identities are not rewritten.
SET LOCAL lock_timeout = '5s';
CREATE INDEX IF NOT EXISTS idx_subscription_purchase_authority
 ON public.billing_events(subscription_id,event_type,occurred_at DESC,id DESC)
 WHERE event_type IN ('subscription.next_selection','subscription.expert_grant');

CREATE OR REPLACE FUNCTION public.guard_subscription_model_evidence() RETURNS trigger
 LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
 IF TG_OP='TRUNCATE' THEN
  IF EXISTS(SELECT 1 FROM public.billing_events WHERE event_type IN
   ('subscription.legacy_snapshot','subscription.model_backfill','subscription.next_selection','subscription.expert_grant')) THEN
   RAISE EXCEPTION 'subscription model evidence is immutable';
  END IF;
  RETURN NULL;
 END IF;
 IF OLD.event_type IN
  ('subscription.legacy_snapshot','subscription.model_backfill','subscription.next_selection','subscription.expert_grant') THEN
  RAISE EXCEPTION 'subscription model evidence is immutable';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.guard_subscription_model_evidence() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.guard_subscription_model_evidence() TO service_role;
