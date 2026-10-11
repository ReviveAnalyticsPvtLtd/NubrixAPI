-- Non-personal schema fixture derived from the confirmed PG17.6 catalog on 2026-10-08.
-- Exact relevant pre-expansion CHECKs, RLS, triggers, RPC bodies and grants; no data.
-- public schema and Supabase roles are created only by the guarded disposable test fixture.
SET check_function_bodies = false;
--
-- Name: notification_deliveries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_deliveries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    notification_type text NOT NULL,
    template_version text NOT NULL,
    dedupe_key text NOT NULL,
    user_id text,
    subscription_id uuid,
    period_end timestamp with time zone NOT NULL,
    status text DEFAULT 'PENDING'::text NOT NULL,
    attempt_count integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamp with time zone DEFAULT now() NOT NULL,
    next_reconcile_at timestamp with time zone,
    lease_owner text,
    lease_expires_at timestamp with time zone,
    provider text,
    provider_message_id text,
    provider_status text,
    last_error_code text,
    accepted_at timestamp with time zone,
    delivered_at timestamp with time zone,
    terminal_at timestamp with time zone,
    metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT notification_deliveries_attempt_chk CHECK ((attempt_count >= 0)),
    CONSTRAINT notification_deliveries_metadata_chk CHECK ((jsonb_typeof(metadata_json) = 'object'::text)),
    CONSTRAINT notification_deliveries_status_chk CHECK ((status = ANY (ARRAY['PENDING'::text, 'SENDING'::text, 'RETRY_PENDING'::text, 'ACCEPTED'::text, 'DELIVERED'::text, 'BOUNCED'::text, 'BLOCKED'::text, 'FAILED'::text, 'CANCELLED'::text]))),
    CONSTRAINT notification_deliveries_type_chk CHECK ((notification_type = 'trial_expiry_warning'::text))
);


--
-- Name: claim_notification_deliveries(text, integer, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.claim_notification_deliveries(p_worker_id text, p_limit integer DEFAULT 50, p_lease_seconds integer DEFAULT 300) RETURNS SETOF public.notification_deliveries
    LANGUAGE plpgsql
    SET search_path TO ''
    AS $$
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
        where (
            candidate.status in ('PENDING', 'RETRY_PENDING')
            and candidate.next_attempt_at <= now()
        ) or (
            candidate.status = 'SENDING'
            and candidate.lease_expires_at <= now()
        )
        order by candidate.next_attempt_at, candidate.created_at
        for update skip locked
        limit p_limit
    )
    update public.notification_deliveries as delivery
    set status = 'SENDING',
        attempt_count = delivery.attempt_count + 1,
        lease_owner = p_worker_id,
        lease_expires_at = now() + make_interval(secs => p_lease_seconds),
        last_error_code = case
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


--
-- Name: clawback_topup_tokens(text, text, bigint); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.clawback_topup_tokens(p_refund_id text, p_payment_id text, p_refund_amount bigint) RETURNS TABLE(clawed boolean, tokens bigint)
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
    v_invoice_id UUID;
    v_user_id TEXT;
    v_total BIGINT;
    v_granted BIGINT;
    v_event_id UUID;
    v_claw BIGINT;
BEGIN
    SELECT i.id,
           i."userId",
           i.total_amount,
           COALESCE((i.pricing_reference_snapshot_json->>'tokens')::BIGINT, 0)
      INTO v_invoice_id, v_user_id, v_total, v_granted
      FROM public."Invoices" i
     WHERE i."razorpayPaymentId" = p_payment_id
       AND i.billing_reason = 'add_on'
     LIMIT 1;

    IF v_invoice_id IS NULL THEN
        RETURN QUERY SELECT FALSE, 0::BIGINT;
        RETURN;
    END IF;

    INSERT INTO public.billing_events (
        user_id,
        invoice_id,
        event_category,
        event_type,
        event_status,
        amount,
        idempotency_key,
        metadata_json
    ) VALUES (
        v_user_id,
        v_invoice_id,
        'reconciliation',
        'credit.topup_clawback',
        'CLAWED',
        p_refund_amount,
        'topup_clawback:' || p_refund_id,
        jsonb_build_object(
            'refundId', p_refund_id,
            'paymentId', p_payment_id
        )
    )
    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL
    DO NOTHING
    RETURNING id INTO v_event_id;

    IF v_event_id IS NULL THEN
        RETURN QUERY SELECT FALSE, 0::BIGINT;
        RETURN;
    END IF;

    IF v_total IS NULL OR v_total <= 0 THEN
        v_claw := 0;
    ELSE
        v_claw := FLOOR(
            v_granted::NUMERIC * GREATEST(0, p_refund_amount) / v_total
        )::BIGINT;
    END IF;

    UPDATE public.credit_balances cb
       SET topup_tokens = GREATEST(0, cb.topup_tokens - v_claw),
           updated_at = NOW()
     WHERE cb.user_id = v_user_id;

    UPDATE public.billing_events
       SET metadata_json = metadata_json ||
           jsonb_build_object('tokensClawedBack', v_claw)
     WHERE id = v_event_id;

    RETURN QUERY SELECT TRUE, v_claw;
