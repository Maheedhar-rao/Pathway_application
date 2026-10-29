-- Let application_files hold Plaid Asset Reports.
--
-- _plaid_collect files the Asset Report PDF as doc_type 'asset_report' (see
-- 20261023_add_application_plaid_items.sql), but application_files_doc_type_check
-- predates Plaid and only knows the upload types. The first real connection
-- (application 844, Cathay Bank - Business, 2026-10-29) stored the PDF in
-- application-docs and then had its row refused three times with 23514, so the
-- connection read 'failed' and the docs email went out without the report.
--
-- The constraint isn't in any migration here, so its value list is read from
-- the database and 'asset_report' is added to it -- nothing it allows today is
-- dropped. If the constraint isn't a plain value list, this stops and prints it
-- rather than guess.

do $$
declare
  def  text;
  vals text[];
begin
  select pg_get_constraintdef(c.oid) into def
    from pg_constraint c
   where c.conname = 'application_files_doc_type_check'
     and c.conrelid = 'public.application_files'::regclass;

  if def is null or def like '%''asset_report''%' then
    return;
  end if;

  if def !~ 'doc_type' or def !~ 'ANY' or def !~ 'ARRAY\[' or def ~* '\s(AND|OR)\s' then
    raise exception 'application_files_doc_type_check is not a plain value list: %', def;
  end if;

  select array_agg(distinct m[1]) into vals
    from regexp_matches(def, '''([^'']+)''', 'g') as m;

  alter table public.application_files drop constraint application_files_doc_type_check;
  execute format(
    'alter table public.application_files add constraint application_files_doc_type_check '
    'check (doc_type = any (%L::text[]))',
    vals || array['asset_report']);
end $$;

-- Backfill: Asset Reports already sitting in storage without their row.
insert into public.application_files (application_id, filename, storage_path, size_bytes, doc_type)
select split_part(o.name, '/', 1)::bigint,
       -- stored as <8 hex>_<original name>
       regexp_replace(split_part(o.name, '/', 3), '^[0-9a-f]{8}_', ''),
       o.name,
       (o.metadata->>'size')::bigint,
       'asset_report'
  from storage.objects o
  join public.applications a on a.id::text = split_part(o.name, '/', 1)
 where o.bucket_id = 'application-docs'
   and split_part(o.name, '/', 1) ~ '^[0-9]+$'
   and split_part(o.name, '/', 2) = 'asset_report'
   and not exists (select 1 from public.application_files f where f.storage_path = o.name);

-- The connection whose only error was that one refused file now has it.
update public.application_plaid_items p
   set status = 'complete', asset_report_ready = true, last_error = null, updated_at = now()
 where p.last_error = 'storage: 1 file(s) failed'
   and not p.asset_report_ready
   and exists (select 1 from public.application_files f
                where f.application_id = p.application_id and f.doc_type = 'asset_report');
