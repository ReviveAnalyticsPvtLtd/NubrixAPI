-- Additive recovery readiness and retained-only correction authority.
SET LOCAL lock_timeout='5s';
ALTER TABLE public.subscription_purchase_checkout_controls
 ADD COLUMN IF NOT EXISTS retry_count integer NOT NULL DEFAULT 0 CHECK(retry_count>=0),
 ADD COLUMN IF NOT EXISTS next_retry_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 ADD COLUMN IF NOT EXISTS escalation_reference text;
CREATE TABLE IF NOT EXISTS private.purchase_recovery_runtime (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 worker_seen_at timestamptz NOT NULL
);
ALTER TABLE private.purchase_recovery_runtime ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON private.purchase_recovery_runtime FROM PUBLIC,anon,authenticated,service_role;
GRANT SELECT,INSERT,UPDATE ON private.purchase_recovery_runtime TO service_role;
CREATE OR REPLACE FUNCTION private.authorized_erased_purchase(attempt_id uuid,invoice_id uuid,request_id uuid)
RETURNS boolean LANGUAGE plpgsql SET search_path='' AS $$
BEGIN
 RETURN EXISTS(SELECT 1 FROM public.subscription_purchase_checkout_controls c
 JOIN public.billing_events a ON a.id=c.attempt_id AND a.invoice_id=c.invoice_id
 JOIN public."Invoices" i ON i.id=c.invoice_id
 JOIN private.retained_user_records r ON r.request_id=c.erasure_request_id
   AND r.source_table='subscription_purchase_checkout_controls' AND r.source_id=c.attempt_id
 JOIN private.retained_user_subjects s ON s.request_id=r.request_id
 JOIN public.user_erasure_requests e ON e.id=s.request_id
 WHERE c.attempt_id=authorized_erased_purchase.attempt_id AND c.invoice_id=authorized_erased_purchase.invoice_id
   AND c.erasure_request_id=authorized_erased_purchase.request_id
   AND c.user_id IS NULL AND a.user_id IS NULL AND i."userId" IS NULL
   AND c.cancellation_accepted_at IS NOT NULL
   AND a.metadata_json->>'erasureRequestId'=c.erasure_request_id::text
   AND r.original_record->>'user_id'=s.original_user_id
   AND r.original_record->>'invoice_id'=c.invoice_id::text
   AND r.original_record->>'cancellation_accepted_at' IS NOT NULL
   AND e.status='COMPLETED');
END $$;
REVOKE ALL ON FUNCTION private.authorized_erased_purchase(uuid,uuid,uuid) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION private.authorized_erased_purchase(uuid,uuid,uuid) TO service_role;
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
 IF TG_OP='INSERT' AND NEW.user_id IS NULL THEN
  IF TG_TABLE_NAME<>'subscription_purchase_operations' OR NEW.kind<>'compensation'
    OR NOT private.authorized_erased_purchase(NEW.attempt_id,NEW.invoice_id,NEW.erasure_request_id) THEN
    RAISE EXCEPTION 'retained purchase authority required';
  END IF;
 END IF;
 IF TG_OP='INSERT' AND NEW.user_id IS NOT NULL AND (attempt.user_id IS DISTINCT FROM NEW.user_id
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
     WHERE p.attempt_id=NEW.predecessor_attempt_id AND p.user_id IS NOT DISTINCT FROM NEW.user_id
       AND p.erasure_request_id IS NOT DISTINCT FROM NEW.erasure_request_id) THEN
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