END;
$$;


--
-- Name: decrement_topup_tokens(text, bigint); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.decrement_topup_tokens(p_user_id text, p_tokens bigint) RETURNS bigint
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
    v_remaining BIGINT;
BEGIN
    UPDATE public.credit_balances cb
       SET topup_tokens = GREATEST(0, cb.topup_tokens - GREATEST(0, p_tokens)),
           updated_at = NOW()
     WHERE cb.user_id = p_user_id
    RETURNING cb.topup_tokens INTO v_remaining;

    RETURN COALESCE(v_remaining, 0);
END;
$$;


--
-- Name: grant_topup_tokens(text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.grant_topup_tokens(p_order_id text, p_payment_id text) RETURNS TABLE(granted boolean, tokens bigint, uid text)
    LANGUAGE plpgsql
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
    v_user_id TEXT;
    v_snapshot JSONB;
    v_tokens BIGINT;
    v_new_topup BIGINT;
BEGIN
    UPDATE public."Invoices"
       SET status = 'PAID',
           "razorpayPaymentId" = p_payment_id,
           "paidAt" = NOW()
     WHERE razorpay_order_id = p_order_id
       AND billing_reason = 'add_on'
       AND LOWER(status) IN ('upcoming', 'payment_pending')
    RETURNING "userId", pricing_reference_snapshot_json
         INTO v_user_id, v_snapshot;

    IF NOT FOUND THEN
        RETURN QUERY SELECT FALSE, 0::BIGINT, NULL::TEXT;
        RETURN;
    END IF;

    v_tokens := COALESCE((v_snapshot->>'tokens')::BIGINT, 0);
    IF v_tokens <= 0 THEN RAISE EXCEPTION
        'Top-up grant aborted. Invoice % has an invalid token quantity.', p_order_id;
    END IF;

    UPDATE public.credit_balances cb
       SET topup_tokens = cb.topup_tokens + v_tokens,
           updated_at = NOW()
     WHERE cb.user_id = v_user_id
    RETURNING cb.topup_tokens INTO v_new_topup;

    IF NOT FOUND THEN RAISE EXCEPTION
        'Top-up grant aborted. No credit balance exists for user %.', v_user_id;
    END IF;

    RETURN QUERY SELECT TRUE, v_tokens, v_user_id;
END;
$$;


--
-- Name: reconcile_credit_balance_if_no_admin_refresh(text, bigint, bigint, timestamp with time zone, timestamp with time zone, timestamp with time zone); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.reconcile_credit_balance_if_no_admin_refresh(p_user_id text, p_remaining_tokens bigint, p_used_tokens bigint, p_last_reconciled_at timestamp with time zone, p_period_start timestamp with time zone, p_period_end timestamp with time zone) RETURNS boolean
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO ''
    AS $$
begin
    perform pg_advisory_xact_lock(hashtextextended(p_user_id, 0));

    update public.credit_balances as balance
    set remaining_tokens = greatest(0, p_remaining_tokens),
        used_tokens = greatest(0, p_used_tokens),
        period_start = coalesce(p_period_start, balance.period_start),
        period_end = coalesce(p_period_end, balance.period_end),
        last_reset_at = case
            when p_period_end is null then balance.last_reset_at
            else p_last_reconciled_at
        end,
        last_reconciled_at = p_last_reconciled_at,
        updated_at = p_last_reconciled_at
    where balance.user_id = p_user_id
      and not exists (
          select 1
          from public.admin_free_trial_extensions as extension
          where extension.user_id = p_user_id
            and extension.credit_sync_status = 'PENDING'
      );

    return found;
end;
$$;


--
-- Name: update_billing_events_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_billing_events_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


--
-- Name: update_subscriptions_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.update_subscriptions_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


--
-- Name: Invoices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."Invoices" (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    "userId" text,
    "razorpayPaymentId" text,
    amount bigint,
    currency text DEFAULT 'INR'::text NOT NULL,
    status text,
    "billingStart" timestamp with time zone,
    "billingEnd" timestamp with time zone,
    "paidAt" timestamp with time zone,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    subscription_id uuid,
    billing_reason text,
    payment_flow text,
    requires_customer_auth boolean DEFAULT false,
    razorpay_order_id text,
    provider_receipt text,
    due_date timestamp with time zone,
    expires_at timestamp with time zone,
    period_start timestamp with time zone,
    period_end timestamp with time zone,
    amount_before_tax bigint,
    tax_amount bigint,
    total_amount bigint,
    tax_breakdown_json jsonb,
    tax_rule_version text,
    place_of_supply_snapshot text,
    pricing_version text,
    pricing_reference_snapshot_json jsonb,
    metadata_json jsonb,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT "Invoices_billing_reason_check" CHECK (((billing_reason IS NULL) OR (billing_reason = ANY (ARRAY['initial_purchase'::text, 'renewal'::text, 'proration'::text, 'add_on'::text, 'manual_adjustment'::text])))),
    CONSTRAINT "Invoices_payment_flow_check" CHECK (((payment_flow IS NULL) OR (payment_flow = ANY (ARRAY['token_charge'::text, 'razorpay_order_checkout'::text]))))
);


--
-- Name: Users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."Users" (
    "userId" text NOT NULL,
    email text NOT NULL,
    password text,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    onboarded boolean DEFAULT false NOT NULL,
    "currentWorkspaceId" text,
    "fullName" text,
    "phoneNumber" text,
    "profileImage" text,
    "companyName" text,
    role text,
    "profileBio" text,
    usage text,
    "industryType" text,
    "companySize" text,
    country text,
    goals text,
    source text,
    "isBanned" boolean DEFAULT false NOT NULL,
    "bannedAt" timestamp with time zone,
    "bannedBy" uuid,
    "banReason" text,
    CONSTRAINT users_ban_reason_length_chk CHECK ((("banReason" IS NULL) OR (char_length("banReason") <= 1000))),
    CONSTRAINT users_ban_state_chk CHECK ((("isBanned" AND ("bannedAt" IS NOT NULL) AND ("bannedBy" IS NOT NULL)) OR ((NOT "isBanned") AND ("bannedAt" IS NULL) AND ("bannedBy" IS NULL) AND ("banReason" IS NULL))))
);


--
-- Name: WebhookEvents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."WebhookEvents" (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    "razorpayEventId" text NOT NULL,
    "eventType" text NOT NULL,
    payload jsonb,
    status text DEFAULT 'processing'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    "lastAttemptAt" timestamp with time zone,
    "errorMessage" text,
    "createdAt" timestamp with time zone DEFAULT now() NOT NULL,
    provider text DEFAULT 'razorpay'::text,
    payload_hash text,
    "completedAt" timestamp with time zone,
    user_id text
);


--
-- Name: admin_audit_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_audit_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    admin_id uuid,
    admin_email text NOT NULL,
    session_id uuid,
    actor_type text NOT NULL,
    action text NOT NULL,
    target_type text NOT NULL,
    target_id text,
    changed_fields jsonb DEFAULT '[]'::jsonb NOT NULL,
    outcome text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    details jsonb DEFAULT '{}'::jsonb NOT NULL,
    CONSTRAINT admin_audit_log_actor_type_chk CHECK ((actor_type = ANY (ARRAY['admin'::text, 'cli'::text]))),
    CONSTRAINT admin_audit_log_admin_actor_chk CHECK (((actor_type <> 'admin'::text) OR ((admin_id IS NOT NULL) AND (session_id IS NOT NULL)))),
    CONSTRAINT admin_audit_log_admin_email_chk CHECK ((btrim(admin_email) <> ''::text)),
    CONSTRAINT admin_audit_log_changed_fields_chk CHECK ((jsonb_typeof(changed_fields) = 'array'::text)),
    CONSTRAINT admin_audit_log_details_chk CHECK ((jsonb_typeof(details) = 'object'::text))
);


