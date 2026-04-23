-- Track whether the initial application email and the docs follow-up email
-- were sent successfully. NULL means never sent; timestamp means most recent
-- successful send. Lets ops identify submissions that silently missed email.

alter table applications
  add column if not exists initial_email_sent_at timestamptz,
  add column if not exists docs_email_sent_at timestamptz;

-- Daily audit query:
--   select id, created_at, business_legal_name
--   from applications a
--   where initial_email_sent_at is null
--      or (exists (select 1 from application_files f where f.application_id = a.id)
--          and docs_email_sent_at is null);
