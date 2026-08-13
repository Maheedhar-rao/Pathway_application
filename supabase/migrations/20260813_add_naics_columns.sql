-- Promote the NAICS/SIC classification out of the payload JSON into first-class
-- columns so underwriting can query and index on them (lender restriction
-- matching + the SIC field on API submissions) instead of digging through the
-- payload blob. The full classify() result (titles, confidence, method,
-- classifier_version) still lives in payload->'naics'; these three columns are
-- just the match keys.
--
-- app.py stamps these on every new submission going forward. Historical rows are
-- filled by the copy-from-payload UPDATE below (for any row that already carried
-- payload.naics) and by tools/backfill_naics.py (re-classifies rows that never
-- got a classification -- as of this migration that is every existing row).

alter table applications
  add column if not exists naics        text,
  add column if not exists sic          text,
  add column if not exists naics_bucket text;

-- Copy from payload for any row that already has a stored classification.
-- Idempotent: only fills columns that are still NULL, so re-running is safe and
-- it never clobbers values written by the app or the backfill script.
update applications
   set naics        = payload->'naics'->>'naics',
       sic          = payload->'naics'->>'sic',
       naics_bucket = payload->'naics'->>'bucket'
 where payload ? 'naics'
   and naics is null;

-- Underwriting matches on the code and the bucket; index both.
create index if not exists applications_naics_idx        on applications (naics);
create index if not exists applications_naics_bucket_idx on applications (naics_bucket);