--
-- Name: admin_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_sessions (
    id uuid NOT NULL,
    admin_id uuid NOT NULL,
    token_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_at timestamp with time zone,
    last_used_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT admin_sessions_expiry_chk CHECK ((expires_at > created_at)),
    CONSTRAINT admin_sessions_token_hash_chk CHECK ((token_hash ~ '^[0-9a-f]{64}$'::text))
);


--
-- Name: admin_users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.admin_users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email text NOT NULL,
    name text NOT NULL,
    password_hash text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    last_login_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT admin_users_email_normalized_chk CHECK (((email = lower(btrim(email))) AND (email <> ''::text))),
    CONSTRAINT admin_users_name_nonempty_chk CHECK ((btrim(name) <> ''::text)),
    CONSTRAINT admin_users_password_hash_nonempty_chk CHECK ((password_hash <> ''::text))
);


--
-- Name: billing_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.billing_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id text,
    subscription_id uuid,
    invoice_id uuid,
    webhook_event_id uuid,
    event_category text NOT NULL,
    event_type text NOT NULL,
    event_status text,
    payment_attempt_type text,
    payment_status text,
    provider text DEFAULT 'razorpay'::text NOT NULL,
    provider_payment_id text,
    provider_order_id text,
    period_start timestamp with time zone,
    period_end timestamp with time zone,
    cycle_key text,
    amount bigint,
    currency text DEFAULT 'INR'::text,
    failure_reason text,
    idempotency_key text,
    metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    attempted_at timestamp with time zone,
    completed_at timestamp with time zone,
    occurred_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT billing_events_event_category_check CHECK ((event_category = ANY (ARRAY['audit'::text, 'payment_attempt'::text, 'notification'::text, 'reconciliation'::text, 'system'::text]))),
    CONSTRAINT billing_events_payment_attempt_required_fields CHECK (((event_category <> 'payment_attempt'::text) OR ((payment_attempt_type IS NOT NULL) AND (payment_status IS NOT NULL) AND (attempted_at IS NOT NULL)))),
    CONSTRAINT billing_events_payment_attempt_type_check CHECK (((payment_attempt_type IS NULL) OR (payment_attempt_type = ANY (ARRAY['token_debit'::text, 'checkout'::text, 'reconciliation_update'::text])))),
    CONSTRAINT billing_events_payment_status_check CHECK (((payment_status IS NULL) OR (payment_status = ANY (ARRAY['created'::text, 'precheck_failed'::text, 'pending_provider_ack'::text, 'authorized'::text, 'captured'::text, 'failed'::text, 'cancelled'::text, 'expired'::text, 'investigated'::text]))))
);


