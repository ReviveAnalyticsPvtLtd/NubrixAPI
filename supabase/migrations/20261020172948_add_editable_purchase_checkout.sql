-- Additive purpose-neutral payment storage. No runtime gate: the refund worker always runs once deployed.
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

-- One slot is the thing a user can buy at most once at a time.
CREATE TABLE IF NOT EXISTS public.payment_slots (
 slot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id text REFERENCES public."Users"("userId"),
 kind text NOT NULL CONSTRAINT payment_slots_kind_check CHECK(kind IN ('pro_term','expert_addition','topup')),
 slot_key text NOT NULL CHECK(length(slot_key) BETWEEN 1 AND 200),
 settled_payment_id text CHECK(length(settled_payment_id) BETWEEN 1 AND 200),
 settled_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 erasure_request_id uuid,
 CHECK((user_id IS NULL) = (erasure_request_id IS NOT NULL)),
 CHECK((settled_payment_id IS NULL) = (settled_at IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS payment_slots_open_key_idx ON public.payment_slots(user_id,kind,slot_key)
 WHERE settled_payment_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS payment_slots_settled_payment_idx ON public.payment_slots(settled_payment_id)
 WHERE settled_payment_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS payment_slots_owner_idx ON public.payment_slots(user_id,slot_id);

CREATE TABLE IF NOT EXISTS public.payment_slot_controls (
 attempt_id uuid PRIMARY KEY REFERENCES public.billing_events(id),
 slot_id uuid NOT NULL REFERENCES public.payment_slots(slot_id),
 invoice_id uuid NOT NULL REFERENCES public."Invoices"(id),
 user_id text REFERENCES public."Users"("userId"),
 purpose text NOT NULL CHECK(purpose IN ('initial_purchase','renewal','expert_addition','topup')),
 draft_revision bigint CHECK(draft_revision BETWEEN 1 AND 9007199254740991),
 operation_revision bigint NOT NULL DEFAULT 0 CHECK(operation_revision BETWEEN 0 AND 9007199254740991),
 provider_order_id text,
 -- 'account_default' marks pre-rollout Orders created without a per-Order payment field.
 capture_policy text NOT NULL CONSTRAINT payment_slot_controls_capture_policy_check CHECK(capture_policy IN ('automatic','account_default')),
 superseded_by uuid REFERENCES public.payment_slot_controls(attempt_id),
 closed_at timestamptz,
 closed_reason text CHECK(closed_reason ~ '^[A-Z][A-Z0-9_]{0,63}$'),
 -- A superseded attempt stays in the recovery scan until this instant (newest payment created_at + window).
 recover_until timestamptz,
 erasure_request_id uuid,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK((user_id IS NULL) = (erasure_request_id IS NOT NULL)),
 CONSTRAINT payment_slot_controls_draft_initial_check CHECK(draft_revision IS NULL OR purpose='initial_purchase'),
 CHECK((closed_at IS NULL) = (closed_reason IS NULL)),
 CONSTRAINT payment_slot_controls_superseded_reason_check CHECK(superseded_by IS NULL OR closed_reason IS NOT DISTINCT FROM 'SUPERSEDED'),
 CHECK(superseded_by IS DISTINCT FROM attempt_id)
);
CREATE INDEX IF NOT EXISTS payment_slot_controls_owner_idx ON public.payment_slot_controls(user_id,attempt_id);
CREATE INDEX IF NOT EXISTS payment_slot_controls_slot_idx ON public.payment_slot_controls(slot_id,attempt_id);

CREATE TABLE IF NOT EXISTS public.payment_operations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id text REFERENCES public."Users"("userId"),
 attempt_id uuid NOT NULL REFERENCES public.payment_slot_controls(attempt_id),
 invoice_id uuid NOT NULL REFERENCES public."Invoices"(id),
 provider_payment_id text NOT NULL CHECK(length(provider_payment_id) BETWEEN 1 AND 200),
 kind text NOT NULL CONSTRAINT payment_operations_kind_check CHECK(kind IN ('refund')),
 reason text NOT NULL CONSTRAINT payment_operations_reason_check
   CHECK(reason IN ('duplicate','paid_after_close','not_deliverable','account_closed')),
 state text NOT NULL DEFAULT 'queued' CHECK(state IN
   ('queued','submission_started','outcome_unknown','submitted','succeeded','failed','cancelled','needs_attention')),
 amount bigint NOT NULL CHECK(amount>0),
 currency text NOT NULL CHECK(currency IN ('INR','USD')),
 dedupe_key text NOT NULL UNIQUE CONSTRAINT payment_operations_dedupe_key_check CHECK(dedupe_key='payment:'||kind||':'||provider_payment_id),
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
CREATE INDEX IF NOT EXISTS payment_operations_due_idx ON public.payment_operations(kind,id)
 WHERE state NOT IN ('succeeded','cancelled');
CREATE INDEX IF NOT EXISTS payment_operations_owner_idx ON public.payment_operations(user_id,id);

-- A pre-rollout attempt erased before controls existed can acquire refund-only authority.
-- Its immutable originals, not unauthenticated metadata or a fabricated control archive, prove it.
CREATE OR REPLACE FUNCTION private.exact_retained_payment_attempt(attempt_id uuid,invoice_id uuid,request_id uuid)
RETURNS boolean LANGUAGE plpgsql STABLE SET search_path='' AS $$
BEGIN
 RETURN EXISTS(SELECT 1 FROM public.billing_events a JOIN public."Invoices" i ON i.id=a.invoice_id
 JOIN private.retained_user_records ar ON ar.source_table='billing_events' AND ar.source_id=a.id AND ar.request_id=$3
 JOIN private.retained_user_records ir ON ir.source_table='Invoices' AND ir.source_id=i.id AND ir.request_id=ar.request_id
 JOIN private.retained_user_subjects s ON s.request_id=ar.request_id
 JOIN public.user_erasure_requests e ON e.id=s.request_id
 WHERE a.id=$1 AND i.id=$2 AND a.user_id IS NULL AND i."userId" IS NULL AND e.status='COMPLETED'
 AND a.metadata_json->>'erasureRequestId'=$3::text AND i.metadata_json->>'erasureRequestId'=$3::text
 AND ar.original_record->>'id'=a.id::text AND ir.original_record->>'id'=i.id::text
 AND ar.original_record->>'user_id'=s.original_user_id AND ir.original_record->>'userId'=s.original_user_id
 AND ar.original_record->>'invoice_id'=i.id::text AND a.event_category='payment_attempt'
 AND ar.original_record->>'event_category'='payment_attempt'
 AND a.provider_order_id IS NOT NULL AND ar.original_record->>'provider_order_id'=a.provider_order_id
 AND a.amount>0 AND ar.original_record->>'amount'=a.amount::text
 AND a.currency IN ('INR','USD') AND ar.original_record->>'currency'=a.currency
 AND i.currency=a.currency AND ir.original_record->>'currency'=a.currency
 AND coalesce(i.total_amount,i.amount)=a.amount
 AND coalesce(ir.original_record->>'total_amount',ir.original_record->>'amount')=a.amount::text
 AND a.metadata_json->'manualBilling'->>'purpose' IN ('initial_purchase','renewal','expert_addition','topup')
 AND ar.original_record->'metadata_json'->'manualBilling'->>'purpose'=a.metadata_json->'manualBilling'->>'purpose'
 AND coalesce(ar.original_record->'metadata_json'->'manualBilling'->>'capturePolicy','account_default') IN ('account_default','automatic')
 AND ar.record_hash=encode(sha256(convert_to(ar.original_record::text,'UTF8')),'hex')
 AND ir.record_hash=encode(sha256(convert_to(ir.original_record::text,'UTF8')),'hex')
 AND NOT EXISTS(SELECT 1 FROM private.retained_user_records cr WHERE cr.source_table='payment_slot_controls' AND cr.source_id=a.id));
END
$$;
REVOKE ALL ON FUNCTION private.exact_retained_payment_attempt(uuid,uuid,uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION private.exact_retained_payment_attempt(uuid,uuid,uuid) TO service_role;

-- Retained-only refund authority after erasure detached the original owner. Any full-refund
-- reason of the capture rules qualifies; the guard separately requires the ungranted capture.
CREATE OR REPLACE FUNCTION private.authorized_erased_payment(attempt_id uuid,invoice_id uuid,request_id uuid)
RETURNS boolean LANGUAGE plpgsql SET search_path='' AS $$
BEGIN
 RETURN EXISTS(SELECT 1 FROM public.payment_slot_controls c
 JOIN public.billing_events a ON a.id=c.attempt_id AND a.invoice_id=c.invoice_id
 JOIN public."Invoices" i ON i.id=c.invoice_id
 JOIN private.retained_user_records r ON r.request_id=c.erasure_request_id
   AND r.source_table='payment_slot_controls' AND r.source_id=c.attempt_id
 JOIN private.retained_user_subjects s ON s.request_id=r.request_id
 JOIN public.user_erasure_requests e ON e.id=s.request_id
 WHERE c.attempt_id=authorized_erased_payment.attempt_id AND c.invoice_id=authorized_erased_payment.invoice_id
   AND c.erasure_request_id=authorized_erased_payment.request_id
   AND c.user_id IS NULL AND a.user_id IS NULL AND i."userId" IS NULL
   AND a.metadata_json->>'erasureRequestId'=c.erasure_request_id::text
   AND r.original_record->>'user_id'=s.original_user_id
   AND r.original_record->>'invoice_id'=c.invoice_id::text
   AND e.status='COMPLETED') OR EXISTS(SELECT 1 FROM public.payment_slot_controls c
 JOIN public.payment_slots s ON s.slot_id=c.slot_id
 WHERE c.attempt_id=authorized_erased_payment.attempt_id AND c.invoice_id=authorized_erased_payment.invoice_id
 AND c.erasure_request_id=authorized_erased_payment.request_id AND c.user_id IS NULL
 AND c.slot_id=c.attempt_id AND s.slot_key='retained:'||c.attempt_id::text
 AND c.closed_reason='ACCOUNT_CLOSED' AND s.settled_payment_id IS NULL
 AND private.exact_retained_payment_attempt(c.attempt_id,c.invoice_id,c.erasure_request_id));
END $$;
REVOKE ALL ON FUNCTION private.authorized_erased_payment(uuid,uuid,uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION private.authorized_erased_payment(uuid,uuid,uuid) TO service_role;

-- Record fields are resolved per statement, so table-specific fields stay inside table-specific branches.
CREATE OR REPLACE FUNCTION private.guard_payment_storage() RETURNS trigger
LANGUAGE plpgsql SET search_path='' AS $$
DECLARE attempt public.billing_events%ROWTYPE; control public.payment_slot_controls%ROWTYPE;
        slot public.payment_slots%ROWTYPE; storage_source_id uuid; original jsonb; slot_kind text;
        supersession_changed boolean;
BEGIN
 IF TG_OP IN ('DELETE','TRUNCATE') THEN RAISE EXCEPTION 'payment financial controls cannot be deleted'; END IF;
 IF TG_TABLE_NAME='payment_slots' THEN storage_source_id=NEW.slot_id;
 ELSIF TG_TABLE_NAME='payment_slot_controls' THEN storage_source_id=NEW.attempt_id;
 ELSE storage_source_id=NEW.id; END IF;
 IF TG_OP='UPDATE' THEN
  -- An exact archived copy and a live leased erasure are required for detachment.
  IF NEW.user_id IS NULL AND OLD.user_id IS NOT NULL THEN
   original=to_jsonb(OLD);
   IF (to_jsonb(NEW)-'user_id'-'erasure_request_id') IS DISTINCT FROM
      (original-'user_id'-'erasure_request_id') THEN RAISE EXCEPTION 'erasure must preserve payment facts'; END IF;
   IF NOT EXISTS(SELECT 1 FROM private.retained_user_records a
     JOIN private.retained_user_subjects s ON s.request_id=a.request_id
     JOIN public.user_erasure_requests r ON r.id=s.request_id
     JOIN public."Users" u ON u."userId"=s.original_user_id
     WHERE a.source_table=TG_TABLE_NAME AND a.source_id=storage_source_id AND a.original_record=original
       AND r.id=NEW.erasure_request_id AND r.target_user_id=OLD.user_id AND s.original_user_id=OLD.user_id
       AND r.status='IN_PROGRESS' AND r.worker_id IS NOT NULL AND r.lease_expires_at>clock_timestamp()
       AND u."isBanned"=true) THEN RAISE EXCEPTION 'payment retention proof required'; END IF;
   RETURN NEW;
  END IF;
  IF NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.erasure_request_id IS DISTINCT FROM OLD.erasure_request_id THEN
   RAISE EXCEPTION 'payment ownership is immutable';
  END IF;
 END IF;

 IF TG_TABLE_NAME='payment_slots' THEN
  IF NEW.user_id IS NULL AND NEW.slot_key='retained:'||NEW.slot_id::text THEN
   SELECT * INTO attempt FROM public.billing_events WHERE id=NEW.slot_id;
   IF NEW.settled_payment_id IS NOT NULL OR NEW.settled_at IS NOT NULL
      OR NEW.kind IS DISTINCT FROM (CASE attempt.metadata_json->'manualBilling'->>'purpose'
         WHEN 'topup' THEN 'topup' WHEN 'expert_addition' THEN 'expert_addition' ELSE 'pro_term' END)
      OR NOT private.exact_retained_payment_attempt(attempt.id,attempt.invoice_id,NEW.erasure_request_id) THEN
    RAISE EXCEPTION 'retained payment authority required';
   END IF;
  ELSIF TG_OP='INSERT' AND (NEW.user_id IS NULL OR NEW.settled_payment_id IS NOT NULL OR NEW.settled_at IS NOT NULL) THEN
   RAISE EXCEPTION 'payment slot must open unsettled for its owner';
  END IF;
  IF TG_OP='UPDATE' AND (NEW.slot_id<>OLD.slot_id OR NEW.kind<>OLD.kind OR NEW.slot_key<>OLD.slot_key
     OR NEW.created_at<>OLD.created_at
     OR (OLD.settled_payment_id IS NOT NULL AND (NEW.settled_payment_id IS DISTINCT FROM OLD.settled_payment_id
         OR NEW.settled_at IS DISTINCT FROM OLD.settled_at))) THEN
   RAISE EXCEPTION 'payment slot identity or settlement is immutable';
  END IF;
  -- A slot settles only with the granted payment of an attempt whose control is in this slot.
  IF TG_OP='UPDATE' AND OLD.settled_payment_id IS NULL AND NEW.settled_payment_id IS NOT NULL AND NOT EXISTS(
     SELECT 1 FROM public.billing_events cap
     JOIN public.billing_events a ON a.event_category='payment_attempt' AND a.provider_order_id=cap.provider_order_id
       AND a.invoice_id=cap.invoice_id AND a.payment_status='captured'
     JOIN public.payment_slot_controls c ON c.attempt_id=a.id AND c.slot_id=NEW.slot_id
       AND c.provider_order_id=a.provider_order_id
     WHERE cap.provider_payment_id=NEW.settled_payment_id AND cap.event_type='payment.capture'
       AND cap.event_status='FINALIZED') THEN
   RAISE EXCEPTION 'payment slot settlement requires a captured payment of its own attempt';
  END IF;
  RETURN NEW;
 END IF;

 IF TG_OP='UPDATE' THEN
  IF NEW.attempt_id<>OLD.attempt_id OR NEW.invoice_id<>OLD.invoice_id THEN
   RAISE EXCEPTION 'payment ownership is immutable';
  END IF;
 END IF;
 SELECT * INTO attempt FROM public.billing_events WHERE id=NEW.attempt_id;
 IF TG_OP='INSERT' AND NEW.user_id IS NULL THEN
  IF TG_TABLE_NAME='payment_slot_controls' THEN
   SELECT * INTO slot FROM public.payment_slots WHERE slot_id=NEW.slot_id;
   IF NEW.slot_id<>NEW.attempt_id OR slot.slot_key IS DISTINCT FROM 'retained:'||NEW.attempt_id::text
     OR slot.erasure_request_id IS DISTINCT FROM NEW.erasure_request_id OR slot.user_id IS NOT NULL
     OR NEW.closed_reason IS DISTINCT FROM 'ACCOUNT_CLOSED' OR NEW.closed_at IS NULL
     OR NEW.superseded_by IS NOT NULL OR NEW.operation_revision<>0
     OR NOT private.exact_retained_payment_attempt(NEW.attempt_id,NEW.invoice_id,NEW.erasure_request_id)
     OR NEW.capture_policy IS DISTINCT FROM (SELECT coalesce(r.original_record->'metadata_json'->'manualBilling'->>'capturePolicy','account_default')
        FROM private.retained_user_records r WHERE r.source_table='billing_events' AND r.source_id=NEW.attempt_id AND r.request_id=NEW.erasure_request_id) THEN
    RAISE EXCEPTION 'retained payment authority required';
   END IF;
  ELSIF NEW.kind<>'refund' OR NOT private.authorized_erased_payment(NEW.attempt_id,NEW.invoice_id,NEW.erasure_request_id) THEN
   RAISE EXCEPTION 'retained payment authority required';
  END IF;
 END IF;

 IF TG_TABLE_NAME='payment_slot_controls' THEN
  IF TG_OP='INSERT' THEN
   SELECT * INTO slot FROM public.payment_slots WHERE slot_id=NEW.slot_id;
   slot_kind=CASE NEW.purpose WHEN 'expert_addition' THEN 'expert_addition' WHEN 'topup' THEN 'topup' ELSE 'pro_term' END;
   IF attempt.user_id IS DISTINCT FROM NEW.user_id OR attempt.invoice_id IS DISTINCT FROM NEW.invoice_id
      OR attempt.event_category IS DISTINCT FROM 'payment_attempt'
      OR attempt.metadata_json->'manualBilling'->>'purpose' IS DISTINCT FROM NEW.purpose
      OR NOT EXISTS(SELECT 1 FROM public."Invoices" i WHERE i.id=NEW.invoice_id AND i."userId" IS NOT DISTINCT FROM NEW.user_id)
      OR slot.user_id IS DISTINCT FROM NEW.user_id OR slot.settled_payment_id IS NOT NULL
      OR slot.kind IS DISTINCT FROM slot_kind THEN
    RAISE EXCEPTION 'payment owner, purpose, invoice or slot mismatch';
   END IF;
  END IF;
  -- The one permitted slot move: a renewal bound to its next term ISO(E) joins the open Pro term
  -- once E has passed unpaid (same owner, both slots unsettled), so one slot instance settles.
  IF TG_OP='UPDATE' AND (NEW.slot_id<>OLD.slot_id AND NOT EXISTS(SELECT 1 FROM public.payment_slots o
      JOIN public.payment_slots n ON n.slot_id=NEW.slot_id
      WHERE o.slot_id=OLD.slot_id AND NEW.user_id IS NOT NULL
        AND o.user_id=NEW.user_id AND n.user_id=NEW.user_id AND o.kind='pro_term' AND n.kind='pro_term'
        AND o.settled_payment_id IS NULL AND n.settled_payment_id IS NULL AND n.slot_key='open'
        AND (CASE WHEN o.slot_key ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+(Z|[+-][0-9]{2}:[0-9]{2})$'
                  THEN o.slot_key::timestamptz END) <= clock_timestamp())
    OR NEW.purpose<>OLD.purpose
    OR NEW.capture_policy<>OLD.capture_policy OR NEW.draft_revision IS DISTINCT FROM OLD.draft_revision
    OR NEW.created_at<>OLD.created_at
    OR (OLD.provider_order_id IS NOT NULL AND NEW.provider_order_id IS DISTINCT FROM OLD.provider_order_id)
    OR NEW.operation_revision<>OLD.operation_revision+1
    OR (OLD.superseded_by IS NOT NULL AND NEW.superseded_by IS DISTINCT FROM OLD.superseded_by)
    OR (OLD.closed_at IS NOT NULL AND (NEW.closed_at IS DISTINCT FROM OLD.closed_at
        OR NEW.closed_reason IS DISTINCT FROM OLD.closed_reason))) THEN
   RAISE EXCEPTION 'payment control revision or frozen identity mismatch';
  END IF;
  IF NEW.provider_order_id IS DISTINCT FROM attempt.provider_order_id THEN RAISE EXCEPTION 'payment order mismatch'; END IF;
  -- Only an open attempt can replace another, so supersession can never form a cycle.
  supersession_changed=NEW.superseded_by IS NOT NULL;
  IF supersession_changed AND TG_OP='UPDATE' THEN supersession_changed=NEW.superseded_by IS DISTINCT FROM OLD.superseded_by; END IF;
  IF supersession_changed AND NOT EXISTS(SELECT 1 FROM public.payment_slot_controls p
     WHERE p.attempt_id=NEW.superseded_by AND p.slot_id=NEW.slot_id AND p.user_id IS NOT DISTINCT FROM NEW.user_id
       AND p.erasure_request_id IS NOT DISTINCT FROM NEW.erasure_request_id AND p.closed_at IS NULL) THEN
   RAISE EXCEPTION 'payment supersession owner or slot mismatch';
  END IF;
 ELSE
  SELECT * INTO control FROM public.payment_slot_controls WHERE attempt_id=NEW.attempt_id;
  IF TG_OP='INSERT' AND NEW.user_id IS NOT NULL AND (attempt.user_id IS DISTINCT FROM NEW.user_id
     OR attempt.invoice_id IS DISTINCT FROM NEW.invoice_id OR attempt.event_category IS DISTINCT FROM 'payment_attempt'
     OR attempt.metadata_json->'manualBilling'->>'purpose' IS DISTINCT FROM control.purpose
     OR NOT EXISTS(SELECT 1 FROM public."Invoices" i WHERE i.id=NEW.invoice_id AND i."userId"=NEW.user_id)) THEN
   RAISE EXCEPTION 'payment owner, purpose or invoice mismatch';
  END IF;
  IF TG_OP='INSERT' AND (control.attempt_id IS NULL OR control.user_id IS DISTINCT FROM NEW.user_id
     OR control.erasure_request_id IS DISTINCT FROM NEW.erasure_request_id OR control.invoice_id IS DISTINCT FROM NEW.invoice_id
     OR control.provider_order_id IS NULL OR control.provider_order_id IS DISTINCT FROM attempt.provider_order_id
     OR attempt.amount IS NULL OR NEW.state<>'queued' OR NEW.currency IS DISTINCT FROM attempt.currency
     OR NEW.amount>attempt.amount) THEN
   RAISE EXCEPTION 'payment operation binding mismatch';
  END IF;
  -- Only money that was recorded on this attempt's Order and never granted is ever returned.
  IF TG_OP='INSERT' AND NOT EXISTS(SELECT 1 FROM public.billing_events cap
     WHERE cap.provider_payment_id=NEW.provider_payment_id AND cap.event_type='payment.capture'
       AND cap.event_status='REFUND_RESERVED' AND cap.invoice_id=NEW.invoice_id
       AND cap.provider_order_id=control.provider_order_id AND cap.currency=NEW.currency AND cap.amount>=NEW.amount) THEN
   RAISE EXCEPTION 'payment refund requires an ungranted recorded capture';
  END IF;
  IF TG_OP='UPDATE' THEN
   IF NEW.id<>OLD.id OR NEW.provider_payment_id<>OLD.provider_payment_id OR NEW.kind<>OLD.kind OR NEW.reason<>OLD.reason
       OR NEW.amount<>OLD.amount OR NEW.currency<>OLD.currency OR NEW.dedupe_key<>OLD.dedupe_key
       OR NEW.created_at<>OLD.created_at THEN
    RAISE EXCEPTION 'payment operation money is immutable';
   END IF;
   IF NEW.state<>OLD.state AND NOT (
     (OLD.state='queued' AND NEW.state IN ('submission_started','cancelled','needs_attention')) OR
     (OLD.state='submission_started' AND NEW.state IN ('outcome_unknown','submitted','succeeded','failed','needs_attention')) OR
     (OLD.state='outcome_unknown' AND NEW.state IN ('submitted','succeeded','failed','needs_attention')) OR
     (OLD.state='submitted' AND NEW.state IN ('outcome_unknown','succeeded','failed','needs_attention')) OR
     (OLD.state='failed' AND NEW.state IN ('queued','cancelled','needs_attention')) OR
     (OLD.state='needs_attention' AND NEW.state IN ('queued','outcome_unknown','submitted','succeeded','failed','cancelled'))
   ) THEN RAISE EXCEPTION 'invalid payment operation transition'; END IF;
  END IF;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION private.guard_payment_storage() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION private.guard_payment_storage() TO service_role;

DO $$ DECLARE name text; BEGIN
 FOREACH name IN ARRAY ARRAY['subscription_purchase_drafts','payment_slots','payment_slot_controls','payment_operations'] LOOP
  EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',name);
  EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC,anon,authenticated,service_role',name);
  EXECUTE format('GRANT SELECT,INSERT,UPDATE%s ON public.%I TO service_role',
    CASE WHEN name='subscription_purchase_drafts' THEN ',DELETE' ELSE '' END,name);
  IF name<>'subscription_purchase_drafts' THEN
   EXECUTE format('DROP TRIGGER IF EXISTS payment_storage_guard ON public.%I',name);
   EXECUTE format('CREATE TRIGGER payment_storage_guard BEFORE INSERT OR UPDATE OR DELETE ON public.%I
     FOR EACH ROW EXECUTE FUNCTION private.guard_payment_storage()',name);
   EXECUTE format('DROP TRIGGER IF EXISTS payment_no_truncate ON public.%I',name);
   EXECUTE format('CREATE TRIGGER payment_no_truncate BEFORE TRUNCATE ON public.%I
     FOR EACH STATEMENT EXECUTE FUNCTION private.guard_payment_storage()',name);
  END IF;
 END LOOP;
 -- The legacy-only fixture allowlist also executes before retention is installed.
 -- Real rollout installs this migration after the existing retention migration.
 IF to_regclass('private.retained_user_records') IS NOT NULL THEN
  ALTER TABLE private.retained_user_records DROP CONSTRAINT IF EXISTS retained_user_records_source_table_check;
  ALTER TABLE private.retained_user_records ADD CONSTRAINT retained_user_records_source_table_check
   CHECK(source_table IN ('billing_events','Invoices','payment_slots','payment_slot_controls','payment_operations'));
 END IF;
 -- The two refund emails join the durable notification pipeline (existing types unchanged).
 IF to_regclass('public.notification_deliveries') IS NOT NULL THEN
  ALTER TABLE public.notification_deliveries DROP CONSTRAINT IF EXISTS notification_deliveries_type_chk;
  ALTER TABLE public.notification_deliveries ADD CONSTRAINT notification_deliveries_type_chk
   CHECK (notification_type IN ('trial_expiry_warning','annual_renewal_upcoming',
    'monthly_renewal_ready','monthly_renewal_reminder','monthly_subscription_expired',
    'payment_receipt','monthly_cancellation_confirmation',
    'subscription_refund_initiated','subscription_refund_processed',
    'payment_refund_started','payment_refund_completed'));
 END IF;
END $$;

COMMENT ON TABLE public.subscription_purchase_drafts IS 'Mutable owned selection only; erased with account; no price, contact or entitlement.';
COMMENT ON TABLE public.payment_slots IS 'Retained purchase slots for every purpose. At most one unsettled slot per owner, kind and key.';
COMMENT ON TABLE public.payment_slot_controls IS 'Retained per-attempt payment controls for every purpose.';
COMMENT ON TABLE public.payment_operations IS 'Retained full refunds of never-granted captured payments, any purpose. Unresolved operations block final erasure. The refund worker always runs once deployed.';
