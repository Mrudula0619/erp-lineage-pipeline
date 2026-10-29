-- Layers:  raw (immutable, append-only)  ->  core (typed, clean, linked)  ->  mart (reporting)
-- Side layers: quarantine (rejected records + reason), dq (check results, reconciliation breaks)

create schema if not exists raw;
create schema if not exists core;
create schema if not exists mart;
create schema if not exists quarantine;
create schema if not exists dq;

create table if not exists raw.load_runs (
    load_id          uuid primary key,
    started_at       timestamptz not null default now(),
    finished_at      timestamptz,
    status           text not null default 'running',
    source_base_url  text not null,
    erp_as_of        date,
    expected_counts  jsonb,
    received_counts  jsonb
);

-- One row per record exactly as the source returned it. Never updated, never deleted.
create table if not exists raw.records (
    raw_id          bigserial primary key,
    load_id         uuid not null references raw.load_runs(load_id),
    entity          text not null,
    source_id       text not null,
    source_url      text not null,   -- the exact API request that returned this record
    page            int  not null,
    position        int  not null,   -- index of the record inside that page
    extracted_at    timestamptz not null,
    payload         jsonb not null,
    payload_sha256  text not null    -- sha256 of canonical JSON; proves the payload is untouched
);
create index if not exists records_load_entity_idx on raw.records (load_id, entity, source_id);

create table if not exists core.customers (
    customer_id text primary key,
    name        text not null,
    region      text not null,
    created_at  date not null,
    status      text not null,
    raw_id      bigint not null references raw.records(raw_id),
    load_id     uuid   not null
);

create table if not exists core.invoices (
    invoice_id  text primary key,
    customer_id text not null references core.customers(customer_id),
    issued_on   date not null,
    due_on      date not null,
    currency    text not null,
    total       numeric(14,2) not null,
    status      text not null,
    raw_id      bigint not null references raw.records(raw_id),
    load_id     uuid   not null
);

create table if not exists core.invoice_lines (
    line_id     text primary key,
    invoice_id  text not null references core.invoices(invoice_id),
    product     text not null,
    quantity    int  not null,
    unit_price  numeric(14,2) not null,
    amount      numeric(14,2) not null,
    raw_id      bigint not null references raw.records(raw_id),
    load_id     uuid   not null
);

create table if not exists core.ledger_entries (
    entry_id    text primary key,
    invoice_id  text not null references core.invoices(invoice_id),
    posted_on   date not null,
    account     text not null,
    side        text not null check (side in ('debit','credit')),
    amount      numeric(14,2) not null,
    kind        text not null,
    raw_id      bigint not null references raw.records(raw_id),
    load_id     uuid   not null
);

create table if not exists quarantine.records (
    q_id       bigserial primary key,
    load_id    uuid not null,
    entity     text not null,
    source_id  text not null,
    raw_id     bigint not null references raw.records(raw_id),
    reason     text not null,
    detail     text
);
create index if not exists quarantine_load_idx on quarantine.records (load_id, entity, reason);

create table if not exists dq.check_results (
    result_id   bigserial primary key,
    load_id     uuid not null,
    check_name  text not null,
    layer       text not null,
    entity      text,
    severity    text not null check (severity in ('critical','warn','info')),
    violations  int  not null,
    status      text not null check (status in ('pass','fail','info')),
    detail      text,
    checked_at  timestamptz not null default now()
);

create table if not exists dq.recon_breaks (
    load_id        uuid not null,
    invoice_id     text not null,
    break_type     text not null,
    header_total   numeric(14,2),
    lines_total    numeric(14,2),
    ledger_revenue numeric(14,2),
    ledger_debit   numeric(14,2),
    ledger_credit  numeric(14,2),
    diff           numeric(14,2),
    raw_id         bigint not null,
    primary key (load_id, invoice_id, break_type)
);
