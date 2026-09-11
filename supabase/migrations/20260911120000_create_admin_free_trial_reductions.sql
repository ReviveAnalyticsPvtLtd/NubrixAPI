create table public.admin_free_trial_reductions (
    id uuid primary key default gen_random_uuid(),
    idempotency_key uuid not null unique,
    request_hash text not null,
    user_id text not null,
    subscription_id uuid references public.subscriptions(id) on delete set null,
    requested_by uuid not null references public.admin_users(id),
    days integer not null,
    reason text not null,
    outcome text not null default 'PENDING',
    days_removed integer,
    previous_expiry timestamptz,
    new_expiry timestamptz,
    access_still_banned boolean not null default false,
    error_code text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    completed_at timestamptz,
    constraint admin_free_trial_reductions_hash_chk
        check (request_hash ~ '^[0-9a-f]{64}$'),
    constraint admin_free_trial_reductions_days_chk
        check (days between 1 and 30),
    constraint admin_free_trial_reductions_reason_chk
        check (char_length(btrim(reason)) between 1 and 1000),
    constraint admin_free_trial_reductions_outcome_chk
        check (outcome in ('PENDING', 'REDUCED', 'FAILED')),
    constraint admin_free_trial_reductions_days_removed_chk
        check (days_removed is null or days_removed between 1 and 30),
    constraint admin_free_trial_reductions_shape_chk
        check (
            (
                outcome = 'PENDING'
                and completed_at is null
                and days_removed is null
                and previous_expiry is null
                and new_expiry is null
                and error_code is null
            )
            or (
                outcome = 'REDUCED'
                and completed_at is not null
                and days_removed is not null
                and days_removed = days
                and previous_expiry is not null
                and new_expiry is not null
                and new_expiry < previous_expiry
                and previous_expiry - new_expiry = days * interval '1 day'
                and error_code is null
            )
            or (
                outcome = 'FAILED'
                and completed_at is not null
                and days_removed is null
                and previous_expiry is null
                and new_expiry is null
                and error_code is not null
            )
        )
);

comment on table public.admin_free_trial_reductions is
    'Idempotent single-user administrator free-trial reduction ledger.';
comment on column public.admin_free_trial_reductions.user_id is
    'Product user identifier. The user-erasure workflow removes matching operations.';

create index admin_free_trial_reductions_requested_by_idx
    on public.admin_free_trial_reductions (requested_by, created_at desc);

create index admin_free_trial_reductions_user_idx
    on public.admin_free_trial_reductions (user_id, created_at desc);

alter table public.admin_free_trial_reductions enable row level security;

revoke all on table public.admin_free_trial_reductions
    from anon, authenticated;

grant select, insert, update, delete
    on table public.admin_free_trial_reductions to service_role;
