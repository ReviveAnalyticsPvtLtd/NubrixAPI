create schema api_ingestion;
revoke all on schema api_ingestion from public;

create table api_ingestion.sources (
    id uuid primary key,
    project_id text not null references public."Projects" ("projectId") on delete cascade,
    config jsonb not null,
    credential text not null,
    revision integer not null default 1,
    enabled boolean not null default true,
    needs_attention boolean not null default false,
    cadence_minutes integer not null check (cadence_minutes in (0, 15, 60, 1440)),
    next_due_at timestamptz,
    last_success_at timestamptz,
    unique (id, project_id)
);

create table api_ingestion.datasets (
    id uuid primary key,
    source_id uuid not null unique,
    project_id text not null,
    table_name text not null check (table_name ~ '^api__[a-f0-9]{32}$'),
    display_name text not null,
    description text,
    columns jsonb not null,
    active boolean not null default true,
    current_version_id uuid,
    foreign key (source_id, project_id) references api_ingestion.sources (id, project_id) on delete cascade,
    unique (project_id, table_name)
);

create table api_ingestion.runs (
    id uuid primary key,
    dataset_id uuid not null references api_ingestion.datasets (id) on delete cascade,
    source_revision integer not null,
    scheduled_at timestamptz,
    status text not null check (status in ('queued', 'running', 'retrying', 'succeeded', 'failed', 'cancelled')),
    attempt integer not null default 0,
    lease_expires_at timestamptz,
    retry_at timestamptz not null default now(),
    dispatch_after timestamptz not null default now(),
    received_rows bigint not null default 0,
    error_code text,
    created_at timestamptz not null default now(),
    finished_at timestamptz,
    unique (dataset_id, scheduled_at),
    unique (id, dataset_id)
);

create unique index api_ingestion_active_run on api_ingestion.runs (dataset_id)
    where status in ('queued', 'running', 'retrying');
create index api_ingestion_dispatch on api_ingestion.runs (retry_at)
    where status in ('queued', 'running', 'retrying');

create table api_ingestion.versions (
    id uuid primary key,
    dataset_id uuid not null references api_ingestion.datasets (id) on delete cascade,
    run_id uuid not null unique,
    object_name text not null unique,
    row_count bigint not null check (row_count >= 0),
    content_digest text not null,
    created_at timestamptz not null default now(),
    foreign key (run_id, dataset_id) references api_ingestion.runs (id, dataset_id) on delete cascade,
    unique (id, dataset_id)
);

alter table api_ingestion.datasets add constraint api_ingestion_current_version
    foreign key (current_version_id, id) references api_ingestion.versions (id, dataset_id)
    deferrable initially deferred;

-- Keep an upload receipt across project deletion so an interrupted worker's
-- private object can still be collected after the project's storage sweep.
create table api_ingestion.staged_objects (
    version_id uuid primary key,
    project_id text not null,
    object_name text not null unique check (object_name ~ '^api__[a-f0-9]{32}--[a-f0-9]{32}\.snapshot$'),
    created_at timestamptz not null default now()
);

create index api_ingestion_staged_age on api_ingestion.staged_objects (created_at);
create index api_ingestion_version_age on api_ingestion.versions (dataset_id, created_at desc);
