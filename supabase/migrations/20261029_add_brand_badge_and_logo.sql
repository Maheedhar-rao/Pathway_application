-- Referral partners: co-branded entry links such as
--   /latin-district-nj  ->  "Pathway Catalyst X Latin District NJ"
--
-- A partner is a client_brands row with partner_contact set, created from the
-- Referral partners section of /admin/reps. Existing brands (Pathway Catalyst,
-- custom domains) leave all three columns NULL and render exactly as before.
--
-- `badge` replaces the whole header pill text ("Harvest Lending X <name>").
-- `logo` is a path in the application-docs storage bucket, served through
-- /brand-logo/<slug> so the bucket can stay private.
-- `partner_contact` is the partner's own contact person -- an outsider, not a
-- sales_reps row. Leads from the link are recorded with
-- rep_name = '<name> (partner: <contact>)' and no rep_email.
--
-- `name` stays the short company name: it also goes to Plaid (30 chars max)
-- and into notification emails, where the badge wording would not fit.

alter table client_brands
  add column if not exists badge           text check (badge is null or length(trim(badge)) > 0),
  add column if not exists logo            text,
  add column if not exists partner_contact text check (partner_contact is null or length(trim(partner_contact)) > 0);

-- An earlier draft of this migration tied partners to a sales rep. It never
-- shipped, but drop the column wherever that draft was applied.
alter table client_brands drop column if exists partner_rep;
