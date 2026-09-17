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

revoke all on function public.claim_notification_deliveries(text, integer, integer)
    from public, anon, authenticated;
grant execute on function public.claim_notification_deliveries(text, integer, integer)
    to service_role;
