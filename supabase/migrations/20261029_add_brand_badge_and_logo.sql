-- Co-branded partner links, e.g. /latin-district-nj?rep=tom.
--
-- `badge` replaces the whole header pill text ("Harvest Lending X <name>")
-- when a partner needs different wording; NULL keeps the existing text.
-- `logo` is a file under static/brands/ shown next to the Pathway logo. It is
-- a bare filename, not a URL, so a row can only point at an image that was
-- shipped with the app -- adding one is a deploy, the row just turns it on.
--
-- `name` stays the short partner name: it also goes to Plaid (30 chars max)
-- and into notification emails, where the badge wording would not fit.

alter table client_brands
  add column if not exists badge text check (badge is null or length(trim(badge)) > 0),
  add column if not exists logo  text check (logo is null or logo ~ '^[a-z0-9][a-z0-9_.-]*\.(png|jpg|jpeg|svg|webp)$');

insert into client_brands (slug, name, badge, logo) values
  ('latin-district-nj', 'Latin District NJ',
   'Pathway Catalyst X Latin District NJ', 'latin-district-nj.png')
on conflict (slug) do nothing;