--
-- Name: credit_balances; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.credit_balances (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id text NOT NULL,
    subscription_id uuid,
    plan_tier text DEFAULT 'none'::text NOT NULL,
    period_start timestamp with time zone,
    period_end timestamp with time zone,
    last_reset_at timestamp with time zone,
    last_reconciled_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    monthly_token_quota bigint DEFAULT 0 NOT NULL,
    used_tokens bigint DEFAULT 0 NOT NULL,
    remaining_tokens bigint DEFAULT 0 NOT NULL,
    topup_tokens bigint DEFAULT 0 NOT NULL,
    domain_count integer DEFAULT 1 NOT NULL,
    CONSTRAINT credit_balances_domain_count_check CHECK (((domain_count >= 1) AND (domain_count <= 4))),
    CONSTRAINT credit_balances_plan_tier_check CHECK ((plan_tier = ANY (ARRAY['none'::text, 'free'::text, 'pro'::text, 'annual'::text])))
);


--
-- Name: subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.subscriptions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id text NOT NULL,
    billing_mode text DEFAULT 'none'::text NOT NULL,
    current_period_start timestamp with time zone,
    current_period_end timestamp with time zone,
    renewal_due_at timestamp with time zone,
    auto_renew_enabled boolean DEFAULT false NOT NULL,
    payment_collection_mode text DEFAULT 'authenticated_checkout'::text NOT NULL,
    status text DEFAULT 'none'::text NOT NULL,
    default_currency text DEFAULT 'INR'::text NOT NULL,
    subscribed_experts jsonb DEFAULT '[]'::jsonb NOT NULL,
    domain_count integer DEFAULT 0 NOT NULL,
    pending_removals jsonb DEFAULT '[]'::jsonb NOT NULL,
    pending_additions jsonb DEFAULT '[]'::jsonb NOT NULL,
    billing_state jsonb DEFAULT '{}'::jsonb NOT NULL,
    razorpay_customer_id text,
    razorpay_token_id text,
    subscription_anchor_day integer,
    recurring_failures integer DEFAULT 0 NOT NULL,
    cancellation_reason text,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    plan_type text DEFAULT 'none'::text NOT NULL,
    erasure_pending boolean DEFAULT false NOT NULL,
    admin_credit_generation bigint DEFAULT 0 NOT NULL,
    CONSTRAINT subscriptions_billing_mode_check CHECK ((billing_mode = ANY (ARRAY['none'::text, 'monthly_recurring'::text, 'annual_prepaid'::text]))),
    CONSTRAINT subscriptions_domain_count_check CHECK (((domain_count >= 0) AND (domain_count <= 4))),
    CONSTRAINT subscriptions_payment_collection_mode_check CHECK ((payment_collection_mode = ANY (ARRAY['silent_token'::text, 'authenticated_checkout'::text]))),
    CONSTRAINT subscriptions_plan_type_check CHECK ((plan_type = ANY (ARRAY['none'::text, 'free'::text, 'pro'::text, 'annual'::text]))),
    CONSTRAINT subscriptions_status_check CHECK ((status = ANY (ARRAY['none'::text, 'trial'::text, 'active'::text, 'renewal_upcoming'::text, 'payment_pending'::text, 'past_due'::text, 'suspended'::text, 'cancelled'::text, 'expired'::text])))
);


--
-- Name: Invoices Invoices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Invoices"
    ADD CONSTRAINT "Invoices_pkey" PRIMARY KEY (id);


--
-- Name: Users Users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Users"
    ADD CONSTRAINT "Users_email_key" UNIQUE (email);


--
-- Name: Users Users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Users"
    ADD CONSTRAINT "Users_pkey" PRIMARY KEY ("userId");


--
-- Name: WebhookEvents WebhookEvents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."WebhookEvents"
    ADD CONSTRAINT "WebhookEvents_pkey" PRIMARY KEY (id);


--
-- Name: admin_audit_log admin_audit_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_audit_log
    ADD CONSTRAINT admin_audit_log_pkey PRIMARY KEY (id);


--
-- Name: admin_sessions admin_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_sessions
    ADD CONSTRAINT admin_sessions_pkey PRIMARY KEY (id);


--
-- Name: admin_sessions admin_sessions_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_sessions
    ADD CONSTRAINT admin_sessions_token_hash_key UNIQUE (token_hash);


--
-- Name: admin_users admin_users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_users
    ADD CONSTRAINT admin_users_pkey PRIMARY KEY (id);


--
-- Name: billing_events billing_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_events
    ADD CONSTRAINT billing_events_pkey PRIMARY KEY (id);


