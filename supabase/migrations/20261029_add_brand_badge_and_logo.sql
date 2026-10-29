-- Referral partners: co-branded entry links such as
--   /latin-district-nj?rep=tom  ->  "Pathway Catalyst X Latin District NJ"
--
-- A partner is a client_brands row with a partner_rep set, created from the
-- Referral partners section of /admin/reps. Existing brands (Pathway Catalyst,
-- custom domains) leave all three columns NULL and render exactly as before.
--
-- `badge` replaces the whole header pill text ("Harvest Lending X <name>").
-- `logo` is a path in the application-docs storage bucket, served through
-- /brand-logo/<slug> so the bucket can stay private.
-- `partner_rep` is the sales rep the partner's leads are attributed to; it
-- goes into the ?rep= of the partner's link.
--
-- `name` stays the short company name: it also goes to Plaid (30 chars max)
-- and into notification emails, where the badge wording would not fit.

alter table client_brands
  add column if not exists badge       text check (badge is null or length(trim(badge)) > 0),
  add column if not exists logo        text,
  add column if not exists partner_rep text references sales_reps(code);
