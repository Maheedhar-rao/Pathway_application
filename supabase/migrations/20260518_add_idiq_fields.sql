-- IDIQ credentials captured on the final wizard page. We hold the username in
-- plain text (needed to look up the account) and the password as a Fernet
-- token; only app.py with IDIQ_PASSWORD_KEY can decrypt.
alter table applications
  add column if not exists idiq_username text,
  add column if not exists idiq_password_encrypted text;
