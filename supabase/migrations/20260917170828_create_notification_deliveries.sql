create table public.notification_deliveries (
    id uuid primary key default gen_random_uuid(),
    notification_type text not null,
    template_version text not null,
    dedupe_key text not null unique,
    user_id text,
    subscription_id uuid,
    period_end timestamptz not null,
    status text not null default 'PENDING',
    attempt_count integer not null default 0,
    next_attempt_at timestamptz not null default now(),
    next_reconcile_at timestamptz,
    lease_owner text,
    lease_expires_at timestamptz,
    provider text,
    provider_message_id text,
    provider_status text,
    last_error_code text,
    accepted_at timestamptz,
    delivered_at timestamptz,
    terminal_at timestamptz,
    metadata_json jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint notification_deliveries_type_chk
        check (notification_type in ('trial_expiry_warning')),
    constraint notification_deliveries_status_chk
        check (status in (
            'PENDING', 'SENDING', 'RETRY_PENDING', 'ACCEPTED',
            'DELIVERED', 'BOUNCED', 'BLOCKED', 'FAILED', 'CANCELLED'
        )),
    constraint notification_deliveries_attempt_chk
        check (attempt_count >= 0),
    constraint notification_deliveries_metadata_chk
        check (jsonb_typeof(metadata_json) = 'object')
);

create index notification_deliveries_due_idx
    on public.notification_deliveries (status, next_attempt_at);

create index notification_deliveries_reconcile_idx
    on public.notification_deliveries (status, next_reconcile_at)
    where next_reconcile_at is not null;

create index notification_deliveries_health_idx
    on public.notification_deliveries (status, updated_at);

create index notification_deliveries_lease_idx
    on public.notification_deliveries (lease_expires_at)
    where status = 'SENDING';

create index notification_deliveries_user_idx
    on public.notification_deliveries (user_id)
    where user_id is not null;

create index notification_deliveries_subscription_idx
    on public.notification_deliveries (subscription_id)
    where subscription_id is not null;

alter table public.notification_deliveries enable row level security;

revoke all on table public.notification_deliveries from anon, authenticated;
grant select, insert, update, delete
    on table public.notification_deliveries
    to service_role;

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
        last_error_code = null,
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
