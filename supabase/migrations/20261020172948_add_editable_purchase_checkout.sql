-- Additive storage only. No capture/cancellation handlers are enabled here.
SET LOCAL lock_timeout = '5s';
CREATE SCHEMA IF NOT EXISTS private;

CREATE OR REPLACE FUNCTION private.valid_purchase_domains(value jsonb) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path='' AS $$
 SELECT CASE WHEN jsonb_typeof(value)='array' THEN
   jsonb_array_length(value) BETWEEN 1 AND 4
   AND value <@ '["banking","manufacturing","supplychain","telecom"]'::jsonb
   AND jsonb_array_length(value)=(SELECT count(DISTINCT item) FROM jsonb_array_elements(value) item)
 ELSE false END
$$;
REVOKE ALL ON FUNCTION private.valid_purchase_domains(jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION private.valid_purchase_domains(jsonb) TO service_role;

CREATE TABLE IF NOT EXISTS public.subscription_purchase_drafts (
 user_id text PRIMARY KEY REFERENCES public."Users"("userId"),
 revision bigint NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991),
 domains jsonb NOT NULL CHECK(private.valid_purchase_domains(domains)),
 billing_interval text NOT NULL CHECK(billing_interval IN ('month','year')),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS public.subscription_purchase_checkout_controls (
 attempt_id uuid PRIMARY KEY REFERENCES public.billing_events(id),
 invoice_id uuid NOT NULL REFERENCES public."Invoices"(id),
 user_id text REFERENCES public."Users"("userId"),
 draft_revision bigint NOT NULL CHECK(draft_revision BETWEEN 1 AND 9007199254740991),
 operation_revision bigint NOT NULL DEFAULT 0 CHECK(operation_revision BETWEEN 0 AND 9007199254740991),
 predecessor_attempt_id uuid REFERENCES public.subscription_purchase_checkout_controls(attempt_id),
 provider_order_id text,
 capture_policy text NOT NULL DEFAULT 'legacy' CHECK(capture_policy IN ('legacy','manual','automatic')),
 cancellation_requested_at timestamptz,
 cancellation_accepted_at timestamptz,
 erasure_request_id uuid,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK((user_id IS NULL) = (erasure_request_id IS NOT NULL)),
 CHECK(cancellation_accepted_at IS NULL OR
       (cancellation_requested_at IS NOT NULL AND cancellation_accepted_at>=cancellation_requested_at)),
 CHECK(predecessor_attempt_id IS DISTINCT FROM attempt_id)
);
CREATE INDEX IF NOT EXISTS purchase_controls_owner_idx ON public.subscription_purchase_checkout_controls(user_id,attempt_id);
CREATE TABLE IF NOT EXISTS public.subscription_purchase_operations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id text REFERENCES public."Users"("userId"),
 attempt_id uuid NOT NULL REFERENCES public.subscription_purchase_checkout_controls(attempt_id),
 invoice_id uuid NOT NULL REFERENCES public."Invoices"(id),
 provider_payment_id text NOT NULL CHECK(length(provider_payment_id) BETWEEN 1 AND 200),
 kind text NOT NULL CHECK(kind IN ('capture','compensation')),
 state text NOT NULL DEFAULT 'queued' CHECK(state IN
   ('queued','submission_started','outcome_unknown','submitted','succeeded','failed','cancelled','needs_attention')),
 amount bigint NOT NULL CHECK(amount>0),
 currency text NOT NULL CHECK(currency IN ('INR','USD')),
 dedupe_key text NOT NULL UNIQUE CHECK(dedupe_key='purchase:'||kind||':'||provider_payment_id),
 submission_evidence jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(submission_evidence)='object'),
 result jsonb NOT NULL DEFAULT '{}'::jsonb CHECK(jsonb_typeof(result)='object'),
 error_code text,
 escalation_reference text,
 retry_count integer NOT NULL DEFAULT 0 CHECK(retry_count>=0),
 next_retry_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 erasure_request_id uuid,
 UNIQUE(kind,provider_payment_id),
 CHECK((user_id IS NULL) = (erasure_request_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS purchase_operations_due_idx ON public.subscription_purchase_operations(kind,id)
 WHERE state NOT IN ('succeeded','cancelled');
CREATE INDEX IF NOT EXISTS purchase_operations_owner_idx ON public.subscription_purchase_operations(user_id,id);

CREATE OR REPLACE FUNCTION private.guard_purchase_storage() RETURNS trigger
LANGUAGE plpgsql SET search_path='' AS $$
DECLARE attempt public.billing_events%ROWTYPE; control public.subscription_purchase_checkout_controls%ROWTYPE;
        purchase_source_id uuid; original jsonb;
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN RAISE EXCEPTION 'purchase financial controls cannot be deleted'; END IF;
 IF TG_TABLE_NAME='subscription_purchase_checkout_controls' THEN purchase_source_id=NEW.attempt_id; ELSE purchase_source_id=NEW.id; END IF;
 IF TG_OP='UPDATE' THEN
  -- An exact archived copy and a live leased erasure are required for detachment.
  IF NEW.user_id IS NULL AND OLD.user_id IS NOT NULL THEN
   original=to_jsonb(OLD);
   IF (to_jsonb(NEW)-'user_id'-'erasure_request_id') IS DISTINCT FROM
      (original-'user_id'-'erasure_request_id') THEN RAISE EXCEPTION 'erasure must preserve purchase facts'; END IF;
   IF NOT EXISTS(SELECT 1 FROM private.retained_user_records a
     JOIN private.retained_user_subjects s ON s.request_id=a.request_id
     JOIN public.user_erasure_requests r ON r.id=s.request_id
     JOIN public."Users" u ON u."userId"=s.original_user_id
     WHERE a.source_table=TG_TABLE_NAME AND a.source_id=purchase_source_id AND a.original_record=original
       AND r.id=NEW.erasure_request_id AND r.target_user_id=OLD.user_id AND s.original_user_id=OLD.user_id
       AND r.status='IN_PROGRESS' AND r.worker_id IS NOT NULL AND r.lease_expires_at>clock_timestamp()
       AND u."isBanned"=true) THEN RAISE EXCEPTION 'purchase retention proof required'; END IF;
   RETURN NEW;
  END IF;
  IF NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.erasure_request_id IS DISTINCT FROM OLD.erasure_request_id
     OR NEW.attempt_id<>OLD.attempt_id OR NEW.invoice_id<>OLD.invoice_id THEN
   RAISE EXCEPTION 'purchase ownership is immutable';
  END IF;
 END IF;
 SELECT * INTO attempt FROM public.billing_events WHERE id=NEW.attempt_id;
 IF TG_OP='INSERT' AND (NEW.user_id IS NULL OR attempt.user_id IS DISTINCT FROM NEW.user_id
    OR attempt.invoice_id IS DISTINCT FROM NEW.invoice_id OR attempt.event_category<>'payment_attempt'
    OR attempt.metadata_json->'manualBilling'->>'purpose' IS DISTINCT FROM 'initial_purchase'
    OR NOT EXISTS(SELECT 1 FROM public."Invoices" i WHERE i.id=NEW.invoice_id AND i."userId"=NEW.user_id)) THEN
  RAISE EXCEPTION 'purchase owner or invoice mismatch';
 END IF;
 IF TG_TABLE_NAME='subscription_purchase_checkout_controls' THEN
  IF TG_OP='UPDATE' AND (NEW.draft_revision<>OLD.draft_revision
    OR NEW.predecessor_attempt_id IS DISTINCT FROM OLD.predecessor_attempt_id
    OR NEW.capture_policy<>OLD.capture_policy
    OR (OLD.provider_order_id IS NOT NULL AND NEW.provider_order_id IS DISTINCT FROM OLD.provider_order_id)
    OR NEW.operation_revision<>OLD.operation_revision+1
    OR (OLD.cancellation_requested_at IS NOT NULL AND NEW.cancellation_requested_at IS DISTINCT FROM OLD.cancellation_requested_at)
    OR (OLD.cancellation_accepted_at IS NOT NULL AND NEW.cancellation_accepted_at IS DISTINCT FROM OLD.cancellation_accepted_at)) THEN
   RAISE EXCEPTION 'purchase control revision or frozen identity mismatch';
  END IF;
  IF NEW.provider_order_id IS DISTINCT FROM attempt.provider_order_id THEN RAISE EXCEPTION 'purchase order mismatch'; END IF;
  IF NEW.predecessor_attempt_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.subscription_purchase_checkout_controls p
     WHERE p.attempt_id=NEW.predecessor_attempt_id AND p.user_id=NEW.user_id) THEN
   RAISE EXCEPTION 'purchase predecessor owner mismatch';
  END IF;
 ELSE
  SELECT * INTO control FROM public.subscription_purchase_checkout_controls WHERE attempt_id=NEW.attempt_id;
  IF TG_OP='INSERT' AND (control.user_id IS DISTINCT FROM NEW.user_id OR control.invoice_id<>NEW.invoice_id
     OR control.provider_order_id IS NULL OR control.provider_order_id IS DISTINCT FROM attempt.provider_order_id
     OR attempt.amount IS NULL OR NEW.state<>'queued' OR NEW.currency IS DISTINCT FROM attempt.currency OR NEW.amount>attempt.amount) THEN
   RAISE EXCEPTION 'purchase operation binding mismatch';
  END IF;
  IF TG_OP='UPDATE' THEN
   IF NEW.id<>OLD.id OR NEW.provider_payment_id<>OLD.provider_payment_id OR NEW.kind<>OLD.kind OR NEW.amount<>OLD.amount
       OR NEW.currency<>OLD.currency OR NEW.dedupe_key<>OLD.dedupe_key OR NEW.created_at<>OLD.created_at THEN
    RAISE EXCEPTION 'purchase operation money is immutable';
   END IF;
   IF NEW.state<>OLD.state AND NOT (
     (OLD.state='queued' AND NEW.state IN ('submission_started','cancelled','needs_attention')) OR
     (OLD.state='submission_started' AND NEW.state IN ('outcome_unknown','submitted','succeeded','failed','needs_attention')) OR
     (OLD.state='outcome_unknown' AND NEW.state IN ('submitted','succeeded','failed','needs_attention')) OR
     (OLD.state='submitted' AND NEW.state IN ('outcome_unknown','succeeded','failed','needs_attention')) OR
     (OLD.state='failed' AND NEW.state IN ('queued','cancelled','needs_attention')) OR
     (OLD.state='needs_attention' AND NEW.state IN ('queued','outcome_unknown','submitted','succeeded','failed','cancelled'))
   ) THEN RAISE EXCEPTION 'invalid purchase operation transition'; END IF;
  END IF;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION private.guard_purchase_storage() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION private.guard_purchase_storage() TO service_role;

DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['subscription_purchase_drafts','subscription_purchase_checkout_controls','subscription_purchase_operations'] LOOP
  EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',name);
  EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC,anon,authenticated,service_role',name);
  EXECUTE format('GRANT SELECT,INSERT,UPDATE%s ON public.%I TO service_role',
    CASE WHEN name='subscription_purchase_drafts' THEN ',DELETE' ELSE '' END,name);
  IF name<>'subscription_purchase_drafts' THEN
   EXECUTE format('DROP TRIGGER IF EXISTS purchase_storage_guard ON public.%I',name);
   EXECUTE format('CREATE TRIGGER purchase_storage_guard BEFORE INSERT OR UPDATE OR DELETE ON public.%I
     FOR EACH ROW EXECUTE FUNCTION private.guard_purchase_storage()',name);
   EXECUTE format('DROP TRIGGER IF EXISTS purchase_no_truncate ON public.%I',name);
   EXECUTE format('CREATE TRIGGER purchase_no_truncate BEFORE TRUNCATE ON public.%I
     FOR EACH STATEMENT EXECUTE FUNCTION private.guard_purchase_storage()',name);
  END IF;
 END LOOP;
 -- The legacy-only fixture allowlist also executes before retention is installed.
 -- Real rollout installs this migration after the existing retention migration.
 IF to_regclass('private.retained_user_records') IS NOT NULL THEN
  ALTER TABLE private.retained_user_records DROP CONSTRAINT IF EXISTS retained_user_records_source_table_check;
  ALTER TABLE private.retained_user_records ADD CONSTRAINT retained_user_records_source_table_check
   CHECK(source_table IN ('billing_events','Invoices','subscription_purchase_checkout_controls','subscription_purchase_operations'));
 END IF;
END $$;

COMMENT ON TABLE public.subscription_purchase_drafts IS 'Mutable owned selection only; erased with account; no price, contact or entitlement.';
COMMENT ON TABLE public.subscription_purchase_operations IS 'Retained financial operation controls. Unresolved operations block final erasure. Handlers remain rollout-gated.';
