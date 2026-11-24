-- Let application_files hold card processing statements.
--
-- /upload-documents has taken processing_statements since #47, but
-- application_files_doc_type_check did not know the type: the first real one
-- (application 1009, 2026-11-24 UTC) reached storage and had its row refused
-- with 23514 on every try.
--
-- As in 20261029, the value list is read from the database and one value is
-- added; nothing allowed today is dropped. That migration left the list as an
-- array literal ('{a,b}'::text[]), which it could not itself read back, so both
-- that and ARRAY['a', 'b'] are read here. Anything else stops and is printed.
-- Safe to re-run. No backfill: the sales desktop resends what was refused.

do $$
declare
  def  text;
  lit  text;
  vals text[];
begin
  select pg_get_constraintdef(c.oid) into def
    from pg_constraint c
   where c.conname = 'application_files_doc_type_check'
     and c.conrelid = 'public.application_files'::regclass;

  if def is null then
    return;
  end if;

  lit := substring(def from '^CHECK \(\(doc_type = ANY \(''(\{[^'']*\})''::text\[\]\)\)\)$');
  if lit is not null then
    vals := lit::text[];
  elsif def ~ '^CHECK \(\(doc_type = ANY \(ARRAY\[(''[^'']+''::text(, )?)+\]\)\)\)$' then
    select array_agg(m[1]) into vals from regexp_matches(def, '''([^'']+)''', 'g') as m;
  else
    raise exception 'application_files_doc_type_check is not a plain value list: %', def;
  end if;

  if 'processing_statement' = any(vals) then
    return;
  end if;

  alter table public.application_files drop constraint application_files_doc_type_check;
  execute format(
    'alter table public.application_files add constraint application_files_doc_type_check '
    'check (doc_type = any (%L::text[]))',
    vals || array['processing_statement']);
end $$;
