-- Bank connections made through Plaid Link, as an alternative to uploading
-- bank-statement PDFs. One row per Plaid Item (one login at one bank); an
-- applicant who connects two banks gets two rows.
--
-- Named application_plaid_items, not plaid_items: this Supabase project is
-- shared, and plaid_items already belongs to Paradise's hosted-link flow
-- (keyed by plaid_requests.request_id). An earlier version of this migration
-- used that name, and "create table if not exists" quietly created nothing.
-- Do not touch plaid_items or the pc_plaid_* tables (pathway_consultants) here.
--
-- What Plaid hands back is filed where uploads already go: each statement PDF
-- and the Asset Report PDF land in application-docs storage with an
-- application_files row (doc_type 'bank_statement' / 'asset_report'), so the
-- dashboard, the docs email and the missing-docs check see them the same way
-- they see an upload. This table only records the connection itself and how the
-- pull went.
--
-- The access token is a standing credential to the applicant's bank data. It is
-- held as a Fernet token (same key as the IDIQ password) and never leaves
-- app.py: not in an API response, not in an audit payload.

create table if not exists application_plaid_items (
  id                       bigint generated always as identity primary key,
  application_id           bigint not null references applications(id) on delete cascade,
  item_id                  text not null unique,
  access_token_encrypted   text not null,
  institution_id           text,
  institution_name         text,
  -- Display only: name, mask, subtype per account. No balances, no numbers.
  accounts                 jsonb not null default '[]'::jsonb,
  -- fetching -> complete | partial | failed
  status                   text not null default 'fetching'
                           check (status in ('fetching', 'complete', 'partial', 'failed')),
  statements_count         int not null default 0,
  asset_report_ready       boolean not null default false,
  last_error               text,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);

create index if not exists application_plaid_items_app_idx
  on application_plaid_items (application_id);

-- Same reasoning as application_events: app.py talks to this table with the
-- service role only, so RLS with no policies closes it to anon/authenticated
-- without changing anything for the app.
alter table application_plaid_items enable row level security;
