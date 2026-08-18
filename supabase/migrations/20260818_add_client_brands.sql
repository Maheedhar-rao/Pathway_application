-- Client brands: the branded entry points reps hand to merchants.
--
-- A brand renders as either
--   * its own domain  -> https://application.croccrm.com/?rep=tom
--   * a path slug     -> https://<this app>/pathway-catalyst?rep=tom
-- so a client can be onboarded with a slug immediately and switched to a
-- vanity domain later without reissuing rep links (both keep resolving).
--
-- Lives in the DB rather than app.py for the same reason sales_reps does:
-- admins add a client from /admin/reps without a deploy.
--
-- `domain` is the bare host (no scheme, no path, no port) and is stored
-- lowercase so host matching on inbound requests is a plain dict lookup.
-- It stays NULL until DNS actually points at this app -- a domain set here
-- is copied straight into rep links, so an unrouted one hands out dead URLs.

create table if not exists client_brands (
  slug        text primary key check (slug = lower(slug) and slug ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
  name        text not null check (length(trim(name)) > 0),
  domain      text unique check (domain is null or domain ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'),
  active      boolean not null default true,
  is_default  boolean not null default false,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index if not exists client_brands_active_idx on client_brands (active);

-- At most one default brand. The default is what /admin/reps preselects and
-- what bare /?rep= links are branded as.
create unique index if not exists client_brands_one_default_idx
  on client_brands (is_default) where is_default;

create or replace function client_brands_touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end$$;

drop trigger if exists client_brands_touch on client_brands;
create trigger client_brands_touch
  before update on client_brands
  for each row execute function client_brands_touch_updated_at();

-- Seed the brand that was hardcoded in app.py (CLIENTS/DEFAULT_CLIENT_SLUG) so
-- existing /pathway-catalyst?rep= links keep working byte-for-byte. Domains are
-- deliberately left NULL -- set them from /admin/reps once DNS resolves.
insert into client_brands (slug, name, domain, is_default) values
  ('pathway-catalyst', 'Pathway Catalyst', null, true)
on conflict (slug) do nothing;