--
-- Name: credit_balances credit_balances_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_balances
    ADD CONSTRAINT credit_balances_pkey PRIMARY KEY (id);


--
-- Name: notification_deliveries notification_deliveries_dedupe_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_deliveries
    ADD CONSTRAINT notification_deliveries_dedupe_key_key UNIQUE (dedupe_key);


--
-- Name: notification_deliveries notification_deliveries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_deliveries
    ADD CONSTRAINT notification_deliveries_pkey PRIMARY KEY (id);


--
-- Name: subscriptions subscriptions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_pkey PRIMARY KEY (id);


--
-- Name: credit_balances uq_credit_balances_user; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_balances
    ADD CONSTRAINT uq_credit_balances_user UNIQUE (user_id);


--
-- Name: admin_audit_log_created_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX admin_audit_log_created_at_idx ON public.admin_audit_log USING btree (created_at DESC);


--
-- Name: admin_audit_log_target_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX admin_audit_log_target_idx ON public.admin_audit_log USING btree (target_type, target_id);


--
-- Name: admin_sessions_active_expiry_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX admin_sessions_active_expiry_idx ON public.admin_sessions USING btree (expires_at) WHERE (revoked_at IS NULL);


--
-- Name: admin_sessions_admin_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX admin_sessions_admin_id_idx ON public.admin_sessions USING btree (admin_id);


--
-- Name: admin_users_email_lower_uidx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX admin_users_email_lower_uidx ON public.admin_users USING btree (lower(email));


--
-- Name: idx_billing_events_idempotency; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_billing_events_idempotency ON public.billing_events USING btree (idempotency_key) WHERE (idempotency_key IS NOT NULL);


--
-- Name: idx_billing_events_invoice; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_billing_events_invoice ON public.billing_events USING btree (invoice_id) WHERE (invoice_id IS NOT NULL);


--
-- Name: idx_billing_events_occurred; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_billing_events_occurred ON public.billing_events USING btree (occurred_at);


--
-- Name: idx_billing_events_payment_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_billing_events_payment_status ON public.billing_events USING btree (event_category, payment_status, attempted_at) WHERE ((event_category = 'payment_attempt'::text) AND (payment_status = ANY (ARRAY['created'::text, 'pending_provider_ack'::text, 'authorized'::text])));


--
-- Name: idx_billing_events_provider_order; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_billing_events_provider_order ON public.billing_events USING btree (provider_order_id) WHERE (provider_order_id IS NOT NULL);


--
-- Name: idx_billing_events_provider_payment; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_billing_events_provider_payment ON public.billing_events USING btree (provider_payment_id) WHERE (provider_payment_id IS NOT NULL);


--
-- Name: idx_billing_events_subscription; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_billing_events_subscription ON public.billing_events USING btree (subscription_id) WHERE (subscription_id IS NOT NULL);


--
-- Name: idx_billing_events_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_billing_events_type ON public.billing_events USING btree (event_type);


--
-- Name: idx_billing_events_unresolved_monthly; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_billing_events_unresolved_monthly ON public.billing_events USING btree (user_id, period_start, period_end, payment_attempt_type) WHERE ((event_category = 'payment_attempt'::text) AND (payment_attempt_type = 'token_debit'::text) AND (payment_status = ANY (ARRAY['created'::text, 'pending_provider_ack'::text, 'authorized'::text])));


--
-- Name: idx_billing_events_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_billing_events_user ON public.billing_events USING btree (user_id) WHERE (user_id IS NOT NULL);


--
-- Name: idx_credit_balances_period; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_credit_balances_period ON public.credit_balances USING btree (period_end) WHERE (remaining_tokens > 0);


--
-- Name: idx_credit_balances_user_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_credit_balances_user_unique ON public.credit_balances USING btree (user_id);


--
-- Name: idx_invoices_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_invoices_created_at ON public."Invoices" USING btree ("createdAt");


--
-- Name: idx_invoices_razorpay_order_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_invoices_razorpay_order_id ON public."Invoices" USING btree (razorpay_order_id) WHERE (razorpay_order_id IS NOT NULL);


--
-- Name: idx_invoices_razorpay_payment; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_invoices_razorpay_payment ON public."Invoices" USING btree ("razorpayPaymentId") WHERE ("razorpayPaymentId" IS NOT NULL);


--
-- Name: idx_invoices_renewal_period_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_invoices_renewal_period_unique ON public."Invoices" USING btree (subscription_id, period_start, period_end, billing_reason) WHERE ((billing_reason = 'renewal'::text) AND (status <> ALL (ARRAY['VOID'::text, 'void'::text])));


--
-- Name: idx_invoices_status_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_invoices_status_due ON public."Invoices" USING btree (status, due_date) WHERE (status = ANY (ARRAY['upcoming'::text, 'payment_pending'::text]));


--
-- Name: idx_invoices_subscription_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_invoices_subscription_id ON public."Invoices" USING btree (subscription_id) WHERE (subscription_id IS NOT NULL);


