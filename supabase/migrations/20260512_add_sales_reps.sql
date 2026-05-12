-- Sales reps live in the DB so admins can manage them from the rep-links page
-- instead of editing app.py. `code` is the URL slug (`/?rep=<code>`) and is
-- stored lowercase so lookups are case-insensitive without extra indexing.
-- Soft-delete via `active=false` preserves existing rep_name/rep_email on
-- historical applications and lets old links 404 gracefully.

create table if not exists sales_reps (
  code        text primary key check (code = lower(code) and length(code) between 1 and 64),
  name        text not null check (length(trim(name)) > 0),
  email       text not null check (position('@' in email) > 1),
  active      boolean not null default true,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index if not exists sales_reps_active_idx on sales_reps (active);

-- Keep updated_at fresh on edits.
create or replace function sales_reps_touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end$$;

drop trigger if exists sales_reps_touch on sales_reps;
create trigger sales_reps_touch
  before update on sales_reps
  for each row execute function sales_reps_touch_updated_at();

-- Seed existing hardcoded reps. ON CONFLICT DO NOTHING so re-running is safe
-- and admin edits to these rows are preserved.
insert into sales_reps (code, name, email) values
  ('yuly',     'Yuly',     'deals@pathwaycatalyst.com'),
  ('tom',      'Tom',      'tom@pathwaycatalyst.com'),
  ('troy',     'Troy',     'troy@pathwaycatalyst.com'),
  ('adrian',   'Adrian',   'adrian@pathwaycatalyst.com'),
  ('frank',    'Frank',    'frank@pathwaycatalyst.com'),
  ('andres',   'Andres',   'andres@pathwaycatalyst.com'),
  ('ethan',    'Ethan',    'ethan@pathwaycatalyst.com'),
  ('juliette', 'Juliette', 'juliette@pathwaycatalyst.com')
on conflict (code) do nothing;
