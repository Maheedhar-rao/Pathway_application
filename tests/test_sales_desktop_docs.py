"""The sales desktop's road into this app: finding a merchant's submission by
the owner's email (POST /partner/submission-lookup), and processing statements
on /upload-documents/<sid>. Supabase is faked; CSRF stays on, as in production.

Run: venv/bin/python -m pytest tests/test_sales_desktop_docs.py
"""
import io
import os
import sys
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE", "x" * 40)
os.environ["APP_SECRET"] = "test-secret"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402
from itsdangerous import URLSafeTimedSerializer  # noqa: E402

import app as pathway  # noqa: E402

ROWS = [
    {"id": 10, "created_at": "2026-10-01T00:00:00", "payload": {"owner_0_email": "Jo_Smith@Shop.com"}},
    {"id": 12, "created_at": "2026-11-01T00:00:00", "payload": {"owner_0_email": "jo_smith@shop.com"}},
    # Would match "jo_smith" if the underscore were left a wildcard.
    {"id": 13, "created_at": "2026-11-05T00:00:00", "payload": {"owner_0_email": "joxsmith@shop.com"}},
    {"id": 14, "created_at": "2026-09-01T00:00:00", "payload": {"owner_1_email": "partner@shop.com"}},
]


class _Query:
    def __init__(self):
        self.key = self.pattern = None

    def select(self, cols):
        self.key = cols.split("payload->>")[1] if "payload->>" in cols else None
        return self

    def ilike(self, col, pattern):
        self.pattern = pattern
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def execute(self):
        import re
        rx = "^" + re.sub(r"\\(.)|(%)|(_)|(.)", lambda m: re.escape(m.group(1)) if m.group(1)
                          else ".*" if m.group(2) else "." if m.group(3) else re.escape(m.group(4)),
                          self.pattern) + "$"
        hits = [r for r in ROWS if re.match(rx, r["payload"].get(self.key, ""), re.I)]
        return type("R", (), {"data": [{"id": r["id"], "created_at": r["created_at"],
                                        "email": r["payload"][self.key]} for r in hits]})


class _SB:
    def table(self, name):
        return _Query()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(pathway, "sb", _SB())
    monkeypatch.setattr(pathway, "log_event", lambda *a, **k: None)
    pathway.app.config.update(TESTING=True, WTF_CSRF_ENABLED=True)
    with pathway.app.test_client() as c:
        yield c


def token(email, secret="test-secret"):
    return URLSafeTimedSerializer(secret, salt=pathway.SALES_LOOKUP_SALT).dumps({"email": email})


def test_finds_the_newest_submission_by_email_ignoring_case(client):
    r = client.post("/partner/submission-lookup", json={"token": token("JO_SMITH@shop.com")})
    assert r.status_code == 200 and r.get_json() == {"submission_id": 12}


def test_an_underscore_is_not_a_wildcard(client):
    r = client.post("/partner/submission-lookup", json={"token": token("jo_smith@shop.com")})
    assert r.get_json()["submission_id"] != 13


def test_second_owner_email_matches(client):
    r = client.post("/partner/submission-lookup", json={"token": token("partner@shop.com")})
    assert r.get_json() == {"submission_id": 14}


def test_unknown_email_is_404(client):
    r = client.post("/partner/submission-lookup", json={"token": token("nobody@shop.com")})
    assert r.status_code == 404


def test_a_token_not_signed_with_app_secret_is_refused(client):
    r = client.post("/partner/submission-lookup", json={"token": token("jo_smith@shop.com", "wrong")})
    assert r.status_code == 403
    assert client.post("/partner/submission-lookup", json={}).status_code == 403


def test_processing_statements_are_stored_as_processing_statement(monkeypatch):
    stored = []
    monkeypatch.setattr(pathway, "_store_uploaded_file",
                        lambda sid, f, dtype, paths, failed: stored.append((f.filename, dtype)) or True)
    from werkzeug.datastructures import FileStorage, MultiDict
    files = MultiDict([
        ("bank_files", FileStorage(io.BytesIO(b"%PDF"), "may.pdf")),
        ("processing_statements", FileStorage(io.BytesIO(b"%PDF"), "square-may.pdf")),
        ("processing_statements", FileStorage(io.BytesIO(b"%PDF"), "square-jun.pdf")),
        ("id_doc", FileStorage(io.BytesIO(b"img"), "dl.jpg")),
    ])
    saved, _paths, failed = pathway._process_uploads(5, files)
    assert saved == ["bank_statement", "processing_statement", "id_doc"]
    assert ("square-jun.pdf", "processing_statement") in stored and not failed