--
-- Name: idx_invoices_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_invoices_user ON public."Invoices" USING btree ("userId");


--
-- Name: idx_subscriptions_domain_count; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_domain_count ON public.subscriptions USING btree (domain_count);


--
-- Name: idx_subscriptions_plan_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_plan_type ON public.subscriptions USING btree (plan_type);


--
-- Name: idx_subscriptions_razorpay_customer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_razorpay_customer ON public.subscriptions USING btree (razorpay_customer_id) WHERE (razorpay_customer_id IS NOT NULL);


--
-- Name: idx_subscriptions_razorpay_token; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_razorpay_token ON public.subscriptions USING btree (razorpay_token_id) WHERE (razorpay_token_id IS NOT NULL);


--
-- Name: idx_subscriptions_renewal_due; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_renewal_due ON public.subscriptions USING btree (renewal_due_at) WHERE (status = ANY (ARRAY['active'::text, 'renewal_upcoming'::text]));


--
-- Name: idx_subscriptions_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_status ON public.subscriptions USING btree (status);


--
-- Name: idx_subscriptions_user_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_subscriptions_user_active ON public.subscriptions USING btree (user_id) WHERE (status <> ALL (ARRAY['cancelled'::text, 'expired'::text]));


--
-- Name: idx_subscriptions_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_subscriptions_user_id ON public.subscriptions USING btree (user_id);


--
-- Name: idx_users_email; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_users_email ON public."Users" USING btree (email);


--
-- Name: idx_webhook_events_event_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_webhook_events_event_id ON public."WebhookEvents" USING btree ("razorpayEventId");


--
-- Name: idx_webhook_events_provider_event_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX idx_webhook_events_provider_event_unique ON public."WebhookEvents" USING btree (provider, "razorpayEventId");


--
-- Name: idx_webhook_events_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_webhook_events_status ON public."WebhookEvents" USING btree (status) WHERE (status = ANY (ARRAY['processing'::text, 'failed'::text]));


--
-- Name: idx_webhook_events_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_webhook_events_type ON public."WebhookEvents" USING btree ("eventType");


--
-- Name: notification_deliveries_due_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX notification_deliveries_due_idx ON public.notification_deliveries USING btree (status, next_attempt_at);


--
-- Name: notification_deliveries_health_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX notification_deliveries_health_idx ON public.notification_deliveries USING btree (status, updated_at);


--
-- Name: notification_deliveries_lease_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX notification_deliveries_lease_idx ON public.notification_deliveries USING btree (lease_expires_at) WHERE (status = 'SENDING'::text);


--
-- Name: notification_deliveries_reconcile_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX notification_deliveries_reconcile_idx ON public.notification_deliveries USING btree (status, next_reconcile_at) WHERE (next_reconcile_at IS NOT NULL);


--
-- Name: notification_deliveries_subscription_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX notification_deliveries_subscription_idx ON public.notification_deliveries USING btree (subscription_id) WHERE (subscription_id IS NOT NULL);


--
-- Name: notification_deliveries_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX notification_deliveries_user_idx ON public.notification_deliveries USING btree (user_id) WHERE (user_id IS NOT NULL);


--
-- Name: subscriptions_erasure_pending_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX subscriptions_erasure_pending_idx ON public.subscriptions USING btree (user_id) WHERE erasure_pending;


--
-- Name: webhook_events_user_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX webhook_events_user_id_idx ON public."WebhookEvents" USING btree (user_id) WHERE (user_id IS NOT NULL);


--
-- Name: billing_events trg_billing_events_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_billing_events_updated_at BEFORE UPDATE ON public.billing_events FOR EACH ROW EXECUTE FUNCTION public.update_billing_events_updated_at();


--
-- Name: subscriptions trg_subscriptions_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_subscriptions_updated_at BEFORE UPDATE ON public.subscriptions FOR EACH ROW EXECUTE FUNCTION public.update_subscriptions_updated_at();


--
-- Name: Invoices Invoices_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Invoices"
    ADD CONSTRAINT "Invoices_subscription_id_fkey" FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: Invoices Invoices_userId_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Invoices"
    ADD CONSTRAINT "Invoices_userId_fkey" FOREIGN KEY ("userId") REFERENCES public."Users"("userId") ON DELETE SET NULL;


--
-- Name: admin_sessions admin_sessions_admin_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.admin_sessions
    ADD CONSTRAINT admin_sessions_admin_id_fkey FOREIGN KEY (admin_id) REFERENCES public.admin_users(id) ON DELETE CASCADE;


--
-- Name: billing_events billing_events_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_events
    ADD CONSTRAINT billing_events_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public."Invoices"(id);


--
-- Name: billing_events billing_events_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_events
    ADD CONSTRAINT billing_events_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: billing_events billing_events_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_events
    ADD CONSTRAINT billing_events_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."Users"("userId") ON DELETE SET NULL;


--
-- Name: billing_events billing_events_webhook_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_events
    ADD CONSTRAINT billing_events_webhook_event_id_fkey FOREIGN KEY (webhook_event_id) REFERENCES public."WebhookEvents"(id);


