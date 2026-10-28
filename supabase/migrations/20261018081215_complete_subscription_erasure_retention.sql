-- Preserve exact original audits under the explicitly selected retention policy.
-- Additive schema only; no live account is copied, deleted or erased here.
SET LOCAL lock_timeout = '5s';
CREATE SCHEMA IF NOT EXISTS private;
CREATE TABLE private.retained_user_subjects (
 request_id uuid PRIMARY KEY REFERENCES public.user_erasure_requests(id),
 original_user_id text NOT NULL UNIQUE,
 policy_code text NOT NULL CHECK(policy_code='ORIGINAL_AUDITS_RETAINED'),
 retained_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE private.retained_user_records (
 request_id uuid NOT NULL REFERENCES private.retained_user_subjects(request_id),
 source_table text NOT NULL CHECK(source_table IN ('billing_events','Invoices')),
 source_id uuid NOT NULL,
 original_record jsonb NOT NULL CHECK(jsonb_typeof(original_record)='object'),
 record_hash text NOT NULL CHECK(record_hash=encode(sha256(convert_to(original_record::text,'UTF8')),'hex')),
 retained_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(source_table,source_id)
);
CREATE INDEX retained_user_records_request_idx ON private.retained_user_records(request_id);
ALTER TABLE private.retained_user_subjects ENABLE ROW LEVEL SECURITY;
ALTER TABLE private.retained_user_records ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON SCHEMA private FROM PUBLIC,anon,authenticated;
REVOKE ALL ON private.retained_user_subjects,private.retained_user_records FROM PUBLIC,anon,authenticated,service_role;
GRANT USAGE ON SCHEMA private TO service_role;
GRANT SELECT,INSERT ON private.retained_user_subjects,private.retained_user_records TO service_role;

CREATE FUNCTION private.guard_retained_erasure_records() RETURNS trigger
 LANGUAGE plpgsql SET search_path='' AS $$
BEGIN RAISE EXCEPTION 'retained erasure evidence is immutable'; END $$;
REVOKE ALL ON FUNCTION private.guard_retained_erasure_records() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION private.guard_retained_erasure_records() TO service_role;
CREATE TRIGGER retained_subjects_immutable BEFORE UPDATE OR DELETE ON private.retained_user_subjects
 FOR EACH ROW EXECUTE FUNCTION private.guard_retained_erasure_records();
CREATE TRIGGER retained_records_immutable BEFORE UPDATE OR DELETE ON private.retained_user_records
 FOR EACH ROW EXECUTE FUNCTION private.guard_retained_erasure_records();
CREATE TRIGGER retained_subjects_no_truncate BEFORE TRUNCATE ON private.retained_user_subjects
 FOR EACH STATEMENT EXECUTE FUNCTION private.guard_retained_erasure_records();
CREATE TRIGGER retained_records_no_truncate BEFORE TRUNCATE ON private.retained_user_records
 FOR EACH STATEMENT EXECUTE FUNCTION private.guard_retained_erasure_records();

CREATE OR REPLACE FUNCTION public.guard_subscription_model_evidence() RETURNS trigger
 LANGUAGE plpgsql SET search_path='' AS $$
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
  -- Relocation is allowed only after an exact immutable copy, during a leased
  -- erasure of the same frozen subject. UPDATE is never allowed.
  IF TG_OP='DELETE' AND EXISTS(
   SELECT 1 FROM private.retained_user_records a
   JOIN private.retained_user_subjects s ON s.request_id=a.request_id
   JOIN public.user_erasure_requests r ON r.id=s.request_id
   JOIN public."Users" u ON u."userId"=s.original_user_id
   WHERE a.source_table='billing_events' AND a.source_id=OLD.id
    AND a.original_record=to_jsonb(OLD)
    AND r.target_user_id=s.original_user_id AND r.status='IN_PROGRESS'
    AND r.worker_id IS NOT NULL AND r.lease_expires_at>clock_timestamp()
    AND u."isBanned"=true
    AND (OLD.user_id=s.original_user_id OR (OLD.user_id IS NULL AND EXISTS(
     SELECT 1 FROM public.subscriptions p WHERE p.id=OLD.subscription_id AND p.user_id=s.original_user_id)))
    AND NOT EXISTS(SELECT 1 FROM public.subscriptions p WHERE p.user_id=s.original_user_id AND NOT p.erasure_pending)
  ) THEN RETURN OLD; END IF;
  RAISE EXCEPTION 'subscription model evidence is immutable';
 END IF;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.guard_subscription_model_evidence() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.guard_subscription_model_evidence() TO service_role;

CREATE FUNCTION private.guard_erased_user_identity() RETURNS trigger
 LANGUAGE plpgsql SET search_path='' AS $$
BEGIN
 IF (TG_OP='INSERT' OR NEW."userId" IS DISTINCT FROM OLD."userId" OR NOT NEW."isBanned")
  AND (EXISTS(SELECT 1 FROM private.retained_user_subjects WHERE original_user_id=NEW."userId")
   OR EXISTS(SELECT 1 FROM public.user_erasure_requests WHERE target_user_id=NEW."userId" AND status<>'COMPLETED')) THEN
  RAISE EXCEPTION 'ERASED_ACCOUNT_IDENTITY';
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION private.guard_erased_user_identity() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION private.guard_erased_user_identity() TO service_role;
CREATE TRIGGER guard_erased_user_identity BEFORE INSERT OR UPDATE ON public."Users"
 FOR EACH ROW EXECUTE FUNCTION private.guard_erased_user_identity();

ALTER TABLE public."WebhookEvents" ADD COLUMN erasure_request_id uuid REFERENCES public.user_erasure_requests(id);
CREATE FUNCTION private.erasure_request_for_webhook(webhook_user_id text,webhook_payload jsonb)
 RETURNS uuid LANGUAGE sql STABLE SET search_path='' AS $$
 SELECT r.id FROM public.user_erasure_requests r
 LEFT JOIN private.retained_user_subjects s ON s.request_id=r.id
 WHERE coalesce(s.original_user_id,r.target_user_id) IN (SELECT btrim(value) FROM unnest(ARRAY[webhook_user_id,
  webhook_payload#>>'{payload,payment,entity,notes,userId}',webhook_payload#>>'{payload,payment,entity,notes,user_id}',
  webhook_payload#>>'{payload,order,entity,notes,userId}',webhook_payload#>>'{payload,order,entity,notes,user_id}',
  webhook_payload#>>'{payload,refund,entity,notes,userId}',webhook_payload#>>'{payload,refund,entity,notes,user_id}',
  webhook_payload#>>'{payload,invoice,entity,notes,userId}',webhook_payload#>>'{payload,invoice,entity,notes,user_id}',
  webhook_payload#>>'{payload,token,entity,notes,userId}',webhook_payload#>>'{payload,token,entity,notes,user_id}']) AS identifiers(value))
 OR EXISTS(SELECT 1 FROM public.billing_events e
  WHERE (e.metadata_json->>'erasureRequestId'=r.id::text OR e.user_id=r.target_user_id)
  AND (e.provider_order_id IN (webhook_payload#>>'{payload,payment,entity,order_id}',webhook_payload#>>'{payload,order,entity,id}')
   OR e.provider_payment_id IN (webhook_payload#>>'{payload,payment,entity,id}',webhook_payload#>>'{payload,refund,entity,payment_id}')
   OR (e.event_type='refund.intent' AND e.id::text=webhook_payload#>>'{payload,refund,entity,notes,refundIntentId}')))
 ORDER BY r.created_at LIMIT 1;
$$;
REVOKE ALL ON FUNCTION private.erasure_request_for_webhook(text,jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION private.erasure_request_for_webhook(text,jsonb) TO service_role;

CREATE FUNCTION private.guard_erased_webhook_payload() RETURNS trigger
 LANGUAGE plpgsql SET search_path='' AS $$
DECLARE erased_request uuid;
BEGIN
 erased_request:=private.erasure_request_for_webhook(NEW.user_id,NEW.payload);
 IF NEW.erasure_request_id IS NOT NULL OR erased_request IS NOT NULL THEN
  NEW:=jsonb_populate_record(NEW,jsonb_build_object('user_id',NULL,'payload','{}'::jsonb,'errorMessage',NULL));
  NEW.erasure_request_id:=coalesce(NEW.erasure_request_id,erased_request);
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION private.guard_erased_webhook_payload() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION private.guard_erased_webhook_payload() TO service_role;
CREATE TRIGGER guard_erased_webhook_payload BEFORE INSERT OR UPDATE ON public."WebhookEvents"
 FOR EACH ROW EXECUTE FUNCTION private.guard_erased_webhook_payload();

COMMENT ON TABLE private.retained_user_subjects IS
 'Explicit original-audit retention: identifying account linkage remains after product-account deletion; no automatic purge deadline is configured.';
COMMENT ON TABLE private.retained_user_records IS
 'Exact original financial/audit rows, including original identifiers and historical metadata, retained under ORIGINAL_AUDITS_RETAINED. Append-only, inaccessible to client roles.';

-- Deleted recipients reconcile by provider identity only; they cannot be claimed to send.
create or replace function public.claim_notification_deliveries(
    p_worker_id text,
    p_limit integer default 50,
    p_lease_seconds integer default 300
)
returns setof public.notification_deliveries
language plpgsql
security invoker
set search_path = ''
as $$
begin
    if p_worker_id is null or btrim(p_worker_id) = '' then
        raise exception 'p_worker_id must be non-empty';
    end if;

    if p_limit not between 1 and 500 then
        raise exception 'p_limit must be between 1 and 500';
    end if;

    if p_lease_seconds not between 30 and 3600 then
        raise exception 'p_lease_seconds must be between 30 and 3600';
    end if;

    return query
    with due as (
        select candidate.id
        from public.notification_deliveries as candidate
        where candidate.user_id IS NOT NULL AND ((
            candidate.status in ('PENDING', 'RETRY_PENDING')
            and candidate.next_attempt_at <= now()
        ) or (
            candidate.status = 'SENDING'
            and candidate.lease_expires_at <= now()
        )
        )
        order by candidate.next_attempt_at, candidate.created_at
        for update skip locked
        limit p_limit
    )
    update public.notification_deliveries as delivery
    set status = 'SENDING',
        attempt_count = delivery.attempt_count + 1,
        claimed_payload_version = delivery.payload_version,
        lease_owner = p_worker_id,
        lease_expires_at = now() + make_interval(secs => p_lease_seconds),
        last_error_code = case
            when delivery.notification_type <> 'trial_expiry_warning'
                and delivery.submission_started_at is not null then 'AMBIGUOUS_SEND'
            when delivery.last_error_code = 'AMBIGUOUS_SEND'
                then delivery.last_error_code
            else null
        end,
        updated_at = now()
    from due
    where delivery.id = due.id
    returning delivery.*;
end;
$$;

revoke all on function public.claim_notification_deliveries(text, integer, integer)
    from public, anon, authenticated;
grant execute on function public.claim_notification_deliveries(text, integer, integer)
    to service_role;
