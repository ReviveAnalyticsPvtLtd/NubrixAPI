-- Durable administrator credit-reset operations.
--
-- An operation freezes its target membership once; every target reaches a
-- terminal outcome in the same transaction as its balance mutation, credit
-- allocation event and strict admin_audit_log row. Provenance is never
-- cascaded away: there is no foreign key to users or admins, and deletion is
-- not granted to any role.

create table if not exists public.admin_credit_reset_operations (
    id uuid primary key,
    scope text not null,
    target_user_id text,
    admin_id uuid not null,
    admin_email text not null,
    session_id uuid not null,
    reason text not null,
    idempotency_key_hash text not null,
    request_fingerprint text not null,
    created_at timestamptz not null default now(),
    constraint admin_credit_reset_operations_scope_chk
        check (scope in ('individual', 'all')),
    constraint admin_credit_reset_operations_target_chk
        check ((scope = 'individual') = (target_user_id is not null)),
    constraint admin_credit_reset_operations_target_length_chk
        check (target_user_id is null
               or (btrim(target_user_id) <> '' and char_length(target_user_id) <= 128)),
    constraint admin_credit_reset_operations_email_chk
        check (btrim(admin_email) <> ''),
    constraint admin_credit_reset_operations_reason_chk
        check (btrim(reason) <> '' and char_length(reason) <= 2000),
    constraint admin_credit_reset_operations_key_hash_chk
        check (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    constraint admin_credit_reset_operations_fingerprint_chk
        check (request_fingerprint ~ '^[0-9a-f]{64}$'),
    constraint admin_credit_reset_operations_admin_key_uniq
        unique (admin_id, idempotency_key_hash)
);

create table if not exists public.admin_credit_reset_targets (
    operation_id uuid not null
        references public.admin_credit_reset_operations (id) on delete restrict,
    user_id text not null,
    outcome text not null default 'PENDING',
    reason_code text,
    before_snapshot jsonb,
    after_snapshot jsonb,
    audit_id uuid,
    reset_at timestamptz,
    cache_state text not null default 'NOT_APPLICABLE',
    updated_at timestamptz not null default now(),
    constraint admin_credit_reset_targets_pkey primary key (operation_id, user_id),
    constraint admin_credit_reset_targets_outcome_chk
        check (outcome in ('PENDING', 'RESET', 'SKIPPED', 'RETRYABLE_FAILED')),
    constraint admin_credit_reset_targets_cache_chk
        check (cache_state in ('NOT_APPLICABLE', 'PENDING', 'INVALIDATED')),
    constraint admin_credit_reset_targets_reason_code_chk
        check (reason_code is null or reason_code ~ '^[A-Z0-9_]{1,80}$'),
    constraint admin_credit_reset_targets_terminal_audit_chk
        check (outcome not in ('RESET', 'SKIPPED') or audit_id is not null),
    -- before_snapshot is null only when the reset initialized a live trial's missing balance.
    constraint admin_credit_reset_targets_reset_chk
        check (outcome <> 'RESET'
               or (reset_at is not null and after_snapshot is not null)),
    constraint admin_credit_reset_targets_skip_chk
        check (outcome <> 'SKIPPED' or (reason_code is not null and after_snapshot is null)),
    constraint admin_credit_reset_targets_cache_outcome_chk
        check (cache_state = 'NOT_APPLICABLE' or outcome = 'RESET')
);

create index if not exists admin_credit_reset_targets_unfinished_idx
    on public.admin_credit_reset_targets (operation_id, user_id)
    where outcome in ('PENDING', 'RETRYABLE_FAILED');

create index if not exists admin_credit_reset_targets_cache_pending_idx
    on public.admin_credit_reset_targets (operation_id, user_id)
    where cache_state = 'PENDING';

-- Terminal financial results are immutable; only cache progress may advance.
create or replace function public.admin_credit_reset_targets_guard()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if old.outcome in ('RESET', 'SKIPPED') then
        if new.outcome is distinct from old.outcome
            or new.reason_code is distinct from old.reason_code
            or new.before_snapshot is distinct from old.before_snapshot
            or new.after_snapshot is distinct from old.after_snapshot
            or new.audit_id is distinct from old.audit_id
            or new.reset_at is distinct from old.reset_at
            or new.operation_id is distinct from old.operation_id
            or new.user_id is distinct from old.user_id
            or not (new.cache_state = old.cache_state
                    or (old.cache_state = 'PENDING' and new.cache_state = 'INVALIDATED')) then
            raise exception 'ADMIN_CREDIT_RESET_TARGET_IMMUTABLE'
                using errcode = 'check_violation';
        end if;
    end if;
    return new;
end;
$$;

drop trigger if exists admin_credit_reset_targets_guard_trg
    on public.admin_credit_reset_targets;

create trigger admin_credit_reset_targets_guard_trg
    before update on public.admin_credit_reset_targets
    for each row execute function public.admin_credit_reset_targets_guard();

revoke all on function public.admin_credit_reset_targets_guard()
    from public, anon, authenticated;

alter table public.admin_credit_reset_operations enable row level security;
alter table public.admin_credit_reset_targets enable row level security;

revoke all on table public.admin_credit_reset_operations from anon, authenticated;
revoke all on table public.admin_credit_reset_targets from anon, authenticated;

-- Service role only; no update on operations and no delete anywhere.
grant select, insert on table public.admin_credit_reset_operations to service_role;
grant select, insert, update on table public.admin_credit_reset_targets to service_role;