--
-- Name: credit_balances credit_balances_subscription_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_balances
    ADD CONSTRAINT credit_balances_subscription_id_fkey FOREIGN KEY (subscription_id) REFERENCES public.subscriptions(id);


--
-- Name: credit_balances credit_balances_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_balances
    ADD CONSTRAINT credit_balances_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."Users"("userId") ON DELETE CASCADE;


--
-- Name: subscriptions subscriptions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.subscriptions
    ADD CONSTRAINT subscriptions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."Users"("userId") ON DELETE CASCADE;


--
-- Name: Users users_banned_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."Users"
    ADD CONSTRAINT users_banned_by_fkey FOREIGN KEY ("bannedBy") REFERENCES public.admin_users(id);


--
-- Name: WebhookEvents webhook_events_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."WebhookEvents"
    ADD CONSTRAINT webhook_events_user_id_fkey FOREIGN KEY (user_id) REFERENCES public."Users"("userId") ON DELETE SET NULL;


--
-- Name: Invoices; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public."Invoices" ENABLE ROW LEVEL SECURITY;

--
-- Name: Users; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public."Users" ENABLE ROW LEVEL SECURITY;

--
-- Name: WebhookEvents; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public."WebhookEvents" ENABLE ROW LEVEL SECURITY;

--
-- Name: admin_audit_log; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.admin_audit_log ENABLE ROW LEVEL SECURITY;

--
-- Name: admin_sessions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.admin_sessions ENABLE ROW LEVEL SECURITY;

--
-- Name: admin_users; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.admin_users ENABLE ROW LEVEL SECURITY;

--
-- Name: billing_events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.billing_events ENABLE ROW LEVEL SECURITY;

--
-- Name: notification_deliveries; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.notification_deliveries ENABLE ROW LEVEL SECURITY;

--
-- Name: subscriptions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.subscriptions ENABLE ROW LEVEL SECURITY;

--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: -
--

REVOKE USAGE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO postgres;
GRANT USAGE ON SCHEMA public TO anon;
GRANT USAGE ON SCHEMA public TO authenticated;
GRANT ALL ON SCHEMA public TO service_role;


--
-- Name: TABLE notification_deliveries; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.notification_deliveries TO postgres;
GRANT ALL ON TABLE public.notification_deliveries TO service_role;


--
-- Name: FUNCTION claim_notification_deliveries(p_worker_id text, p_limit integer, p_lease_seconds integer); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.claim_notification_deliveries(p_worker_id text, p_limit integer, p_lease_seconds integer) FROM PUBLIC;
GRANT ALL ON FUNCTION public.claim_notification_deliveries(p_worker_id text, p_limit integer, p_lease_seconds integer) TO postgres;
GRANT ALL ON FUNCTION public.claim_notification_deliveries(p_worker_id text, p_limit integer, p_lease_seconds integer) TO service_role;


