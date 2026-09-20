-- Append-only activity trace for an application: one row per thing that
-- happened to it, never updated in place (except the back-stamp below), read
-- back as a timeline on the admin detail view.
--
-- Modelled on CrocSign's crocsign_audit_events, with one difference that drives
-- the whole schema. There, every event belongs to a document that already
-- exists. Here the most useful half of the journey -- landing on a rep link,
-- walking the wizard, giving up on page 3 -- happens before any `applications`
-- row does. So events carry a `visit_id` minted when the form renders, and
-- app.py back-stamps `application_id` onto that visit's rows once the
-- application finally lands.
--
--   application_id null + visit_id set  -> a visit that never submitted
--   both set                            -> the journey behind a real lead
--   application_id set + visit_id null  -> a later or admin-side event
--                                          (resume link, IDIQ creds, PDF pull)

create table if not exists application_events (
  id             bigint generated always as identity primary key,
  application_id bigint references applications(id) on delete cascade,
  visit_id       text,
  event_type     text not null,
  -- Who caused it: the merchant filling the form, an admin on the dashboard,
  -- or the app itself (background email sends).
  actor          text not null default 'applicant'
                 check (actor in ('applicant', 'admin', 'system')),
  -- Attribution context captured at the moment of the event. Denormalized on
  -- purpose: a rep can be retired and a brand renamed afterwards, and the trail
  -- should still say which link was actually clicked.
  rep_code       text,
  brand_slug     text,
  ip             text,
  user_agent     text,
  -- Event-specific detail. Never form field values: the trail records that a
  -- step was reached or credentials were saved, not what was typed.
  payload        jsonb not null default '{}'::jsonb,
  created_at     timestamptz not null default now()
);

-- The timeline query: every event for one application, oldest first.
create index if not exists application_events_app_idx
  on application_events (application_id, id);

-- The back-stamp at submit time, and drop-off analysis, both filter on visit.
create index if not exists application_events_visit_idx
  on application_events (visit_id) where visit_id is not null;

-- Funnel questions ("how many form_viewed became application_submitted last
-- week") scan by type over a date range.
create index if not exists application_events_type_time_idx
  on application_events (event_type, created_at desc);

-- Drop-off report: visits that reached the form but never produced an
-- application, with the furthest wizard page each one got to.
--
--   select e.visit_id,
--          min(e.created_at)                              as started_at,
--          max((e.payload->>'step')::int)                 as furthest_step,
--          max(e.rep_code)                                as rep_code
--     from application_events e
--    where e.application_id is null
--      and e.created_at > now() - interval '7 days'
--    group by e.visit_id
--    order by started_at desc;
