-- Trial initialization/selection is authority, just like reviewed purchase evidence.
SET LOCAL lock_timeout = '5s';
CREATE OR REPLACE FUNCTION public.guard_subscription_model_evidence() RETURNS trigger
 LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
 IF TG_OP='TRUNCATE' THEN
  IF EXISTS(SELECT 1 FROM public.billing_events WHERE event_type IN
   ('subscription.legacy_snapshot','subscription.model_backfill','subscription.next_selection','subscription.expert_grant',
    'subscription.model_initialized','subscription.trial_started')) THEN
   RAISE EXCEPTION 'subscription model evidence is immutable';
  END IF;
  RETURN NULL;
 END IF;
 IF OLD.event_type IN
  ('subscription.legacy_snapshot','subscription.model_backfill','subscription.next_selection','subscription.expert_grant',
   'subscription.model_initialized','subscription.trial_started') THEN
  RAISE EXCEPTION 'subscription model evidence is immutable';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.guard_subscription_model_evidence() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.guard_subscription_model_evidence() TO service_role;