--
-- Name: FUNCTION clawback_topup_tokens(p_refund_id text, p_payment_id text, p_refund_amount bigint); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.clawback_topup_tokens(p_refund_id text, p_payment_id text, p_refund_amount bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION public.clawback_topup_tokens(p_refund_id text, p_payment_id text, p_refund_amount bigint) TO postgres;
GRANT ALL ON FUNCTION public.clawback_topup_tokens(p_refund_id text, p_payment_id text, p_refund_amount bigint) TO anon;
GRANT ALL ON FUNCTION public.clawback_topup_tokens(p_refund_id text, p_payment_id text, p_refund_amount bigint) TO authenticated;
GRANT ALL ON FUNCTION public.clawback_topup_tokens(p_refund_id text, p_payment_id text, p_refund_amount bigint) TO service_role;


--
-- Name: FUNCTION decrement_topup_tokens(p_user_id text, p_tokens bigint); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.decrement_topup_tokens(p_user_id text, p_tokens bigint) FROM PUBLIC;
GRANT ALL ON FUNCTION public.decrement_topup_tokens(p_user_id text, p_tokens bigint) TO postgres;
GRANT ALL ON FUNCTION public.decrement_topup_tokens(p_user_id text, p_tokens bigint) TO anon;
GRANT ALL ON FUNCTION public.decrement_topup_tokens(p_user_id text, p_tokens bigint) TO authenticated;
GRANT ALL ON FUNCTION public.decrement_topup_tokens(p_user_id text, p_tokens bigint) TO service_role;


--
-- Name: FUNCTION grant_topup_tokens(p_order_id text, p_payment_id text); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.grant_topup_tokens(p_order_id text, p_payment_id text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.grant_topup_tokens(p_order_id text, p_payment_id text) TO postgres;
GRANT ALL ON FUNCTION public.grant_topup_tokens(p_order_id text, p_payment_id text) TO anon;
GRANT ALL ON FUNCTION public.grant_topup_tokens(p_order_id text, p_payment_id text) TO authenticated;
GRANT ALL ON FUNCTION public.grant_topup_tokens(p_order_id text, p_payment_id text) TO service_role;


--
-- Name: FUNCTION reconcile_credit_balance_if_no_admin_refresh(p_user_id text, p_remaining_tokens bigint, p_used_tokens bigint, p_last_reconciled_at timestamp with time zone, p_period_start timestamp with time zone, p_period_end timestamp with time zone); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.reconcile_credit_balance_if_no_admin_refresh(p_user_id text, p_remaining_tokens bigint, p_used_tokens bigint, p_last_reconciled_at timestamp with time zone, p_period_start timestamp with time zone, p_period_end timestamp with time zone) FROM PUBLIC;
GRANT ALL ON FUNCTION public.reconcile_credit_balance_if_no_admin_refresh(p_user_id text, p_remaining_tokens bigint, p_used_tokens bigint, p_last_reconciled_at timestamp with time zone, p_period_start timestamp with time zone, p_period_end timestamp with time zone) TO service_role;


--
-- Name: FUNCTION update_billing_events_updated_at(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.update_billing_events_updated_at() TO postgres;
GRANT ALL ON FUNCTION public.update_billing_events_updated_at() TO anon;
GRANT ALL ON FUNCTION public.update_billing_events_updated_at() TO authenticated;
GRANT ALL ON FUNCTION public.update_billing_events_updated_at() TO service_role;


--
-- Name: FUNCTION update_subscriptions_updated_at(); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.update_subscriptions_updated_at() TO postgres;
GRANT ALL ON FUNCTION public.update_subscriptions_updated_at() TO anon;
GRANT ALL ON FUNCTION public.update_subscriptions_updated_at() TO authenticated;
GRANT ALL ON FUNCTION public.update_subscriptions_updated_at() TO service_role;


--
-- Name: TABLE "Invoices"; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public."Invoices" TO postgres;
GRANT ALL ON TABLE public."Invoices" TO anon;
GRANT ALL ON TABLE public."Invoices" TO authenticated;
GRANT ALL ON TABLE public."Invoices" TO service_role;


--
-- Name: TABLE "Users"; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public."Users" TO postgres;
GRANT ALL ON TABLE public."Users" TO anon;
GRANT ALL ON TABLE public."Users" TO authenticated;
GRANT ALL ON TABLE public."Users" TO service_role;


--
-- Name: TABLE "WebhookEvents"; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public."WebhookEvents" TO postgres;
GRANT ALL ON TABLE public."WebhookEvents" TO anon;
GRANT ALL ON TABLE public."WebhookEvents" TO authenticated;
GRANT ALL ON TABLE public."WebhookEvents" TO service_role;


--
-- Name: TABLE admin_audit_log; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.admin_audit_log TO postgres;
GRANT ALL ON TABLE public.admin_audit_log TO anon;
GRANT ALL ON TABLE public.admin_audit_log TO authenticated;
GRANT ALL ON TABLE public.admin_audit_log TO service_role;

-- Actual column ACLs are separate from the table ACL and must also be reconciled.
GRANT UPDATE(target_id) ON TABLE public.admin_audit_log TO service_role;
GRANT UPDATE(details) ON TABLE public.admin_audit_log TO service_role;


--
-- Name: TABLE admin_sessions; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.admin_sessions TO postgres;
GRANT ALL ON TABLE public.admin_sessions TO anon;
GRANT ALL ON TABLE public.admin_sessions TO authenticated;
GRANT ALL ON TABLE public.admin_sessions TO service_role;


--
-- Name: TABLE admin_users; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.admin_users TO postgres;
GRANT ALL ON TABLE public.admin_users TO anon;
GRANT ALL ON TABLE public.admin_users TO authenticated;
GRANT ALL ON TABLE public.admin_users TO service_role;


--
-- Name: TABLE billing_events; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.billing_events TO postgres;
GRANT ALL ON TABLE public.billing_events TO anon;
GRANT ALL ON TABLE public.billing_events TO authenticated;
GRANT ALL ON TABLE public.billing_events TO service_role;


--
-- Name: TABLE credit_balances; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.credit_balances TO postgres;
GRANT ALL ON TABLE public.credit_balances TO anon;
GRANT ALL ON TABLE public.credit_balances TO authenticated;
GRANT ALL ON TABLE public.credit_balances TO service_role;


--
-- Name: TABLE subscriptions; Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON TABLE public.subscriptions TO postgres;
GRANT ALL ON TABLE public.subscriptions TO anon;
GRANT ALL ON TABLE public.subscriptions TO authenticated;
GRANT ALL ON TABLE public.subscriptions TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON SEQUENCES TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON SEQUENCES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON SEQUENCES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON SEQUENCES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON SEQUENCES TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR FUNCTIONS; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON FUNCTIONS TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR FUNCTIONS; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON FUNCTIONS TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON FUNCTIONS TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON FUNCTIONS TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT ALL ON TABLES TO service_role;


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: public; Owner: -
--

ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON TABLES TO postgres;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON TABLES TO anon;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON TABLES TO authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE supabase_admin IN SCHEMA public GRANT ALL ON TABLES TO service_role;


--
-- PostgreSQL database dump complete
--
