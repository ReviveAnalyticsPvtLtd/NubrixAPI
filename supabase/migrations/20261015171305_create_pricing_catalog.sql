-- Catalog prices are backend-only. The public row carries a revision notification,
-- never prices, customer information or administrator identity.
create schema if not exists private;

create table private.pricing_catalog_revisions (
    revision bigint generated always as identity primary key,
    admin_id uuid,
    admin_email text not null check (length(btrim(admin_email)) > 0),
    reason text not null check (length(btrim(reason)) between 5 and 500),
    published_at timestamptz not null default now()
);

create table private.pricing_catalog_prices (
    revision bigint not null references private.pricing_catalog_revisions(revision),
    currency text not null check (currency in ('INR', 'USD')),
    sku text not null check (sku in ('monthly', 'annual', 'small', 'medium', 'large')),
    amount_minor bigint not null check (amount_minor between 1 and 187649984473770),
    primary key (revision, currency, sku),
    constraint pricing_inr_minimum check (currency <> 'INR' or amount_minor >= 100)
);

create table private.pricing_catalog_publications (
    admin_id uuid not null,
    idempotency_key text not null check (length(idempotency_key) between 1 and 128),
    request_hash text not null check (length(request_hash) = 64),
    result_revision bigint not null references private.pricing_catalog_revisions(revision),
    no_op boolean not null,
    created_at timestamptz not null default now(),
    primary key (admin_id, idempotency_key)
);

create table public.pricing_catalog_state (
    id smallint primary key check (id = 1),
    revision bigint not null references private.pricing_catalog_revisions(revision),
    updated_at timestamptz not null default now()
);

alter table private.pricing_catalog_revisions enable row level security;
alter table private.pricing_catalog_prices enable row level security;
alter table private.pricing_catalog_publications enable row level security;
alter table public.pricing_catalog_state enable row level security;

revoke all on schema private from public, anon, authenticated;
revoke all on table private.pricing_catalog_revisions,
    private.pricing_catalog_prices, private.pricing_catalog_publications
    from public, anon, authenticated;
revoke all on table public.pricing_catalog_state from public, anon, authenticated;
grant usage on schema private to postgres, service_role;
grant select, insert on table private.pricing_catalog_revisions,
    private.pricing_catalog_prices, private.pricing_catalog_publications to postgres, service_role;
grant usage, select on sequence private.pricing_catalog_revisions_revision_seq to postgres, service_role;
grant select, update on table public.pricing_catalog_state to postgres, service_role;
grant select on table public.pricing_catalog_state to anon, authenticated;
create policy pricing_revision_public_read on public.pricing_catalog_state
    for select to anon, authenticated using (id = 1);

insert into private.pricing_catalog_revisions(admin_email, reason)
values ('system', 'Initial pricing catalog');
insert into private.pricing_catalog_prices(revision, currency, sku, amount_minor)
values
    (1, 'INR', 'monthly', 200000), (1, 'INR', 'annual', 2000000),
    (1, 'INR', 'small', 50000), (1, 'INR', 'medium', 200000), (1, 'INR', 'large', 350000),
    (1, 'USD', 'monthly', 2000), (1, 'USD', 'annual', 20000),
    (1, 'USD', 'small', 500), (1, 'USD', 'medium', 2000), (1, 'USD', 'large', 3500);
insert into public.pricing_catalog_state(id, revision) values (1, 1);

-- Only this singleton participates in the existing read-only price notification.
alter publication supabase_realtime add table public.pricing_catalog_state;
