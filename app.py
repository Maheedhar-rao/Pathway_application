#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Public form → Thank You (optional uploads). Separate admin dashboard.
JSON APIs power the dashboard. Stores data in Supabase (REST), no DB URL.
Each sales rep gets a unique link that tracks their submissions.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import smtplib
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from html import escape
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from io import BytesIO
from pathlib import Path
from typing import List, Optional

import socket as _socket

# Force IPv4 DNS resolution — Railway has no outbound IPv6, causing
# [Errno 101] Network is unreachable when Python tries AAAA records first.
_orig_getaddrinfo = _socket.getaddrinfo

def _ipv4_only_getaddrinfo(*args, **kwargs):
    results = _orig_getaddrinfo(*args, **kwargs)
    ipv4 = [r for r in results if r[0] == _socket.AF_INET]
    return ipv4 if ipv4 else results

_socket.getaddrinfo = _ipv4_only_getaddrinfo

from functools import wraps

from flask import (
    Flask, request, redirect, url_for, render_template, jsonify,
    send_from_directory, send_file, abort, session, has_request_context
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash
from supabase import create_client, Client
# SyncClientOptions, not the ClientOptions base: only the sync subclass carries
# the storage the sync client expects. This is the alias the library uses itself.
from supabase.lib.client_options import SyncClientOptions as ClientOptions
from dotenv import load_dotenv, find_dotenv

# PDF generation
try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        Image, HRFlowable, BaseDocTemplate, Frame, PageTemplate, PageBreak
    )
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_JUSTIFY
    PDF_ENABLED = True
except ImportError:
    PDF_ENABLED = False
    logging.warning("reportlab not installed. PDF generation disabled. Run: pip install reportlab")

# IDIQ password encryption
from cryptography.fernet import Fernet, InvalidToken

# Signed expiring tokens for the "resume application" magic links
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# ── Eastern time ───────────────────────────────────────────────────────────
# The business runs on Eastern, and this app stamps a human-readable "Submitted"
# time onto the notification email and the application PDF. We format those with
# an explicit, timezone-aware datetime (datetime.now(EASTERN)) rather than a bare
# datetime.now(), so correctness never depends on the process/system clock. The
# `tzdata` package is pinned in requirements so ZoneInfo resolves even on a slim
# container with no system tz database.
#
# Earlier this relied on pinning the process TZ (os.environ["TZ"] + tzset()).
# That silently no-oped on Railway -- the platform sets TZ=UTC, so datetime.now()
# stayed UTC and the "Submitted" line printed the UTC wall clock labelled "ET"
# (e.g. 12:43 PM ET shown as 04:43 PM ET). ZoneInfo removes that dependency.
EASTERN = ZoneInfo("America/New_York")

# Still nudge the process TZ for nicer log timestamps, but never fatally -- the
# ET stamping above no longer depends on it.
os.environ.setdefault("TZ", "America/New_York")
if hasattr(time, "tzset"):          # no-op on Windows
    time.tzset()
log.info("Process timezone: %s | Eastern zone: %s", time.tzname, EASTERN.key)

load_dotenv(find_dotenv())

APP_DIR = Path(__file__).resolve().parent
STORAGE_BUCKET = "application-docs"
SIGNED_URL_EXPIRY = 3600  # 1 hour

# ---- Config (no DB URL needed) ----------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE = os.environ.get("SUPABASE_SERVICE_ROLE")
if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
    raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE in your environment.")

# Email config (optional - for sending PDFs to reps)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "tech@pathwaycatalyst.com")
EMAIL_ENABLED = bool(SMTP_USER and SMTP_PASS)

# Resend API (preferred on Railway where SMTP is blocked)
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

# SAM.gov Entity API (free, for business verification)
SAM_GOV_API_KEY = os.environ.get("SAM_GOV_API_KEY", "")

# Main team email - receives ALL submissions
TEAM_EMAIL = os.environ.get("TEAM_EMAIL", "team@pathwaycatalyst.com")

# Internal inboxes that receive the full application package alongside
# TEAM_EMAIL. Blank disables that recipient rather than sending to a
# placeholder address.
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "").strip()
PROCESSING_EMAIL = os.environ.get("PROCESSING_EMAIL", "").strip()

# External URL the applicant follows on page 6 to create their IDIQ account.
# Placeholder until the real partner URL is provisioned.
IDIQ_SIGNUP_URL = os.environ.get(
    "IDIQ_SIGNUP_URL",
    "https://www.idiq.com/sign-up/"  # TODO: replace with partner-specific URL
)

# Fernet key used to encrypt the IDIQ password the applicant types on page 7.
# In production set IDIQ_PASSWORD_KEY (output of `Fernet.generate_key().decode()`).
# If unset, generate ephemeral so dev still runs — but stored passwords become
# unrecoverable after each restart, hence the loud warning.
_idiq_key_env = os.environ.get("IDIQ_PASSWORD_KEY", "").strip()
if _idiq_key_env:
    try:
        _IDIQ_FERNET = Fernet(_idiq_key_env.encode())
    except Exception as exc:
        raise RuntimeError(f"IDIQ_PASSWORD_KEY is not a valid Fernet key: {exc}")
else:
    logging.warning(
        "IDIQ_PASSWORD_KEY not set — generating ephemeral key. "
        "Stored IDIQ passwords will be UNRECOVERABLE across restarts."
    )
    _IDIQ_FERNET = Fernet(Fernet.generate_key())

def encrypt_idiq_password(plain: str) -> str:
    if not plain:
        return ""
    return _IDIQ_FERNET.encrypt(plain.encode("utf-8")).decode("utf-8")

def decrypt_idiq_password(token: str) -> Optional[str]:
    if not token:
        return None
    try:
        return _IDIQ_FERNET.decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


# ── Resume tokens ────────────────────────────────────────────────────────────
# Magic link in the applicant receipt email lets the merchant come back to
# finish their IDIQ signup without re-filling the application. Tokens are
# signed with APP_SECRET and expire after 30 days. Admin can resend a fresh
# token from the dashboard; merchants can self-serve via the expired-link page.
RESUME_TOKEN_MAX_AGE_SECONDS = 30 * 86400  # 30 days

def _resume_serializer() -> URLSafeTimedSerializer:
    # Built lazily so a key rotation via APP_SECRET takes effect on next request
    # without needing a module reload.
    return URLSafeTimedSerializer(
        os.environ.get("APP_SECRET", "dev-secret"),
        salt="resume-link-v1",
    )

def sign_resume_token(sid: int) -> str:
    return _resume_serializer().dumps({"sid": int(sid)})

def verify_resume_token(token: str) -> tuple[Optional[int], str]:
    """Return (sid, status). status is one of: 'ok', 'expired', 'invalid'."""
    if not token:
        return None, "invalid"
    try:
        data = _resume_serializer().loads(token, max_age=RESUME_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired:
        return None, "expired"
    except BadSignature:
        return None, "invalid"
    sid = data.get("sid") if isinstance(data, dict) else None
    return (int(sid) if sid else None), ("ok" if sid else "invalid")

# ---- Client Branding --------------------------------------------------------
# Brands are the entry points reps hand to merchants. Each one renders a link
# two ways, and both keep resolving forever:
#   * custom domain -> https://application.croccrm.com/?rep=tom
#   * path slug     -> https://<this app>/pathway-catalyst?rep=tom
# A client can go live on a slug today and move to a vanity domain later
# without reissuing a single rep link.
#
# Brands live in the Supabase `client_brands` table (migration
# 20260818_add_client_brands.sql) so admins add a client from /admin/reps
# without a deploy — same reasoning as sales_reps. Display-only: branding does
# not change what is submitted or where it lands, and bare /?rep=tom links
# keep working unchanged.
_BRAND_CACHE_TTL = 60  # seconds; bumped by writes via _invalidate_brand_cache()
_brand_cache: dict = {"brands": None, "expires_at": 0.0, "table_ok": True}
_brand_cache_lock = threading.Lock()

# Used only when `client_brands` is missing or empty — mirrors what was
# hardcoded here before the table existed, so a deploy that lands ahead of the
# migration still serves branded /pathway-catalyst links instead of 404ing.
_FALLBACK_BRANDS = [
    {"slug": "pathway-catalyst", "name": "Pathway Catalyst", "domain": None,
     "active": True, "is_default": True},
]

def _brand_sort_key(b: dict):
    return (not b.get("is_default"), not b.get("active", True), b.get("name", "").lower())

def _load_brands_from_db() -> list:
    try:
        res = sb.table("client_brands").select(
            "slug, name, domain, active, is_default"
        ).execute()
        rows = res.data or []
    except Exception:
        # Never fatal: a missing table would otherwise take down the public
        # form, which only needs a display name.
        log.exception("client_brands unavailable; using built-in brand")
        _brand_cache["table_ok"] = False
        return [dict(b) for b in _FALLBACK_BRANDS]
    _brand_cache["table_ok"] = True
    if not rows:
        return [dict(b) for b in _FALLBACK_BRANDS]
    out = []
    for r in rows:
        slug = (r.get("slug") or "").lower().strip()
        if not slug:
            continue
        out.append({
            "slug": slug,
            "name": r.get("name") or slug,
            "domain": (r.get("domain") or "").lower().strip() or None,
            "active": bool(r.get("active", True)),
            "is_default": bool(r.get("is_default", False)),
        })
    out.sort(key=_brand_sort_key)
    return out

def _get_brands_cached() -> list:
    now = time.time()
    if _brand_cache["brands"] is not None and now < _brand_cache["expires_at"]:
        return _brand_cache["brands"]
    with _brand_cache_lock:
        if _brand_cache["brands"] is not None and time.time() < _brand_cache["expires_at"]:
            return _brand_cache["brands"]
        _brand_cache["brands"] = _load_brands_from_db()
        _brand_cache["expires_at"] = time.time() + _BRAND_CACHE_TTL
        return _brand_cache["brands"]

def _invalidate_brand_cache() -> None:
    with _brand_cache_lock:
        _brand_cache["brands"] = None
        _brand_cache["expires_at"] = 0.0

def get_brand_by_slug(slug: str, include_inactive: bool = False) -> Optional[dict]:
    slug = (slug or "").lower().strip()
    for b in _get_brands_cached():
        if b["slug"] == slug and (include_inactive or b["active"]):
            return b
    return None

def get_brand_by_host(host: str) -> Optional[dict]:
    """Match an inbound request host against a brand's custom domain.

    Port is stripped so this works behind the proxy and in local dev; inactive
    brands still match, because a domain that is live in DNS should keep
    rendering its own name rather than a competitor's until DNS is cut over.
    """
    host = (host or "").lower().strip().split(":")[0]
    if not host:
        return None
    for b in _get_brands_cached():
        if b["domain"] and b["domain"] == host:
            return b
    return None

def get_default_brand() -> Optional[dict]:
    brands = _get_brands_cached()
    for b in brands:
        if b["is_default"] and b["active"]:
            return b
    for b in brands:
        if b["active"]:
            return b
    return None

def brand_link_base(brand: Optional[dict]) -> str:
    """Base URL a rep link is built on: `f"{brand_link_base(b)}?rep={code}"`.

    A brand with a domain owns its root path; one without borrows this app's
    host and identifies itself with a path segment.
    """
    host_base = request.host_url.rstrip("/")
    if brand and brand.get("domain"):
        return f"https://{brand['domain']}/"
    if brand:
        return f"{host_base}/{brand['slug']}"
    return f"{host_base}/"

def brand_rep_link(brand: Optional[dict], rep_code: str) -> str:
    return f"{brand_link_base(brand)}?rep={rep_code}"

def current_brand() -> Optional[dict]:
    """Brand for the host this request came in on, for pages with no slug.

    A merchant who started on application.croccrm.com stays on that host
    through /thank-you, so the name follows them. Path-slug brands can't be
    recovered here (the slug is only on the entry URL) and fall back to the
    default, which is what those links rendered before brands existed.
    """
    return get_brand_by_host(request.host) or get_default_brand()

def current_brand_name() -> Optional[str]:
    brand = current_brand()
    return brand["name"] if brand else None

# ---- Sales Rep Configuration ------------------------------------------------
# Reps live in the Supabase `sales_reps` table (see migration
# 20260512_add_sales_reps.sql). Admins manage them via the /admin/reps page.
# URL format: /?rep=<code>  e.g., /?rep=tom. Branded variants resolve to the
# same form — see Client Branding above for the domain/slug entry points.
_REP_CACHE_TTL = 60  # seconds; bumped explicitly by writes via _invalidate_rep_cache()
_rep_cache: dict = {"reps": None, "expires_at": 0.0}
_rep_cache_lock = threading.Lock()

def _load_reps_from_db() -> dict:
    """Fetch every rep (active and inactive) keyed by lowercase code."""
    res = sb.table("sales_reps").select("code, name, email, active, created_at, updated_at").execute()
    rows = res.data or []
    out = {}
    for r in rows:
        code = (r.get("code") or "").lower()
        if code:
            out[code] = r
    return out

def _get_reps_cached() -> dict:
    now = time.time()
    if _rep_cache["reps"] is not None and now < _rep_cache["expires_at"]:
        return _rep_cache["reps"]
    with _rep_cache_lock:
        if _rep_cache["reps"] is not None and time.time() < _rep_cache["expires_at"]:
            return _rep_cache["reps"]
        _rep_cache["reps"] = _load_reps_from_db()
        _rep_cache["expires_at"] = time.time() + _REP_CACHE_TTL
        return _rep_cache["reps"]

def _invalidate_rep_cache() -> None:
    with _rep_cache_lock:
        _rep_cache["reps"] = None
        _rep_cache["expires_at"] = 0.0

def get_rep_info(rep_code: str, include_inactive: bool = False) -> Optional[dict]:
    """Get rep info by code, case-insensitive. Inactive reps are hidden by default."""
    if not rep_code:
        return None
    rec = _get_reps_cached().get(rep_code.lower().strip())
    if not rec:
        return None
    if not include_inactive and not rec.get("active", True):
        return None
    return {"name": rec["name"], "email": rec["email"]}

def sign_rep_code(rep_code: str) -> str:
    """Generate HMAC signature to prevent rep_code tampering."""
    key = (os.environ.get("APP_SECRET", "dev-secret")).encode()
    return hmac.new(key, rep_code.lower().strip().encode(), hashlib.sha256).hexdigest()

def verify_rep_code(rep_code: str, signature: str) -> bool:
    """Verify that rep_code has not been tampered with."""
    if not rep_code or not signature:
        return not rep_code  # no rep is valid (direct submission)
    expected = sign_rep_code(rep_code)
    return hmac.compare_digest(expected, signature)

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)

# ---- Supabase Storage helpers ------------------------------------------------
def _upload_to_storage(file_data: bytes, bucket_path: str, content_type: str = "application/pdf") -> int:
    """Upload file bytes to Supabase Storage. Returns size in bytes."""
    sb.storage.from_(STORAGE_BUCKET).upload(
        path=bucket_path,
        file=file_data,
        file_options={"content-type": content_type, "x-upsert": "true"},
    )
    return len(file_data)

def _download_from_storage(bucket_path: str) -> bytes:
    """Download file bytes from Supabase Storage."""
    return sb.storage.from_(STORAGE_BUCKET).download(bucket_path)

def _get_signed_url(bucket_path: str, expires_in: int = SIGNED_URL_EXPIRY) -> str:
    """Generate a time-limited signed URL for a private file."""
    result = sb.storage.from_(STORAGE_BUCKET).create_signed_url(bucket_path, expires_in)
    return result["signedURL"]

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("APP_SECRET", "dev-secret")
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB upload cap

# Railway terminates TLS at the edge and forwards plain HTTP to this app.
# Without ProxyFix, request.host_url returns http://... — which made the rep
# tracking links rendered on /admin/reps come out as insecure URLs. Trust the
# X-Forwarded-Proto / X-Forwarded-Host headers from one proxy hop.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

from flask_wtf.csrf import CSRFProtect, generate_csrf
csrf = CSRFProtect(app)

# -------------------- Activity Trace --------------------
# Append-only record of what happened to an application -- the same idea as
# CrocSign's crocsign_audit_events, replayed as a timeline on the admin detail
# view. Schema and the drop-off query live in the migration,
# supabase/migrations/20260920_add_application_events.sql.
#
# What differs from CrocSign is where the trail starts. There, every event
# belongs to a document that already exists. Here the most useful half of the
# journey happens before any `applications` row does: which rep link was
# clicked, how far up the wizard the merchant got, whether they closed the tab
# on page 3. So events are keyed by a visit id minted when the form renders and
# carried in the signed session cookie, and attach_visit_to_application() points
# that visit's rows at the application once it lands. Rows left holding a visit
# and no application are the drop-offs.
#
# Two rules hold everywhere below:
#   * Tracing never breaks a request. Every write swallows its own exceptions --
#     a failed audit insert must not cost a lead.
#   * Payloads carry metadata, never form content: "reached step 3", "saved
#     credentials", "three bank statements", never what was typed. The one
#     exception is an email address we just sent something to, which the
#     dashboard already displays anyway and which is the whole point of a
#     "link sent" line.
SESSION_VISIT_KEY = "visit_id"
# The application this visit produced, kept server-side so events fired after
# the submit can be filed against it without trusting the browser to name an id.
SESSION_VISIT_APP_KEY = "visit_app_id"

# Event types the unauthenticated beacon (/api/activity) is allowed to write.
# Anything else is dropped: the browser may only add to the trail in ways we
# already expect, never forge an "application_submitted" that makes it lie.
PUBLIC_EVENT_TYPES = {"form_step_viewed", "form_abandoned", "form_identity_captured"}

# Page 1 asks for these five and requires every one of them, so a visit that
# reaches page 2 has them filled and browser-validated. Captured there and
# nowhere else: it is the earliest point at which an abandoned application can
# be followed up, and the only alternative is a half-filled lead nobody can
# contact.
#
# This is a deliberate exception to the rule that payloads carry metadata and
# never form content. It is contact detail for a lead, held before the
# applicant has authorized anything -- the authorization block is on page 3 --
# so it is the one place in the trail that needs to be covered by a privacy
# policy and a retention decision. Nothing else from the form is taken: no
# SSN, no EIN, no bank detail, no signature.
PUBLIC_IDENTITY_FIELDS = {
    "name": 120, "business": 200, "email": 254, "mobile": 40, "amount": 24,
}

# Beacon writes allowed per visit. A real wizard run produces a handful; past
# this it is a redirect loop or someone poking the endpoint by hand.
_VISIT_EVENT_CAP = 60
_visit_event_counts: dict = {}
_visit_event_lock = threading.Lock()

# Flipped off if the table turns out to be missing, so a deploy that lands ahead
# of the migration degrades to "no tracing" instead of throwing on every page
# view. Same defensive shape as the brand cache's table_ok.
_events_table_ok = True

# Tracing gets its own Supabase client, with a short timeout.
#
# The shared `sb` keeps the library default of 120s, which is right for a
# submission insert: that request *is* the lead, and waiting two minutes beats
# losing it. It is wrong for an audit row. log_event sits on the form-render
# path, and a Supabase that hangs rather than fails would park a gunicorn
# worker for the full two minutes on every page view -- with four workers, that
# takes the application form down for everyone to record that someone looked at
# it. Better to drop the event and serve the form.
EVENTS_TIMEOUT_SECONDS = float(os.environ.get("EVENTS_TIMEOUT_SECONDS", "2.5"))
_events_sb: Client = create_client(
    SUPABASE_URL, SUPABASE_SERVICE_ROLE,
    options=ClientOptions(postgrest_client_timeout=EVENTS_TIMEOUT_SECONDS),
)


def _is_missing_table(exc: Exception) -> bool:
    msg = str(exc).lower()
    return ("pgrst205" in msg or "does not exist" in msg
            or "could not find the table" in msg)


def _client_ip() -> Optional[str]:
    """Caller IP. ProxyFix has already unwrapped one hop of X-Forwarded-For."""
    return request.remote_addr if has_request_context() else None


def _user_agent() -> Optional[str]:
    if not has_request_context():
        return None
    return (request.headers.get("User-Agent") or "")[:500] or None


def current_visit_id() -> Optional[str]:
    return session.get(SESSION_VISIT_KEY) if has_request_context() else None


def start_visit() -> str:
    """Mint a fresh visit id for this browser session.

    A new id per form render rather than one reused forever: a reload restarts
    the client-side wizard anyway, so this keeps one run of steps per real
    attempt instead of interleaving two into the same trail.
    """
    visit_id = uuid.uuid4().hex
    session[SESSION_VISIT_KEY] = visit_id
    session.pop(SESSION_VISIT_APP_KEY, None)
    return visit_id


def log_event(event_type: str, *, application_id: Optional[int] = None,
              visit_id: Optional[str] = None, actor: str = "applicant",
              payload: Optional[dict] = None, rep_code: str = "",
              brand_slug: str = "") -> None:
    """Append one row to the trace. Best-effort: this never raises."""
    global _events_table_ok
    if not _events_table_ok:
        return
    try:
        row = {"event_type": event_type, "actor": actor, "payload": payload or {}}
        if application_id is not None:
            row["application_id"] = int(application_id)
        # Only an applicant's own events inherit the session visit. An admin
        # whose browser once opened the public form still carries that visit in
        # their cookie, and without this every dashboard click they made landed
        # in some merchant's journey.
        vid = visit_id if visit_id is not None else (
            current_visit_id() if actor == "applicant" else None)
        if vid:
            row["visit_id"] = vid
        if rep_code:
            row["rep_code"] = rep_code.lower().strip()[:64]
        if brand_slug:
            row["brand_slug"] = brand_slug[:64]
        ip = _client_ip()
        if ip:
            row["ip"] = ip
        ua = _user_agent()
        if ua:
            row["user_agent"] = ua
        _events_sb.table("application_events").insert(row).execute()
    except Exception as exc:
        if _is_missing_table(exc):
            _events_table_ok = False
            log.error("application_events table is missing -- activity tracing "
                      "is off until 20260920_add_application_events.sql is "
                      "applied and the app restarts")
        else:
            log.exception("log_event(%s) failed", event_type)


def attach_visit_to_application(visit_id: Optional[str], sid: int) -> None:
    """Point everything this visit already did at the application it produced.

    Until the insert lands there is no application_id to file the entry link and
    the wizard steps under, so they sit with a visit id alone. This is the join:
    after it, one query replays the whole journey on the detail view. Only rows
    still missing an application are touched, so a second call cannot re-file
    events that already belong somewhere.
    """
    if not visit_id or not _events_table_ok:
        return
    try:
        _events_sb.table("application_events").update(
            {"application_id": sid}
        ).eq("visit_id", visit_id).is_("application_id", "null").execute()
    except Exception:
        log.exception("Failed to attach visit %s to application %s", visit_id, sid)


def fetch_application_events(sid: int, limit: int = 500) -> list:
    """Timeline for one application, oldest first. Empty on any failure --
    a dashboard that can't read the trail should still render the lead."""
    if not _events_table_ok:
        return []
    try:
        res = _events_sb.table("application_events").select(
            "id, created_at, event_type, actor, rep_code, brand_slug, ip, "
            "user_agent, payload"
        ).eq("application_id", sid).order("id", desc=False).limit(limit).execute()
        return res.data or []
    except Exception as exc:
        log.warning("Could not load activity trace for %s: %s", sid, exc)
        return []


# -------------------- Admin Auth --------------------
ADMIN_EMAIL = (os.environ.get("ADMIN_EMAIL") or "").strip().lower()
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", "")

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_authed"):
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped

# -------------------- Validation --------------------
SSN_RE = re.compile(r'^(?!000|666|9\d\d)(\d{3})-(?!00)(\d{2})-(?!0000)(\d{4})$')
EIN_RE = re.compile(r'^(?!00)\d{2}-\d{7}$')
PHONE_RE = re.compile(r'^\+?1?\s*\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}$')
ZIP_RE = re.compile(r'^\d{5}(-\d{4})?$')
STATE_RE = re.compile(r'^[A-Za-z]{2}$')
FICO_RE = re.compile(r'^\d{3}$')

def _is_valid_fico(value: str) -> bool:
    """
    Accept blank or 300-850.
    """
    if value is None:
        return True
    v = value.strip()
    if v == "":
        return True
    if not FICO_RE.match(v):
        return False
    try:
        n = int(v)
    except ValueError:
        return False
    return 300 <= n <= 850

LOGO_PATH = APP_DIR / "static" / "pathway-logo.png"

# Brand colours
BRAND_BLUE = colors.HexColor('#1e40af')
BRAND_LIGHT_BLUE = colors.HexColor('#3b82f6')
BRAND_BG = colors.HexColor('#f0f7ff')
BRAND_BORDER = colors.HexColor('#bfdbfe')
BRAND_DARK = colors.HexColor('#1e293b')
BRAND_GRAY = colors.HexColor('#64748b')

def _pdf_header_footer(canvas, doc, submission_id):
    """Draw logo header, divider lines, and 'Powered by CROC' footer on every page."""
    canvas.saveState()
    w, h = letter

    # ── Header: logo + title ──
    if LOGO_PATH.exists():
        canvas.drawImage(str(LOGO_PATH), 0.6*inch, h - 1.05*inch, width=0.75*inch, height=0.75*inch, preserveAspectRatio=True, mask='auto')
    canvas.setFont("Helvetica-Bold", 16)
    canvas.setFillColor(BRAND_BLUE)
    canvas.drawString(1.5*inch, h - 0.65*inch, "Pathway Catalyst")
    canvas.setFont("Helvetica", 10)
    canvas.setFillColor(BRAND_GRAY)
    canvas.drawString(1.5*inch, h - 0.85*inch, "Business Financing Application")

    # Header divider line
    canvas.setStrokeColor(BRAND_LIGHT_BLUE)
    canvas.setLineWidth(2)
    canvas.line(0.5*inch, h - 1.15*inch, w - 0.5*inch, h - 1.15*inch)

    # ── Footer ──
    canvas.setStrokeColor(BRAND_BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(0.5*inch, 0.55*inch, w - 0.5*inch, 0.55*inch)

    # Left: Powered by CROC
    canvas.setFont("Helvetica-Oblique", 8)
    canvas.setFillColor(BRAND_GRAY)
    canvas.drawString(0.6*inch, 0.35*inch, "Powered by CROC")

    # Center: page number
    canvas.setFont("Helvetica", 8)
    canvas.drawCentredString(w / 2, 0.35*inch, f"Page {doc.page}")

    # Right: application ID
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(w - 0.6*inch, 0.35*inch, f"Application ID: {submission_id}")

    canvas.restoreState()


def _styled_section_table(data, col_widths=None):
    """Create a consistently styled two-column data table."""
    if col_widths is None:
        col_widths = [2.2*inch, 4.3*inch]
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TEXTCOLOR', (0, 0), (0, -1), BRAND_DARK),
        ('TEXTCOLOR', (1, 0), (1, -1), colors.HexColor('#334155')),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('LINEBELOW', (0, 0), (-1, -2), 0.25, BRAND_BORDER),
        ('LINEBELOW', (0, -1), (-1, -1), 0.25, BRAND_BORDER),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BACKGROUND', (0, 0), (-1, -1), BRAND_BG),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('ROUNDEDCORNERS', [4, 4, 4, 4]),
    ]))
    return t


def _mask_email(email: str, business_name: str) -> str:
    """Mask email for PDF display: xxxx45@businessname.com"""
    if not email:
        return ""
    biz = re.sub(r'[^a-zA-Z0-9]', '', business_name).lower() if business_name else "business"
    return f"xxxx45@{biz}.com"


def _mask_mobile(mobile: str) -> str:
    """Mask mobile for PDF display."""
    if not mobile:
        return ""
    return "7654562345"



# ---- Submission record (the PDF's audit page) --------------------------------
# A signed document that cannot say when it was opened, from which link, or
# when it was signed is weak evidence. CrocSign appends an audit page to every
# stamped document for exactly this reason; this is the same page for the
# application PDF, built from the same trail the dashboard reads.
#
# Two rules, and both matter because this copy reaches the applicant:
#   * Applicant events only. Who on the team opened the file afterwards is
#     internal and belongs on the dashboard timeline, not on the customer's
#     document.
#   * No internal attribution notes. A rep code that failed to resolve is an
#     operations problem; on the applicant's copy the rep line is simply
#     omitted rather than annotated.
#
# The page is a snapshot: the emailed copy is generated seconds after submit,
# so it cannot know about documents uploaded later. It stamps the time it was
# built and says so, and an admin re-download shows everything since.
_PDF_RECORD_EVENTS = {
    "form_viewed":            "Application opened",
    "form_step_viewed":       "Progressed through the form",
    "application_submitted":  "Application signed and submitted",
    "documents_uploaded":     "Documents uploaded",
    "resume_link_opened":     "Credit-setup link opened",
    "credit_setup_viewed":    "Credit setup opened",
    "idiq_credentials_saved": "Credit credentials provided",
}

_UA_BROWSERS = (("Edg/", "Edge"), ("OPR/", "Opera"), ("Chrome/", "Chrome"),
                ("Firefox/", "Firefox"), ("Safari/", "Safari"))
_UA_SYSTEMS = (("Windows NT 10", "Windows"), ("Windows", "Windows"),
               ("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
               ("Mac OS X", "macOS"), ("Linux", "Linux"))


def _describe_user_agent(ua: str) -> str:
    """'Chrome on Windows' rather than 90 characters of version string."""
    if not ua:
        return ""
    browser = next((name for token, name in _UA_BROWSERS if token in ua), "")
    system = next((name for token, name in _UA_SYSTEMS if token in ua), "")
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or ""


def _fmt_record_time(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat((iso_str or "").replace("Z", "+00:00"))
        return dt.astimezone(EASTERN).strftime("%b %d, %Y at %I:%M:%S %p ET")
    except Exception:
        return iso_str or ""


def _submission_record_rows(sid: int) -> tuple[list, dict]:
    """(timeline rows, context) for the record page. Empty list disables it."""
    events = [e for e in fetch_application_events(sid)
              if e.get("actor") == "applicant"
              and e.get("event_type") in _PDF_RECORD_EVENTS]
    if not events:
        return [], {}

    ctx, rows, steps = {}, [], set()
    for e in events:
        etype = e["event_type"]
        payload = e.get("payload") or {}
        if etype == "form_viewed":
            ctx.setdefault("opened_at", e.get("created_at"))
            ctx.setdefault("ip", e.get("ip"))
            ctx.setdefault("user_agent", e.get("user_agent"))
            ctx.setdefault("brand", payload.get("client_name"))
            # Named only when the code resolved to a real rep -- printing a
            # code that no longer exists would assert something untrue.
            if e.get("rep_code") and payload.get("rep_resolved"):
                ctx.setdefault("rep_code", e["rep_code"])
        if etype == "form_step_viewed":
            step = payload.get("step")
            if isinstance(step, int):
                steps.add(step)
            continue          # folded into one line below, not one row per step
        detail = ""
        if etype == "documents_uploaded":
            n = payload.get("files") or 0
            detail = f"{n} file{'' if n == 1 else 's'}"
        rows.append((_fmt_record_time(e.get("created_at")),
                     _PDF_RECORD_EVENTS[etype], detail))
        if etype == "application_submitted":
            ctx["submitted_at"] = e.get("created_at")

    if steps:
        ctx["furthest_step"] = max(steps)
    return rows, ctx


def generate_application_pdf(form_data: dict, submission_id: int, rep_name: str = None) -> BytesIO:
    """Generate a professionally styled PDF summary of the application."""
    if not PDF_ENABLED:
        return None

    buffer = BytesIO()
    w, h = letter

    # Custom page template with header/footer
    frame = Frame(0.6*inch, 0.75*inch, w - 1.2*inch, h - 2.0*inch, id='main')
    template = PageTemplate(
        id='branded',
        frames=[frame],
        onPage=lambda canvas, doc: _pdf_header_footer(canvas, doc, submission_id)
    )
    doc = BaseDocTemplate(buffer, pagesize=letter, title=f"Application {submission_id}")
    doc.addPageTemplates([template])

    styles = getSampleStyleSheet()

    # Custom styles
    section_style = ParagraphStyle(
        'SectionHead', parent=styles['Heading2'],
        fontSize=13, spaceBefore=18, spaceAfter=8,
        textColor=BRAND_BLUE, borderPadding=(0, 0, 4, 0),
    )
    meta_style = ParagraphStyle(
        'Meta', parent=styles['Normal'],
        fontSize=10, textColor=BRAND_GRAY, spaceAfter=2,
    )
    consent_style = ParagraphStyle(
        'Consent', parent=styles['Normal'],
        fontSize=9, textColor=BRAND_GRAY, alignment=TA_CENTER, spaceBefore=20,
    )

    elements = []

    # ── Submission meta info ──
    elements.append(Paragraph(f"<b>Application ID:</b> {submission_id}", meta_style))
    elements.append(Paragraph(f"<b>Submitted:</b> {datetime.now(EASTERN).strftime('%B %d, %Y at %I:%M %p ET')}", meta_style))
    if rep_name:
        elements.append(Paragraph(f"<b>Sales Representative:</b> {rep_name}", meta_style))
    elements.append(Spacer(1, 10))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=BRAND_BORDER, spaceAfter=6))

    # ── Business Information ──
    elements.append(Paragraph("Business Information", section_style))
    loan_amt = form_data.get('loan_amount', '')
    try:
        loan_display = f"${float(loan_amt):,.0f}" if loan_amt else ""
    except (ValueError, TypeError):
        loan_display = str(loan_amt)

    biz_data = [
        ["Business Legal Name", form_data.get("business_legal_name", "")],
        ["DBA Name", form_data.get("business_dba", "")],
        ["Industry", form_data.get("industry", "")],
        ["Legal Entity", form_data.get("legal_entity", "")],
        ["Business Start Date", form_data.get("business_start_date", "")],
        ["EIN", form_data.get("ein", "")],
        ["Website", form_data.get("company_website", "")],
        ["Phone", _mask_mobile(form_data.get("business_phone", ""))],
        ["Requested Loan Amount", loan_display],
        ["Loan Purpose", form_data.get("loan_purpose", "")],
    ]
    elements.append(_styled_section_table(biz_data))

    # ── Company Address ──
    elements.append(Paragraph("Company Address", section_style))
    addr = f"{form_data.get('company_address1', '')} {form_data.get('company_address2', '')}".strip()
    city_state = f"{form_data.get('company_city', '')}, {form_data.get('company_state', '')} {form_data.get('company_zip', '')}"
    country = form_data.get('company_country', 'United States')
    addr_data = [
        ["Street", addr],
        ["City / State / ZIP", city_state],
        ["Country", country],
    ]
    elements.append(_styled_section_table(addr_data))

    # ── Primary Owner ──
    elements.append(Paragraph("Primary Owner", section_style))
    owner_data = [
        ["Name", f"{form_data.get('owner_0_first', '')} {form_data.get('owner_0_last', '')}"],
        ["Ownership %", f"{form_data.get('owner_0_pct', '')}%"],
        ["Date of Birth", form_data.get("owner_0_dob", "")],
        ["SSN", form_data.get("owner_0_ssn", "")],
        ["Email", _mask_email(form_data.get("owner_0_email", ""), form_data.get("business_legal_name", ""))],
        ["Mobile", _mask_mobile(form_data.get("owner_0_mobile", ""))],
        ["FICO Score", form_data.get("owner_0_fico", "N/A")],
        ["MCA Balances", form_data.get("owner_0_mca_balances", "N/A")],
    ]
    elements.append(_styled_section_table(owner_data))

    # Owner home address
    owner_addr = f"{form_data.get('owner_0_addr1', '')} {form_data.get('owner_0_addr2', '')}".strip()
    owner_city_state = f"{form_data.get('owner_0_city', '')}, {form_data.get('owner_0_state', '')} {form_data.get('owner_0_zip', '')}"
    elements.append(Paragraph("Owner Home Address", section_style))
    elements.append(_styled_section_table([
        ["Street", owner_addr],
        ["City / State / ZIP", owner_city_state],
    ]))

    # ── Second Owner (if present) ──
    if (form_data.get("has_owner_1") or "No").strip() == "Yes":
        elements.append(Paragraph("Second Owner", section_style))
        owner2_data = [
            ["Name", f"{form_data.get('owner_1_first', '')} {form_data.get('owner_1_last', '')}"],
            ["Ownership %", f"{form_data.get('owner_1_pct', '')}%"],
            ["Date of Birth", form_data.get("owner_1_dob", "")],
            ["SSN", form_data.get("owner_1_ssn", "")],
            ["Email", _mask_email(form_data.get("owner_1_email", ""), form_data.get("business_legal_name", ""))],
            ["Mobile", _mask_mobile(form_data.get("owner_1_mobile", ""))],
            ["FICO Score", form_data.get("owner_1_fico", "N/A")],
            ["MCA Balances", form_data.get("owner_1_mca_balances", "N/A")],
        ]
        elements.append(_styled_section_table(owner2_data))

        # Second owner home address
        owner1_addr = f"{form_data.get('owner_1_addr1', '')} {form_data.get('owner_1_addr2', '')}".strip()
        owner1_city_state = f"{form_data.get('owner_1_city', '')}, {form_data.get('owner_1_state', '')} {form_data.get('owner_1_zip', '')}"
        elements.append(Paragraph("Second Owner Home Address", section_style))
        elements.append(_styled_section_table([
            ["Street", owner1_addr],
            ["City / State / ZIP", owner1_city_state],
        ]))

    # ── Property Information ──
    elements.append(Paragraph("Property &amp; Location", section_style))
    prop_data = [
        ["Owns Real Estate", form_data.get("own_real_estate", "")],
        ["Own Home Location", form_data.get("own_home_location", "")],
        ["Own Business Location", form_data.get("own_business_location", "")],
    ]
    elements.append(_styled_section_table(prop_data))

    # ── IDIQ Account ── (username only — password stays encrypted in DB)
    idiq_username = form_data.get("idiq_username", "")
    if idiq_username:
        elements.append(Paragraph("IDIQ Account", section_style))
        elements.append(_styled_section_table([
            ["IDIQ Username", idiq_username],
            ["IDIQ Password", "Stored encrypted — retrieve via admin dashboard"],
        ]))

    # ── Signature & Authorization ──
    elements.append(Paragraph("Authorization &amp; Signature", section_style))
    auth_style = ParagraphStyle(
        'AuthText', parent=styles['Normal'], fontSize=9, textColor=BRAND_GRAY,
        spaceAfter=8, alignment=TA_JUSTIFY, leading=12,
    )
    elements.append(Paragraph(
        "By submitting this application, the applicant authorizes the lender and its partners to contact the "
        "applicant at the telephone, cell phone, email, or direct mail contact data provided in this form for "
        "purposes of fulfilling this inquiry about business financing, even if the applicant has previously "
        "indicated a preference of \"do not call\" or \"do not email\" with a government registry. The applicant "
        "also authorizes the lender and its representatives, successors, assigns, and designees to obtain consumer "
        "and/or personal, business and investigative reports and other information about the applicant from "
        "consumer reporting agencies and other third parties. The applicant consents to the release of any "
        "information relating to the applicant to the lender on its behalf. By providing a cell phone number, "
        "the applicant consents to the receipt of text messages knowing that message and data rates may apply. "
        "Reply STOP to unsubscribe, HELP for help. Message frequency varies. The applicant certifies that all "
        "the information contained herein is complete, true, and accurate.",
        auth_style
    ))
    elements.append(Paragraph(
        "<b>E-SIGN Act / UETA Consent:</b> The applicant agrees that the electronic digitized signature applied "
        "on this document is a representation of the applicant's signature and is legally valid and binding as "
        "if the applicant had signed the document with ink on paper in accordance with the Uniform Electronic "
        "Transactions Act (UETA) and the Electronic Signatures in Global and National Commerce Act (E-SIGN) of 2000.",
        auth_style
    ))
    elements.append(Spacer(1, 6))

    sig_info = [
        ["Print Name", form_data.get("signature_print_name", "")],
        ["Date Signed", form_data.get("signature_date", "")],
    ]
    elements.append(_styled_section_table(sig_info))

    # Render hand signature image
    sig_data = form_data.get("signature_data", "")
    if sig_data and sig_data.startswith("data:image/png;base64,"):
        raw = base64.b64decode(sig_data.split(",", 1)[1])
        sig_buf = BytesIO(raw)
        sig_img = Image(sig_buf, width=3.2*inch, height=1.2*inch)
        sig_img.hAlign = 'LEFT'
        elements.append(Spacer(1, 8))
        elements.append(sig_img)
        elements.append(HRFlowable(width="50%", thickness=0.5, color=BRAND_DARK, spaceAfter=4))
        elements.append(Paragraph("Applicant Signature", ParagraphStyle(
            'SigLabel', parent=styles['Normal'], fontSize=9, textColor=BRAND_GRAY
        )))

    # Second-owner signature block (only if a second owner was added)
    if (form_data.get("has_owner_1") or "No").strip() == "Yes":
        elements.append(Spacer(1, 14))
        owner1_sig_info = [
            ["Print Name", form_data.get("owner_1_signature_print_name", "")],
            ["Date Signed", form_data.get("owner_1_signature_date", "")],
        ]
        elements.append(_styled_section_table(owner1_sig_info))

        owner1_sig_data = form_data.get("owner_1_signature_data", "")
        if owner1_sig_data and owner1_sig_data.startswith("data:image/png;base64,"):
            raw1 = base64.b64decode(owner1_sig_data.split(",", 1)[1])
            sig_buf1 = BytesIO(raw1)
            sig_img1 = Image(sig_buf1, width=3.2*inch, height=1.2*inch)
            sig_img1.hAlign = 'LEFT'
            elements.append(Spacer(1, 8))
            elements.append(sig_img1)
            elements.append(HRFlowable(width="50%", thickness=0.5, color=BRAND_DARK, spaceAfter=4))
            elements.append(Paragraph("Second Owner Signature", ParagraphStyle(
                'SigLabel2', parent=styles['Normal'], fontSize=9, textColor=BRAND_GRAY
            )))

    # ── Submission record ──
    # Never allowed to cost the document: any failure here drops the page and
    # the application PDF is emailed exactly as before.
    try:
        record_rows, ctx = _submission_record_rows(submission_id)
    except Exception as exc:
        log.warning("Submission record omitted from PDF %s: %s", submission_id, exc)
        record_rows, ctx = [], {}

    if record_rows:
        elements.append(PageBreak())
        elements.append(Paragraph("Submission Record", section_style))
        elements.append(Paragraph(
            "A record of this application's own session, taken from the "
            "server's activity log.", meta_style))
        elements.append(Spacer(1, 10))

        summary = []
        if ctx.get("opened_at"):
            summary.append(["Opened", _fmt_record_time(ctx["opened_at"])])
        entry = " · ".join(x for x in (
            ctx.get("brand"),
            f"rep link: {ctx['rep_code']}" if ctx.get("rep_code") else "",
        ) if x)
        if entry:
            summary.append(["Entry point", entry])
        if ctx.get("furthest_step"):
            summary.append(["Progress", f"reached step {ctx['furthest_step']} of 5"])
        if ctx.get("submitted_at"):
            summary.append(["Signed and submitted", _fmt_record_time(ctx["submitted_at"])])
        device = " · ".join(x for x in (ctx.get("ip"),
                                        _describe_user_agent(ctx.get("user_agent") or "")) if x)
        if device:
            summary.append(["Device", device])
        if summary:
            elements.append(_styled_section_table(summary))
            elements.append(Spacer(1, 14))

        elements.append(Paragraph("Timeline", ParagraphStyle(
            'RecordSub', parent=styles['Normal'], fontSize=10,
            textColor=BRAND_DARK, spaceAfter=6,
        )))
        elements.append(_styled_section_table(
            [[when, f"{what}{(' — ' + detail) if detail else ''}"]
             for when, what, detail in record_rows],
            col_widths=[2.3*inch, 4.0*inch],
        ))
        elements.append(Paragraph(
            "This record reflects activity known at "
            f"{datetime.now(EASTERN).strftime('%B %d, %Y at %I:%M %p ET')}. "
            "Steps taken after this document was generated are not shown.",
            consent_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer


def _build_email_content(business_name, submission_id, rep_name, attached_files,
                         email_type="new_application", resume_url=None, pdf_url=None,
                         lead_details=None):
    """Build shared email HTML, plain text, and subject.

    `resume_url` is only rendered for applicant_receipt emails — it links the
    merchant back to the credit-setup page without re-filling the application.
    `pdf_url`: when set, the PDF was too large to attach and is instead provided
    as a signed download link embedded in the email body.
    `lead_details`: (label, value) pairs rendered as extra rows — the
    rep_lead_summary email carries the lead facts inline instead of a PDF.
    """
    rep_line = f"Referred by: {rep_name}" if rep_name else "Direct submission (no rep)"
    doc_count = len(attached_files) if attached_files else 0
    # Explicit Eastern (see EASTERN at top) so the stamp is correct regardless
    # of the process/system clock; labelled ET so it can't read as local time.
    submitted = datetime.now(EASTERN).strftime('%B %d, %Y at %I:%M %p ET')
    base_subject = f"New Application: {business_name} (ID: {submission_id})"
    is_applicant_copy = (email_type == "applicant_receipt")
    if email_type == "docs_update":
        subject = f"Re: {base_subject}"
        alert_text = "Additional Documents Uploaded"
        attachments_text = f"{doc_count} supporting document(s)"
        body_note = (
            "The applicant has uploaded additional supporting documents for this application. "
            "Please find them attached to this email."
        )
    elif email_type == "rep_lead_summary":
        subject = f"New Lead: {business_name} (ID: {submission_id})"
        alert_text = "New Lead Submitted"
        attachments_text = ""
        body_note = (
            "A new application just came in on your link. The key lead details are "
            "listed above. The full application and any supporting documents go "
            "to the processing team."
        )
    elif is_applicant_copy:
        # Customer-facing receipt — friendlier copy, no internal rep details.
        subject = f"Application Received — {business_name}"
        alert_text = "Thanks for your application"
        attachments_text = "Application PDF"
        body_note = (
            "We've received your business financing application. Our team will review it "
            "and reach out within 24-48 hours if any additional information is needed. "
            "A copy of your application is attached for your records."
        )
    else:
        subject = base_subject
        alert_text = "New Loan Application Received"
        attachments_text = "Application PDF"
        body_note = (
            "Please find the complete application summary attached to this email. "
            "You can also view full details in the admin dashboard."
        )

    # When the PDF is too large to attach, swap in a download-link note.
    if pdf_url:
        attachments_text = "Application PDF (download link below)"
        pdf_link_html = (
            f'<p style="margin:0 0 20px;">'
            f'<a href="{pdf_url}" style="display:inline-block;background:#1e40af;color:#ffffff;'
            f'font-size:14px;font-weight:600;padding:10px 20px;border-radius:6px;'
            f'text-decoration:none;">&#8681;&nbsp;Download Application PDF</a>'
            f'<br><span style="font-size:11px;color:#94a3b8;">Link expires in 1 hour.</span>'
            f'</p>'
        )
        pdf_link_plain = f"\nDownload Application PDF: {pdf_url}\n(Link expires in 1 hour)\n"
    else:
        pdf_link_html = ""
        pdf_link_plain = ""

    # The Representative row is internal-only — omit from the applicant's copy.
    rep_row_html = "" if is_applicant_copy else f"""<tr>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#64748b;font-size:13px;">Representative</td>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#1e293b;font-size:14px;">{rep_line}</td>
              </tr>"""

    # Lead facts (rep_lead_summary only). That email carries its own Business
    # Name row, so the generic header row is dropped to avoid repeating it, as
    # is the Attachments row — it has no attachment to name.
    business_row_html = "" if lead_details else f"""<tr>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#64748b;font-size:13px;width:140px;">Business</td>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#1e293b;font-size:14px;font-weight:600;">{business_name}</td>
              </tr>"""
    lead_rows_html = "".join(
        f"""<tr>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#64748b;font-size:13px;">{escape(label)}</td>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#1e293b;font-size:14px;">{escape(value)}</td>
              </tr>"""
        for label, value in (lead_details or [])
    )
    attachments_row_html = "" if not attachments_text else f"""<tr>
                <td style="padding:8px 0;color:#64748b;font-size:13px;">Attachments</td>
                <td style="padding:8px 0;color:#1e293b;font-size:14px;">{attachments_text}</td>
              </tr>"""

    # Resume-link CTA for the merchant. Only the applicant receipt gets it.
    resume_cta_html = ""
    if is_applicant_copy and resume_url:
        resume_cta_html = f"""
            <table width="100%" cellpadding="0" cellspacing="0" style="margin:24px 0;">
              <tr>
                <td style="background:rgba(96,165,250,0.08);border:1px solid #bfdbfe;border-radius:10px;padding:18px 22px;">
                  <p style="margin:0 0 10px;color:#1e40af;font-size:15px;font-weight:600;">Want to finish your credit setup later?</p>
                  <p style="margin:0 0 14px;color:#475569;font-size:13px;line-height:1.55;">
                    We use a soft credit pull through IDIQ (no impact to your score) to speed up review.
                    If you skipped it earlier, use the secure link below to come back any time in the next 30 days.
                  </p>
                  <p style="margin:0;">
                    <a href="{resume_url}"
                       style="display:inline-block;background:linear-gradient(135deg,#2563eb,#3b82f6);color:#ffffff;text-decoration:none;padding:11px 22px;border-radius:8px;font-weight:600;font-size:14px;">
                       Complete Credit Setup
                    </a>
                  </p>
                </td>
              </tr>
            </table>
        """

    html_body = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:24px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.06);">

        <!-- Header -->
        <tr>
          <td style="background:linear-gradient(135deg,#1e40af 0%,#3b82f6 100%);padding:28px 32px;text-align:center;">
            <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;">Pathway Catalyst</h1>
            <p style="margin:6px 0 0;color:#bfdbfe;font-size:13px;">Business Financing Application</p>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="padding:28px 32px;">

            <!-- Alert badge -->
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f7ff;border:1px solid #bfdbfe;border-radius:8px;margin-bottom:24px;">
              <tr>
                <td style="padding:14px 18px;">
                  <p style="margin:0;font-size:15px;font-weight:600;color:#1e40af;">{alert_text}</p>
                </td>
              </tr>
            </table>

            <!-- Details table -->
            <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px;">
              {business_row_html}
              <tr>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#64748b;font-size:13px;">Application ID</td>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#1e293b;font-size:14px;font-weight:600;">{submission_id}</td>
              </tr>
              <tr>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#64748b;font-size:13px;">Submitted</td>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#1e293b;font-size:14px;">{submitted}</td>
              </tr>
              {lead_rows_html}
              {rep_row_html}
              {attachments_row_html}
            </table>

            <p style="color:#475569;font-size:14px;line-height:1.6;margin:0 0 20px;">
              {body_note}
            </p>

            {resume_cta_html}

            {pdf_link_html}

          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:#f8fafc;padding:18px 32px;border-top:1px solid #e2e8f0;text-align:center;">
            <p style="margin:0 0 4px;color:#64748b;font-size:12px;">Pathway Catalyst &mdash; See the Pathway. Be the Catalyst.</p>
            <p style="margin:0;color:#94a3b8;font-size:11px;font-style:italic;">Powered by CROC</p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>
    """

    plain_rep_line = "" if is_applicant_copy else f"{rep_line}\n"
    plain_resume_line = ""
    if is_applicant_copy and resume_url:
        plain_resume_line = (
            "\nWant to finish your credit setup later? Use this secure link "
            "(valid 30 days):\n" + resume_url + "\n"
        )
    plain_lead_lines = "".join(
        f"{label}: {value}\n" for label, value in (lead_details or [])
    )
    plain_attachments_line = f"\nAttachments: {attachments_text}\n" if attachments_text else ""
    plain_business_line = "" if lead_details else f"Business: {business_name}\n"
    plain_text = (
        f"{alert_text}\n\n{plain_business_line}"
        f"Application ID: {submission_id}\nSubmitted: {submitted}\n"
        f"{plain_lead_lines}"
        f"{plain_rep_line}{plain_attachments_line}"
        f"{plain_resume_line}"
        f"{pdf_link_plain}\n"
        "Powered by CROC"
    )

    return subject, html_body, plain_text


def _send_via_resend(to_emails, subject, html_body, plain_text, pdf_buffer, submission_id, attached_files,
                     message_id=None, in_reply_to=None, pdf_url=None):
    """Send email using Resend REST API (works on Railway where SMTP is blocked).

    pdf_buffer: BytesIO to attach directly (small PDFs < 25 MB).
    pdf_url: signed download URL already embedded in html_body/plain_text (large PDFs).
    """
    log.info("Sending via Resend API to %s", ', '.join(to_emails))

    attachments = []
    if pdf_buffer:
        pdf_buffer.seek(0)
        attachments.append({
            "filename": f"application_{submission_id}.pdf",
            "content": base64.b64encode(pdf_buffer.read()).decode(),
        })
    if attached_files:
        for bp in attached_files:
            try:
                file_bytes = _download_from_storage(bp)
                attachments.append({
                    "filename": bp.split("/")[-1],
                    "content": base64.b64encode(file_bytes).decode(),
                })
            except Exception as e:
                log.error("Failed to download attachment %s: %s", bp, e)

    body = {
        "from": EMAIL_FROM,
        "to": to_emails,
        "subject": subject,
        "html": html_body,
        "text": plain_text,
        "attachments": attachments,
    }
    headers_extra = {}
    if message_id:
        headers_extra["Message-ID"] = message_id
    if in_reply_to:
        headers_extra["In-Reply-To"] = in_reply_to
        headers_extra["References"] = in_reply_to
    if headers_extra:
        body["headers"] = headers_extra
    payload = json.dumps(body).encode()

    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
        log.info("Resend API success: %s", result)
    return True


def _send_via_smtp(to_emails, subject, html_body, plain_text, pdf_buffer, submission_id, attached_files,
                   message_id=None, in_reply_to=None, pdf_url=None):
    """Send email using SMTP (works locally, blocked on some cloud hosts).

    pdf_buffer: BytesIO to attach directly (small PDFs < 25 MB).
    pdf_url: signed download URL already embedded in html_body/plain_text (large PDFs).
    """
    log.info("Sending via SMTP to %s (%s:%s)", ', '.join(to_emails), SMTP_HOST, SMTP_PORT)

    msg = MIMEMultipart('mixed')
    msg['From'] = EMAIL_FROM
    msg['To'] = ', '.join(to_emails)
    msg['Subject'] = subject
    if message_id:
        msg['Message-ID'] = message_id
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = in_reply_to

    alt_part = MIMEMultipart('alternative')
    alt_part.attach(MIMEText(plain_text, 'plain'))
    alt_part.attach(MIMEText(html_body, 'html'))
    msg.attach(alt_part)

    if pdf_buffer:
        pdf_buffer.seek(0)
        pdf_attachment = MIMEApplication(pdf_buffer.read(), _subtype='pdf')
        pdf_attachment.add_header('Content-Disposition', 'attachment',
                                  filename=f'application_{submission_id}.pdf')
        msg.attach(pdf_attachment)

    if attached_files:
        for bp in attached_files:
            try:
                file_bytes = _download_from_storage(bp)
                file_attachment = MIMEApplication(file_bytes, _subtype='pdf')
                file_attachment.add_header('Content-Disposition', 'attachment',
                                          filename=bp.split("/")[-1])
                msg.attach(file_attachment)
            except Exception as e:
                log.error("Failed to download attachment %s for SMTP: %s", bp, e)

    # Port 465 uses implicit SSL; port 587 uses STARTTLS
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)

    log.info("SMTP email sent successfully to %s", ', '.join(to_emails))
    return True


def _send_via_supabase_fn(to_emails, subject, html_body, plain_text, pdf_buffer, submission_id, attached_files,
                          message_id=None, in_reply_to=None, pdf_url=None):
    """Send email via Supabase Edge Function (HTTP relay to bypass Railway SMTP block).

    pdf_buffer: BytesIO to attach directly (small PDFs < 25 MB).
    pdf_url: signed download URL already embedded in html_body/plain_text (large PDFs).
    """
    fn_url = f"{SUPABASE_URL}/functions/v1/send-email"
    log.info("Sending via Supabase Edge Function to %s (%s)", ', '.join(to_emails), fn_url)

    attachments = []
    if pdf_buffer:
        pdf_buffer.seek(0)
        attachments.append({
            "filename": f"application_{submission_id}.pdf",
            "content": base64.b64encode(pdf_buffer.read()).decode(),
        })
    if attached_files:
        for bp in attached_files:
            try:
                file_bytes = _download_from_storage(bp)
                attachments.append({
                    "filename": bp.split("/")[-1],
                    "content": base64.b64encode(file_bytes).decode(),
                })
            except Exception as e:
                log.error("Failed to download attachment %s: %s", bp, e)

    body = {
        "from": EMAIL_FROM,
        "to": to_emails,
        "subject": subject,
        "html": html_body,
        "text": plain_text,
        "attachments": attachments,
    }
    if message_id:
        body["messageId"] = message_id
    if in_reply_to:
        body["inReplyTo"] = in_reply_to
        body["references"] = in_reply_to
    payload = json.dumps(body).encode()

    req = urllib.request.Request(
        fn_url,
        data=payload,
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
        if result.get("success"):
            log.info("Supabase Edge Function email sent: %s", result)
        else:
            raise RuntimeError(f"Edge Function error: {result.get('error', 'unknown')}")
    return True


def _application_message_id(submission_id: int) -> str:
    """Deterministic Message-ID used to thread all emails for an application."""
    return f"<application-{submission_id}@pathwaycatalyst.app>"


def _mark_email_sent(submission_id: int, column: str):
    """Stamp initial_email_sent_at or docs_email_sent_at after a successful send."""
    try:
        sb.table("applications").update(
            {column: datetime.utcnow().isoformat()}
        ).eq("id", submission_id).execute()
    except Exception as exc:
        log.error("Failed to mark %s for %s: %s", column, submission_id, exc)


PDF_SIZE_LIMIT = 25 * 1024 * 1024  # 25 MB — Gmail's attachment limit


def send_email_with_pdf(
    to_emails: List[str],
    business_name: str,
    pdf_buffer: BytesIO,
    submission_id: int,
    rep_name: str = None,
    attached_files: List[str] = None,
    email_type: str = "new_application",
    resume_url: str = None,
    lead_details: List[tuple] = None,
):
    """Send email with PDF + attachments. Priority: Resend → Supabase Edge Fn → SMTP.

    PDFs smaller than 25 MB are attached directly for convenience.
    PDFs >= 25 MB are uploaded to Supabase Storage and sent as a signed
    download link to avoid hitting Gmail's attachment size limit.
    """
    if not RESEND_API_KEY and not EMAIL_ENABLED:
        log.warning("EMAIL DISABLED – set RESEND_API_KEY or SMTP credentials. Would send to %s", ', '.join(to_emails))
        return False

    if not to_emails:
        log.warning("No recipients provided for email")
        return False

    # ── Decide: attach directly or upload and link ──────────────────────────
    pdf_url: Optional[str] = None
    send_buffer: Optional[BytesIO] = None

    if pdf_buffer is not None:
        pdf_buffer.seek(0)
        pdf_bytes = pdf_buffer.read()
        pdf_size = len(pdf_bytes)

        if pdf_size < PDF_SIZE_LIMIT:
            # Small PDF — attach directly (original behaviour).
            log.info(
                "PDF for submission %s is %d bytes (< 25 MB) — attaching directly",
                submission_id, pdf_size,
            )
            send_buffer = BytesIO(pdf_bytes)
        else:
            # Large PDF — upload to storage and embed a signed link.
            log.info(
                "PDF for submission %s is %d bytes (>= 25 MB) — uploading to storage",
                submission_id, pdf_size,
            )
            try:
                bucket_path = f"pdfs/{submission_id}/application_{submission_id}.pdf"
                _upload_to_storage(pdf_bytes, bucket_path)
                pdf_url = _get_signed_url(bucket_path)
                log.info("Signed URL generated for submission %s", submission_id)
            except Exception as exc:
                log.error(
                    "Storage upload failed for submission %s: %s — falling back to direct attach",
                    submission_id, exc,
                )
                # Graceful fallback: try to attach anyway even if it may be
                # rejected by the mail provider.
                send_buffer = BytesIO(pdf_bytes)
                pdf_url = None

    subject, html_body, plain_text = _build_email_content(
        business_name, submission_id, rep_name, attached_files,
        email_type=email_type, resume_url=resume_url, pdf_url=pdf_url,
        lead_details=lead_details,
    )

    thread_id = _application_message_id(submission_id)
    if email_type == "new_application":
        message_id, in_reply_to = thread_id, None
    else:
        message_id, in_reply_to = None, thread_id

    try:
        if RESEND_API_KEY:
            return _send_via_resend(
                to_emails, subject, html_body, plain_text,
                send_buffer, submission_id, attached_files,
                message_id=message_id, in_reply_to=in_reply_to,
                pdf_url=pdf_url,
            )
        # Supabase Edge Function relay (works on Railway where SMTP is blocked)
        return _send_via_supabase_fn(
            to_emails, subject, html_body, plain_text,
            send_buffer, submission_id, attached_files,
            message_id=message_id, in_reply_to=in_reply_to,
            pdf_url=pdf_url,
        )
    except Exception as e:
        log.error("Primary email method failed: %s – falling back to SMTP", e)

    # SMTP fallback (works locally / on hosts that allow outbound SMTP)
    try:
        return _send_via_smtp(
            to_emails, subject, html_body, plain_text,
            send_buffer, submission_id, attached_files,
            message_id=message_id, in_reply_to=in_reply_to,
            pdf_url=pdf_url,
        )
    except Exception as e:
        log.error("SMTP fallback also failed for %s: %s\n%s", ', '.join(to_emails), e, traceback.format_exc())
        return False


# ---- Rep lead-summary email ---------------------------------------------------

def _format_time_in_business(start_date: str) -> str:
    """Render business_start_date as elapsed time to today, e.g. "3 yrs 2 mos".

    The form supplies an <input type="date"> value (YYYY-MM-DD). Anything
    unparseable — or a future date — falls back to the raw value so the rep
    still sees what the merchant entered.
    """
    raw = (start_date or "").strip()
    if not raw:
        return ""
    try:
        started = datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return raw

    today = datetime.now(EASTERN).date()
    if started > today:
        return raw

    months = (today.year - started.year) * 12 + (today.month - started.month)
    if today.day < started.day:
        months -= 1
    months = max(months, 0)
    years, rem_months = divmod(months, 12)

    parts = []
    if years:
        parts.append(f"{years} yr" + ("s" if years != 1 else ""))
    if rem_months or not years:
        parts.append(f"{rem_months} mo" + ("s" if rem_months != 1 else ""))
    return " ".join(parts)


def _internal_recipients() -> List[str]:
    """The internal inboxes that receive the full application package.

    Reps are deliberately excluded. A rep only ever receives the lead summary
    (_build_lead_details), which carries no SSN, date of birth or EIN -- the
    application PDF and the applicant's uploaded documents stay internal.
    Blank env vars are skipped so an unconfigured inbox is simply absent.
    """
    out = []
    for addr in (TEAM_EMAIL, SUPPORT_EMAIL, PROCESSING_EMAIL):
        addr = (addr or "").strip()
        if addr and "@" in addr and addr not in out:
            out.append(addr)
    return out


def _build_lead_details(form: dict) -> List[tuple]:
    """The lead facts the rep email carries, as ordered (label, value) pairs."""
    loan_amt = form.get("loan_amount", "")
    try:
        loan_display = f"${float(loan_amt):,.0f}" if loan_amt else ""
    except (ValueError, TypeError):
        loan_display = str(loan_amt)

    return [
        ("Business Name", form.get("business_legal_name", "") or ""),
        ("First Name", form.get("owner_0_first", "") or ""),
        ("Last Name", form.get("owner_0_last", "") or ""),
        ("Phone", form.get("owner_0_mobile", "") or ""),
        ("Email", form.get("owner_0_email", "") or ""),
        ("Requested Amount", loan_display),
        ("Time in Business", _format_time_in_business(form.get("business_start_date", ""))),
        ("Industry", form.get("industry", "") or ""),
    ]


def _queue_lead_summary_email(form: dict, submission_id: int, rep_info: Optional[dict]):
    """Fire the plain lead-summary email to the rep and the internal inboxes.

    Sent in the background alongside the PDF notification and never allowed to
    fail the submission. Recipients with no configured address are skipped.
    """
    recipients = []
    for addr in ((rep_info or {}).get("email"), *_internal_recipients()):
        addr = (addr or "").strip()
        if addr and "@" in addr and addr not in recipients:
            recipients.append(addr)
    if not recipients:
        log.info("No lead-summary recipients for submission %s — skipping", submission_id)
        return

    business_name = form.get("business_legal_name") or ""
    rep_name = (rep_info or {}).get("name")
    lead_details = _build_lead_details(form)

    def _bg_send_lead(recips, biz, sid, rname, details):
        try:
            send_email_with_pdf(
                to_emails=recips, business_name=biz,
                pdf_buffer=None, submission_id=sid,
                rep_name=rname, attached_files=[],
                email_type="rep_lead_summary",
                lead_details=details,
            )
        except Exception as exc:
            log.error("Background lead summary email failed for %s: %s", sid, exc)

    threading.Thread(
        target=_bg_send_lead,
        args=(recipients, business_name, submission_id, rep_name, lead_details),
        daemon=True,
    ).start()
    log.info("Lead summary email queued for submission %s → %s", submission_id, recipients)


# ---- Business Lookup (SAM.gov) -----------------------------------------------

def classify_naics(business_name: str, industry: str, website: str = "") -> dict:
    """Classify the merchant into a NAICS/SIC code and industry bucket.

    Stamped onto the application at intake so the code is stored once and stays
    stable. Underwriting reads it downstream for lender restriction matching and
    for the SIC field on API submissions; classifying here means the merchant is
    bucketed the same way at intake and at match time.

    Rules live in the shared `pathway-naics` package, never in this repo -- if
    the two copies drift, the lender warnings disagree with the stored code.

    Enrichment only: like the SAM.gov lookup above, this must never block a
    submission. A missing package or a bad row yields {} and the application
    saves normally; underwriting falls back to classifying on its own side.
    """
    try:
        import pathway_naics
    except ImportError:
        log.info("pathway-naics not installed; skipping NAICS classification")
        return {}
    try:
        got = pathway_naics.classify(business_name=business_name, industry=industry,
                                     website=website)
    except Exception:
        log.exception("NAICS classification failed for '%s'", business_name)
        return {}
    if not (got.get("naics") or got.get("bucket")):
        return {}
    return {
        "naics": got.get("naics"),
        "naics_title": got.get("naics_title"),
        "sic": got.get("sic"),
        "sic_title": got.get("sic_title"),
        "bucket": got.get("bucket"),
        "confidence": got.get("confidence"),
        "method": got.get("method"),
        "classifier_version": getattr(pathway_naics, "__version__", None),
    }


def lookup_business_sam_gov(business_name: str, state_code: str, ein: str = "") -> dict:
    """
    Query SAM.gov Entity Management API for business registration data.
    Free tier: 10 requests/day for non-federal accounts.
    Never raises -- all exceptions caught and returned as error status.
    """
    result = {
        "lookup_source": "sam.gov",
        "lookup_timestamp": datetime.utcnow().isoformat() + "Z",
        "lookup_status": "error",
        "lookup_error": None,
    }

    if not SAM_GOV_API_KEY:
        result["lookup_status"] = "skipped"
        result["lookup_error"] = "No SAM.gov API key configured"
        return result

    if not business_name:
        result["lookup_status"] = "skipped"
        result["lookup_error"] = "Missing business name"
        return result

    try:
        params = {
            "api_key": SAM_GOV_API_KEY,
            "legalBusinessName": business_name.strip(),
            "registrationStatus": "A",
        }
        if state_code and len(state_code.strip()) == 2:
            params["physicalAddressStateCode"] = state_code.strip().upper()

        url = "https://api.sam.gov/entity-information/v3/entities?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        total = data.get("totalRecords", 0)
        entities = data.get("entityData", [])

        if not entities or total == 0:
            result["lookup_status"] = "not_found"
            result["sam_total_results"] = 0
            return result

        # Use the first (best) match
        entity = entities[0]
        reg = entity.get("entityRegistration", {})
        core = entity.get("coreData", {})
        phys_addr = core.get("physicalAddress", {})
        gen_info = core.get("generalInformation", {})
        biz_types = core.get("businessTypes", {})

        result["lookup_status"] = "found"
        result["sam_total_results"] = total
        result["sam_uei"] = reg.get("ueiSAM")
        result["sam_cage_code"] = reg.get("cageCode")
        result["sam_legal_name"] = reg.get("legalBusinessName")
        result["sam_dba_name"] = reg.get("dbaName")
        result["sam_registration_status"] = reg.get("registrationStatus")
        result["sam_expiration_date"] = reg.get("registrationExpirationDate")
        result["sam_entity_structure"] = gen_info.get("entityStructureDesc")
        result["sam_entity_type"] = gen_info.get("entityTypeDesc")
        result["sam_state_of_incorporation"] = gen_info.get("stateOfIncorporationCode")
        result["sam_country_of_incorporation"] = gen_info.get("countryOfIncorporationCode")
        result["sam_business_start_date"] = gen_info.get("companyEstablishedDate") or gen_info.get("fiscalYearEndCloseDate")
        result["sam_organization_structure"] = gen_info.get("organizationStructureDesc")
        result["sam_naics_codes"] = [
            n.get("naicsCode") for n in (gen_info.get("naicsList") or []) if n.get("naicsCode")
        ]
        result["sam_business_type_list"] = [
            bt.get("businessTypeDesc") for bt in (biz_types.get("businessTypeList") or []) if bt.get("businessTypeDesc")
        ]
        result["sam_physical_address"] = ", ".join(filter(None, [
            phys_addr.get("addressLine1"),
            phys_addr.get("addressLine2"),
            phys_addr.get("city"),
            phys_addr.get("stateOrProvinceCode"),
            phys_addr.get("zipCode"),
        ]))
        result["sam_sam_gov_url"] = f"https://sam.gov/entity/{reg.get('ueiSAM', '')}/coreData" if reg.get("ueiSAM") else None

        return result

    except urllib.error.HTTPError as e:
        result["lookup_error"] = f"HTTP {e.code}: {e.reason}"
        log.warning("SAM.gov lookup HTTP error for '%s': %s", business_name, result["lookup_error"])
    except urllib.error.URLError as e:
        result["lookup_error"] = f"URL error: {e.reason}"
        log.warning("SAM.gov lookup URL error for '%s': %s", business_name, result["lookup_error"])
    except Exception as e:
        result["lookup_error"] = str(e)[:200]
        log.warning("SAM.gov lookup failed for '%s': %s", business_name, e)

    return result


def validate_fields(form: dict) -> dict:
    errors = {}

    # Base required fields. IDIQ credentials are intentionally NOT here —
    # applicants can submit without creating an IDIQ account; the team will
    # follow up out-of-band if needed.
    req = [
        'business_legal_name','industry','legal_entity','business_start_date','ein',
        'company_address1','company_city','company_state','company_zip',
        'owner_0_first','owner_0_last','owner_0_pct','owner_0_dob','owner_0_ssn','owner_0_email','owner_0_mobile',
        'own_real_estate','own_home_location','own_business_location',
        'esign_consent','esign_act_consent',
        'signature_data','signature_date','signature_print_name',
    ]
    for k in req:
        if not form.get(k):
            errors[k] = 'Required'


    # Second owner conditional required fields
    has_owner_1 = (form.get('has_owner_1') or 'No').strip()
    if has_owner_1 == 'Yes':
        owner1_req = [
            'owner_1_first','owner_1_last','owner_1_pct','owner_1_dob','owner_1_ssn',
            'owner_1_email','owner_1_mobile',
            'owner_1_addr1','owner_1_city','owner_1_state','owner_1_zip',
            'owner_1_signature_data','owner_1_signature_date','owner_1_signature_print_name',
        ]
        for k in owner1_req:
            if not form.get(k):
                errors[k] = 'Required'

    # Pattern validations
    if form.get('ein') and not EIN_RE.match(form['ein']):
        errors['ein'] = 'Invalid EIN (##-#######)'

    if form.get('owner_0_ssn') and not SSN_RE.match(form['owner_0_ssn']):
        errors['owner_0_ssn'] = 'Invalid SSN (###-##-####)'

    if form.get('owner_0_mobile') and not PHONE_RE.match(form['owner_0_mobile']):
        errors['owner_0_mobile'] = 'Invalid phone number'

    if form.get('company_zip') and not ZIP_RE.match(form['company_zip']):
        errors['company_zip'] = 'Invalid ZIP'


    # Owner 0 optional FICO validation
    if not _is_valid_fico(form.get('owner_0_fico')):
        errors['owner_0_fico'] = 'FICO must be 300-850'

    # Owner 1 optional FICO validation (only if enabled)
    if has_owner_1 == 'Yes' and not _is_valid_fico(form.get('owner_1_fico')):
        errors['owner_1_fico'] = 'FICO must be 300-850'

    # Owner 1 extra validations if enabled
    if has_owner_1 == 'Yes':
        if form.get('owner_1_ssn') and not SSN_RE.match(form['owner_1_ssn']):
            errors['owner_1_ssn'] = 'Invalid SSN (###-##-####)'
        if form.get('owner_1_mobile') and not PHONE_RE.match(form['owner_1_mobile']):
            errors['owner_1_mobile'] = 'Invalid phone number'
        if form.get('owner_1_zip') and not ZIP_RE.match(form['owner_1_zip']):
            errors['owner_1_zip'] = 'Invalid ZIP'

    # E-sign consent must be explicitly "Yes"
    if form.get('esign_consent') and form.get('esign_consent') != 'Yes':
        errors['esign_consent'] = 'Consent is required'
    if form.get('esign_act_consent') and form.get('esign_act_consent') != 'Yes':
        errors['esign_act_consent'] = 'Consent is required'

    return errors

# -------------------- File upload helper --------------------
def _store_uploaded_file(sid: int, file_storage, dtype: str,
                         attached_paths: List[str], failed: List[str]) -> bool:
    """Upload one file to Supabase Storage + insert application_files row.
    Returns True on success. Failures are logged + appended to `failed` so
    the caller can continue processing the rest of the batch."""
    original = file_storage.filename or "file"
    safe = original.replace("/", "_").replace("\\", "_")
    unique = f"{uuid.uuid4().hex[:8]}_{safe}"
    bucket_path = f"{sid}/{dtype}/{unique}"
    file_bytes = file_storage.read()
    content_type = file_storage.content_type or "application/octet-stream"

    last_exc = None
    for attempt in range(3):
        try:
            size = _upload_to_storage(file_bytes, bucket_path, content_type)
            sb.table("application_files").insert({
                "application_id": sid,
                "filename": safe,
                "storage_path": bucket_path,
                "size_bytes": size,
                "doc_type": dtype,
            }).execute()
            attached_paths.append(bucket_path)
            return True
        except Exception as exc:
            last_exc = exc
            log.warning("Upload attempt %d failed for %s (%s): %s",
                        attempt + 1, original, dtype, exc)
            time.sleep(0.5)

    log.error("Upload failed after 3 attempts for %s (%s): %s", original, dtype, last_exc)
    failed.append(original)
    return False


def _process_uploads(sid: int, request_files) -> tuple[List[str], List[str], List[str]]:
    """Pull bank_files / voided_check / id_doc out of a Flask request.files and
    upload each. Returns (doc_types_saved, attached_storage_paths, failed_filenames)."""
    saved: List[str] = []
    attached_paths: List[str] = []
    failed: List[str] = []

    for f in request_files.getlist("bank_files"):
        if not f or not f.filename:
            continue
        if _store_uploaded_file(sid, f, "bank_statement", attached_paths, failed) \
                and "bank_statement" not in saved:
            saved.append("bank_statement")

    for field, dtype in [("voided_check", "voided_check"), ("id_doc", "id_doc")]:
        f = request_files.get(field)
        if f and f.filename:
            if _store_uploaded_file(sid, f, dtype, attached_paths, failed):
                saved.append(dtype)

    return saved, attached_paths, failed


# -------------------- Public Pages --------------------
def _render_form(client_name=None, brand_slug=None):
    rep_code = request.args.get("rep", "").strip()
    rep_info = get_rep_info(rep_code)
    rep_sig = sign_rep_code(rep_code) if rep_code else ""

    # Start of a visit: everything the applicant does from here until they
    # submit (or give up) files under this id. rep_resolved is recorded because
    # the page looks identical either way -- a dropped rep code is invisible to
    # the merchant and, without this, invisible to us until the lead lands as
    # Direct with no explanation.
    visit_id = start_visit()
    payload = {
        "client_name": client_name,
        "entry_path": request.path,
        "referrer": (request.referrer or "")[:300] or None,
    }
    if rep_code:
        # Only recorded when a code was actually on the link. Stamping
        # rep_resolved=false on a plain direct visit made every one of them
        # read as a dropped rep code on the timeline.
        payload["rep_resolved"] = bool(rep_info)
    log_event("form_viewed", visit_id=visit_id, rep_code=rep_code,
              brand_slug=brand_slug or "", payload=payload)

    return render_template(
        "form.html",
        rep_code=rep_code,
        rep_info=rep_info,
        rep_sig=rep_sig,
        idiq_signup_url=IDIQ_SIGNUP_URL,
        client_name=client_name,
    )

@app.route("/")
def home():
    # On a brand's own domain (application.croccrm.com) the root path is that
    # brand's entry point; on this app's own host it falls back to the default.
    brand = current_brand()
    return _render_form(client_name=(brand["name"] if brand else None),
                        brand_slug=(brand["slug"] if brand else None))

@app.route("/<client_slug>")
def home_client(client_slug):
    # Branded per-client entry point, e.g. /pathway-catalyst?rep=tom. Inactive
    # brands still render so links already in reps' hands don't break; unknown
    # slugs 404 so this doesn't shadow real assets or typo'd URLs.
    brand = get_brand_by_slug(client_slug, include_inactive=True)
    if not brand:
        abort(404)
    return _render_form(client_name=brand["name"], brand_slug=brand["slug"])

@app.route("/thank-you")
def thank_you():
    sid = request.args.get("sid", type=int)
    business = None
    idiq_already_saved = False
    if sid:
        res = sb.table("applications").select(
            "business_legal_name, idiq_username"
        ).eq("id", sid).limit(1).execute()
        rows = res.data or []
        if rows:
            business = rows[0].get("business_legal_name")
            idiq_already_saved = bool(rows[0].get("idiq_username"))
        log_event("thank_you_viewed", application_id=sid,
                  payload={"idiq_saved": idiq_already_saved,
                           "done": request.args.get("done") == "1"})
    return render_template(
        "thank_you.html",
        client_name=current_brand_name(),
        sid=sid,
        business=business,
        idiq_signup_url=IDIQ_SIGNUP_URL,
        idiq_already_saved=idiq_already_saved,
        idiq_just_saved=(request.args.get("idiq") == "saved"),
        is_done=(request.args.get("done") == "1"),
    )

# -------------------- Submission Endpoints --------------------
@app.route("/submit-application", methods=["POST"])
def submit_application():
    """AJAX endpoint: receives form fields (no files), validates, inserts into
    Supabase, fires PDF/email in background, returns JSON {success, submission_id}."""
    form = {k: (v.strip() if isinstance(v, str) else v) for k, v in request.form.items()}

    rep_code = form.get("rep_code", "").strip()
    rep_sig = form.get("rep_sig", "").strip()
    if rep_code and not verify_rep_code(rep_code, rep_sig):
        rep_code = ""
    rep_info = get_rep_info(rep_code)

    if "has_owner_1" not in form or not form.get("has_owner_1"):
        form["has_owner_1"] = "No"

    for ssn_key in ('owner_0_ssn', 'owner_1_ssn'):
        raw = form.get(ssn_key, '')
        digits = re.sub(r'\D', '', raw)
        if len(digits) == 9:
            form[ssn_key] = f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    raw_ein = form.get('ein', '')
    ein_digits = re.sub(r'\D', '', raw_ein)
    if len(ein_digits) == 9:
        form['ein'] = f"{ein_digits[:2]}-{ein_digits[2:]}"

    errors = validate_fields(form)
    if errors:
        # Field names only -- which questions people fail on is the useful
        # signal, and the answers themselves have no business in an audit row.
        log_event("submission_rejected", rep_code=rep_code,
                  payload={"fields": sorted(errors.keys())})
        return jsonify(success=False, error="Please fix validation errors and try again.", errors=errors), 400

    business_legal_name = form.get("business_legal_name") or ""
    industry = form.get("industry") or ""
    try:
        loan_amount = float(form.get("loan_amount") or 0)
    except Exception:
        loan_amount = 0.0

    owners: List[str] = []
    first0 = (form.get("owner_0_first") or "").strip()
    last0 = (form.get("owner_0_last") or "").strip()
    if first0 or last0:
        owners.append((first0 + " " + last0).strip())
    has_owner_1 = (form.get("has_owner_1") or "No").strip()
    if has_owner_1 == "Yes":
        first1 = (form.get("owner_1_first") or "").strip()
        last1 = (form.get("owner_1_last") or "").strip()
        if first1 or last1:
            owners.append((first1 + " " + last1).strip())

    business_lookup = lookup_business_sam_gov(
        business_name=business_legal_name,
        state_code=form.get("company_state", ""),
        ein=form.get("ein", ""),
    )
    form["business_lookup"] = business_lookup
    log.info("SAM.gov lookup for '%s' (state=%s): status=%s",
             business_legal_name, form.get("company_state", ""),
             business_lookup.get("lookup_status"))

    # Industry classification (enrichment - never blocks submission)
    naics_info = classify_naics(business_legal_name, industry,
                                form.get("company_website") or "")
    if naics_info:
        form["naics"] = naics_info
        log.info("NAICS for '%s': %s / SIC %s / %s (%s)",
                 business_legal_name, naics_info.get("naics"),
                 naics_info.get("sic"), naics_info.get("bucket"),
                 naics_info.get("method"))

    db_payload = {
        "business_legal_name": business_legal_name,
        "industry": industry,
        "loan_amount": loan_amount,
        "owners": owners,
        "payload": form,
        "ein": form.get("ein"),
        "business_phone": form.get("business_phone"),
        "company_website": form.get("company_website"),
        # Match keys promoted out of payload for underwriting queries; full
        # classification detail still lives in payload["naics"].
        "naics": naics_info.get("naics"),
        "sic": naics_info.get("sic"),
        "naics_bucket": naics_info.get("bucket"),
    }
    if rep_info:
        db_payload["rep_name"] = rep_info["name"]
        db_payload["rep_email"] = rep_info["email"]

    ins = sb.table("applications").insert(db_payload).execute()
    if not ins.data:
        return jsonify(success=False, error="Database insert failed"), 500
    submission_id = ins.data[0]["id"]

    # The visit now has an application to hang off: back-stamp the landing and
    # the wizard steps, then remember the id so the upload step and the
    # thank-you page can file against it without the browser supplying one.
    visit_id = current_visit_id()
    attach_visit_to_application(visit_id, submission_id)
    session[SESSION_VISIT_APP_KEY] = submission_id
    log_event("application_submitted", application_id=submission_id,
              visit_id=visit_id, rep_code=rep_code,
              payload={"loan_amount": loan_amount, "owners": len(owners),
                       "path": "wizard",
                       **({"rep_resolved": bool(rep_info)} if rep_code else {})})

    # Lead summary to the rep and the internal inboxes — independent of the
    # PDF pipeline, and the only application email a rep receives.
    _queue_lead_summary_email(form, submission_id, rep_info)

    if PDF_ENABLED:
        try:
            rep_name = rep_info["name"] if rep_info else None
            log.info("Generating PDF for submission %s (rep=%s)", submission_id, rep_name)
            pdf_buffer = generate_application_pdf(form, submission_id, rep_name)
            if pdf_buffer:
                pdf_buffer.seek(0)
                pdf_bytes = pdf_buffer.read()
                recipients = _internal_recipients()

                def _bg_send_team(recips, biz, sid, rname):
                    try:
                        ok = send_email_with_pdf(
                            to_emails=recips, business_name=biz,
                            pdf_buffer=BytesIO(pdf_bytes), submission_id=sid,
                            rep_name=rname, attached_files=[],
                        )
                        if ok:
                            _mark_email_sent(sid, "initial_email_sent_at")
                    except Exception as exc:
                        log.error("Background team email failed for %s: %s", sid, exc)

                threading.Thread(
                    target=_bg_send_team,
                    args=(recipients, business_legal_name, submission_id, rep_name),
                    daemon=True,
                ).start()
                log.info("Team email queued for submission %s → %s", submission_id, recipients)

                applicant_email = (form.get("owner_0_email") or "").strip()
                if applicant_email and "@" in applicant_email:
                    resume_token = sign_resume_token(submission_id)
                    resume_url = url_for("resume_application", token=resume_token, _external=True)

                    def _bg_send_applicant(to_email, biz, sid, link):
                        try:
                            send_email_with_pdf(
                                to_emails=[to_email], business_name=biz,
                                pdf_buffer=BytesIO(pdf_bytes), submission_id=sid,
                                rep_name=None, attached_files=[],
                                email_type="applicant_receipt",
                                resume_url=link,
                            )
                        except Exception as exc:
                            log.error("Background applicant email failed for %s: %s", sid, exc)

                    threading.Thread(
                        target=_bg_send_applicant,
                        args=(applicant_email, business_legal_name, submission_id, resume_url),
                        daemon=True,
                    ).start()
                    log.info("Applicant receipt queued for submission %s → %s", submission_id, applicant_email)
        except Exception as e:
            log.error("Failed to generate PDF for submission %s: %s\n%s", submission_id, e, traceback.format_exc())

    return jsonify(success=True, submission_id=submission_id)


@app.route("/upload-documents/<int:sid>", methods=["POST"])
def upload_documents(sid):
    """AJAX endpoint: receives file uploads for an existing submission."""
    _saved_types, saved_paths, _failed = _process_uploads(sid, request.files)
    if _failed:
        log.warning("Submission %s had upload failures: %s", sid, _failed)

    if _saved_types or _failed:
        log_event("documents_uploaded", application_id=sid,
                  payload={"types": _saved_types, "files": len(saved_paths),
                           "failed": len(_failed)})

    if saved_paths:
        try:
            app_res = sb.table("applications").select(
                "business_legal_name, rep_name"
            ).eq("id", sid).execute()
            row = (app_res.data or [{}])[0]
            business_name = row.get("business_legal_name") or ""
            rep_name = row.get("rep_name")

            recipients = _internal_recipients()

            def _bg_send_docs(recips, biz, sid_, rname, files):
                try:
                    ok = send_email_with_pdf(
                        to_emails=recips, business_name=biz,
                        pdf_buffer=None, submission_id=sid_,
                        rep_name=rname, attached_files=files,
                        email_type="docs_update",
                    )
                    if ok:
                        _mark_email_sent(sid_, "docs_email_sent_at")
                except Exception as exc:
                    log.error("Background docs email failed for %s: %s", sid_, exc)

            threading.Thread(
                target=_bg_send_docs,
                args=(recipients, business_name, sid, rep_name, saved_paths),
                daemon=True,
            ).start()
            log.info("Docs follow-up email queued for submission %s → %s", sid, recipients)
        except Exception as e:
            log.error("Failed to queue docs email for %s: %s", sid, e)

    return jsonify(success=True, saved=_saved_types, failed=_failed)


@app.route("/api/activity", methods=["POST"])
def api_activity():
    """Beacon from the wizard: which page the applicant reached, and whether
    they left without submitting.

    Necessarily unauthenticated -- it fires before an application exists -- so
    it is narrow on purpose. The visit is read off the signed session cookie
    and never off the request body, only allowlisted event types are accepted,
    and a visit gets a fixed budget of writes. It always answers 204: the
    browser has nothing to do with the result, and tracing must never surface
    as an error on a page someone is in the middle of filling in.
    """
    visit_id = current_visit_id()
    if not visit_id:
        return "", 204

    event_type = (request.form.get("event_type") or "").strip()
    if event_type not in PUBLIC_EVENT_TYPES:
        return "", 204

    with _visit_event_lock:
        # Bounded rather than expiring: visits are short, and the worst case of
        # the reset is that one long-lived visit gets a second budget.
        if len(_visit_event_counts) > 10_000:
            _visit_event_counts.clear()
        count = _visit_event_counts.get(visit_id, 0) + 1
        _visit_event_counts[visit_id] = count
    if count > _VISIT_EVENT_CAP:
        return "", 204

    payload = {}
    try:
        step = int(request.form.get("step") or 0)
    except ValueError:
        step = 0
    if 1 <= step <= 20:
        payload["step"] = step

    if event_type == "form_identity_captured":
        # Named fields with hard caps, never a passthrough of whatever the
        # browser sent: this endpoint is unauthenticated, and the difference
        # between the two is whether a stranger can write arbitrary content
        # into the trail.
        for field, cap in PUBLIC_IDENTITY_FIELDS.items():
            value = (request.form.get(field) or "").strip()
            if value:
                payload[field] = value[:cap]
        if not any(k in payload for k in PUBLIC_IDENTITY_FIELDS):
            return "", 204

    log_event(event_type, visit_id=visit_id, payload=payload,
              application_id=session.get(SESSION_VISIT_APP_KEY))
    return "", 204


@app.route("/submit", methods=["POST"])
def submit():
    # Normalize request.form into a clean dict
    form = {k: (v.strip() if isinstance(v, str) else v) for k, v in request.form.items()}

    # Get rep info from hidden field — verify HMAC to prevent tampering
    rep_code = form.get("rep_code", "").strip()
    rep_sig = form.get("rep_sig", "").strip()
    if rep_code and not verify_rep_code(rep_code, rep_sig):
        rep_code = ""  # reject tampered rep code
    rep_info = get_rep_info(rep_code)

    # Enforce default for has_owner_1 if not present
    if "has_owner_1" not in form or not form.get("has_owner_1"):
        form["has_owner_1"] = "No"

    # Normalize SSN/EIN: strip non-digits, re-insert dashes so validation works
    for ssn_key in ('owner_0_ssn', 'owner_1_ssn'):
        raw = form.get(ssn_key, '')
        digits = re.sub(r'\D', '', raw)
        if len(digits) == 9:
            form[ssn_key] = f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    raw_ein = form.get('ein', '')
    ein_digits = re.sub(r'\D', '', raw_ein)
    if len(ein_digits) == 9:
        form['ein'] = f"{ein_digits[:2]}-{ein_digits[2:]}"

    errors = validate_fields(form)

    if errors:
        log_event("submission_rejected", rep_code=rep_code,
                  payload={"fields": sorted(errors.keys()), "path": "legacy"})
        rep_sig = sign_rep_code(rep_code) if rep_code else ""
        return render_template(
            "form.html",
            errors=errors, form=form,
            rep_code=rep_code, rep_info=rep_info, rep_sig=rep_sig,
            idiq_signup_url=IDIQ_SIGNUP_URL,
        ), 400

    business_legal_name = form.get("business_legal_name") or ""
    industry = form.get("industry") or ""
    try:
        loan_amount = float(form.get("loan_amount") or 0)
    except Exception:
        loan_amount = 0.0

    # Owners list (for dashboard display)
    owners: List[str] = []
    first0 = (form.get("owner_0_first") or "").strip()
    last0 = (form.get("owner_0_last") or "").strip()
    if first0 or last0:
        owners.append((first0 + " " + last0).strip())

    has_owner_1 = (form.get("has_owner_1") or "No").strip()
    if has_owner_1 == "Yes":
        first1 = (form.get("owner_1_first") or "").strip()
        last1 = (form.get("owner_1_last") or "").strip()
        if first1 or last1:
            owners.append((first1 + " " + last1).strip())

    # Business lookup via SAM.gov (enrichment - never blocks submission)
    business_lookup = lookup_business_sam_gov(
        business_name=business_legal_name,
        state_code=form.get("company_state", ""),
        ein=form.get("ein", ""),
    )
    form["business_lookup"] = business_lookup
    log.info("SAM.gov lookup for '%s' (state=%s): status=%s",
             business_legal_name, form.get("company_state", ""),
             business_lookup.get("lookup_status"))

    # Industry classification (enrichment - never blocks submission)
    naics_info = classify_naics(business_legal_name, industry,
                                form.get("company_website") or "")
    if naics_info:
        form["naics"] = naics_info
        log.info("NAICS for '%s': %s / SIC %s / %s (%s)",
                 business_legal_name, naics_info.get("naics"),
                 naics_info.get("sic"), naics_info.get("bucket"),
                 naics_info.get("method"))

    # Insert into Supabase. IDIQ credentials are collected post-submit on the
    # thank-you page (see /idiq-credentials), so they're left NULL here.
    db_payload = {
        "business_legal_name": business_legal_name,
        "industry": industry,
        "loan_amount": loan_amount,
        "owners": owners,              # jsonb
        "payload": form,               # jsonb
        "ein": form.get("ein"),
        "business_phone": form.get("business_phone"),
        "company_website": form.get("company_website"),
        # Match keys promoted out of payload for underwriting queries; full
        # classification detail still lives in payload["naics"].
        "naics": naics_info.get("naics"),
        "sic": naics_info.get("sic"),
        "naics_bucket": naics_info.get("bucket"),
    }

    # Add rep info if available
    if rep_info:
        db_payload["rep_name"] = rep_info["name"]
        db_payload["rep_email"] = rep_info["email"]

    ins = sb.table("applications").insert(db_payload).execute()
    if not ins.data:
        abort(500, description="Insert failed")
    submission_id = ins.data[0]["id"]

    visit_id = current_visit_id()
    attach_visit_to_application(visit_id, submission_id)
    session[SESSION_VISIT_APP_KEY] = submission_id
    log_event("application_submitted", application_id=submission_id,
              visit_id=visit_id, rep_code=rep_code,
              payload={"loan_amount": loan_amount, "owners": len(owners),
                       "path": "legacy",
                       **({"rep_resolved": bool(rep_info)} if rep_code else {})})

    # Lead summary to the rep and the internal inboxes — independent of the
    # PDF pipeline, and the only application email a rep receives.
    _queue_lead_summary_email(form, submission_id, rep_info)

    # Process inline file uploads (bank statements / voided check / ID).
    # All optional per the new flow; failures are logged but don't block.
    _saved_types, saved_paths, _failed = _process_uploads(submission_id, request.files)
    if _failed:
        log.warning("Submission %s had upload failures: %s", submission_id, _failed)
    if _saved_types or _failed:
        log_event("documents_uploaded", application_id=submission_id,
                  payload={"types": _saved_types, "files": len(saved_paths),
                           "failed": len(_failed)})

    # Generate PDF and email it internally + to the applicant (background so
    # the user doesn't wait). Reps are not on this send.
    if PDF_ENABLED:
        try:
            rep_name = rep_info["name"] if rep_info else None
            log.info("Generating PDF for submission %s (rep=%s)", submission_id, rep_name)
            pdf_buffer = generate_application_pdf(form, submission_id, rep_name)

            if pdf_buffer:
                # Read the PDF bytes once. Each background thread gets its own
                # BytesIO so the two sends can run in parallel without racing
                # on buffer position.
                pdf_buffer.seek(0)
                pdf_bytes = pdf_buffer.read()

                recipients = _internal_recipients()

                def _bg_send_team(recips, biz, sid, rname, files):
                    try:
                        ok = send_email_with_pdf(
                            to_emails=recips, business_name=biz,
                            pdf_buffer=BytesIO(pdf_bytes), submission_id=sid,
                            rep_name=rname, attached_files=files,
                        )
                        if ok:
                            _mark_email_sent(sid, "initial_email_sent_at")
                    except Exception as exc:
                        log.error("Background team email failed for %s: %s", sid, exc)

                threading.Thread(
                    target=_bg_send_team,
                    args=(recipients, business_legal_name,
                          submission_id, rep_name, saved_paths),
                    daemon=True,
                ).start()
                log.info("Team email queued for submission %s → %s", submission_id, recipients)

                # Send a customer-facing receipt to the applicant. Best-effort —
                # failure is logged but never blocks the team email or the user
                # redirect. Basic "@" check guards malformed values from reaching
                # the email provider.
                applicant_email = (form.get("owner_0_email") or "").strip()
                if applicant_email and "@" in applicant_email:
                    # Magic link the merchant can click later to land back on
                    # /credit-setup without re-filling the application.
                    resume_token = sign_resume_token(submission_id)
                    resume_url = url_for("resume_application", token=resume_token, _external=True)

                    def _bg_send_applicant(to_email, biz, sid, files, link):
                        try:
                            send_email_with_pdf(
                                to_emails=[to_email], business_name=biz,
                                pdf_buffer=BytesIO(pdf_bytes), submission_id=sid,
                                rep_name=None, attached_files=files,
                                email_type="applicant_receipt",
                                resume_url=link,
                            )
                        except Exception as exc:
                            log.error("Background applicant email failed for %s: %s", sid, exc)

                    threading.Thread(
                        target=_bg_send_applicant,
                        args=(applicant_email, business_legal_name,
                              submission_id, saved_paths, resume_url),
                        daemon=True,
                    ).start()
                    log.info("Applicant receipt queued for submission %s → %s", submission_id, applicant_email)
                else:
                    log.info("No valid applicant email on submission %s — skipping receipt", submission_id)
            else:
                log.warning("PDF generation returned None for submission %s", submission_id)
        except Exception as e:
            log.error("Failed to generate PDF for submission %s: %s\n%s", submission_id, e, traceback.format_exc())
    else:
        log.warning("PDF_ENABLED is False – reportlab not installed. Skipping PDF/email for submission %s", submission_id)

    return redirect(url_for("thank_you", sid=submission_id))

# ── Resume flow ─────────────────────────────────────────────────────────────
# Lets the merchant come back later (via emailed magic link) to attach IDIQ
# credentials to a submitted application without going through the wizard
# again. Backed by signed 30-day tokens; admin can resend a fresh one from
# the dashboard, applicants can self-serve from the expired-link page.

SESSION_RESUME_KEY = "resume_sid"


def _email_resume_link(sid: int, to_email: str, business_name: str = "",
                       actor: str = "system") -> bool:
    """Send the merchant a fresh 30-day resume link. Returns True on success.

    `actor` says who caused the send -- "admin" from the dashboard's resend
    button, "system" when the merchant asked for a replacement themselves --
    and only shapes the activity row, never the email.
    """
    if not to_email or "@" not in to_email:
        return False
    token = sign_resume_token(sid)
    link = url_for("resume_application", token=token, _external=True)

    subject = "Complete your credit setup — Pathway Catalyst"
    html_body = f"""
<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:24px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.06);">
        <tr><td style="background:linear-gradient(135deg,#1e40af,#3b82f6);padding:28px 32px;text-align:center;">
          <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;">Pathway Catalyst</h1>
          <p style="margin:6px 0 0;color:#bfdbfe;font-size:13px;">Complete your application</p>
        </td></tr>
        <tr><td style="padding:28px 32px;">
          <p style="margin:0 0 12px;color:#1e293b;font-size:15px;">Hi{(' ' + business_name) if business_name else ''},</p>
          <p style="margin:0 0 16px;color:#475569;font-size:14px;line-height:1.6;">
            We're finishing the review of your business financing application.
            To proceed, we need to run a <strong>soft credit pull</strong> through IDIQ —
            it won't impact your score. Use the secure link below to set up your IDIQ account
            and share your credentials with us. The link is valid for 30 days.
          </p>
          <p style="margin:24px 0;text-align:center;">
            <a href="{link}" style="display:inline-block;background:linear-gradient(135deg,#2563eb,#3b82f6);color:#fff;text-decoration:none;padding:13px 28px;border-radius:8px;font-weight:600;font-size:15px;">
              Complete Credit Setup
            </a>
          </p>
          <p style="margin:0;color:#94a3b8;font-size:12px;line-height:1.5;">
            If the button doesn't work, copy and paste this URL into your browser:<br>
            <span style="word-break:break-all;color:#475569;">{link}</span>
          </p>
        </td></tr>
        <tr><td style="background:#f8fafc;padding:18px 32px;border-top:1px solid #e2e8f0;text-align:center;">
          <p style="margin:0 0 4px;color:#64748b;font-size:12px;">Pathway Catalyst &mdash; See the Pathway. Be the Catalyst.</p>
          <p style="margin:0;color:#94a3b8;font-size:11px;font-style:italic;">Powered by CROC</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>
""".strip()
    plain_text = (
        f"Hi{(' ' + business_name) if business_name else ''},\n\n"
        f"To finish reviewing your application, we need to run a soft credit pull "
        f"through IDIQ (no impact to your score). Click below to complete the "
        f"setup. The link is valid for 30 days.\n\n{link}\n\nPowered by CROC"
    )

    def _deliver() -> bool:
        try:
            if RESEND_API_KEY:
                return _send_via_resend([to_email], subject, html_body, plain_text,
                                        None, sid, None,
                                        message_id=None, in_reply_to=_application_message_id(sid))
            return _send_via_supabase_fn([to_email], subject, html_body, plain_text,
                                         None, sid, None,
                                         message_id=None, in_reply_to=_application_message_id(sid))
        except Exception:
            try:
                return _send_via_smtp([to_email], subject, html_body, plain_text,
                                      None, sid, None,
                                      message_id=None, in_reply_to=_application_message_id(sid))
            except Exception as e:
                log.error("Resume-link email failed for sid=%s to=%s: %s", sid, to_email, e)
                return False

    # Logged either way: a link that was never delivered is exactly what you
    # want to see on the timeline when the merchant says they never got one.
    ok = bool(_deliver())
    log_event("resume_link_sent", application_id=sid, actor=actor,
              payload={"to": to_email, "delivered": ok})
    return ok


@app.route("/resume")
def resume_application():
    """Validate a magic-link token; on success stash sid in the session and
    redirect to the standalone credit-setup page. On failure/expiry, show a
    page that lets the merchant request a fresh link by email."""
    token = request.args.get("token", "")
    sid, status = verify_resume_token(token)
    if status == "ok" and sid:
        session[SESSION_RESUME_KEY] = sid
        log_event("resume_link_opened", application_id=sid)
        return redirect(url_for("credit_setup"))
    # No application id to file this against -- a bad token names nobody.
    log_event("resume_link_rejected", payload={"reason": status})
    # Expired or invalid — render the recovery page; show "request another"
    # form for expired tokens, generic for invalid.
    return render_template(
        "credit_link_expired.html",
        expired=(status == "expired"),
        idiq_signup_url=IDIQ_SIGNUP_URL,
    ), 410 if status == "expired" else 404


@app.route("/credit-setup", methods=["GET"])
def credit_setup():
    sid = session.get(SESSION_RESUME_KEY)
    if not sid:
        # No active resume session — send them back to the expired/recover page.
        return redirect(url_for("credit_setup_link_lost"))

    res = sb.table("applications").select(
        "id, business_legal_name, idiq_username"
    ).eq("id", sid).limit(1).execute()
    rows = res.data or []
    if not rows:
        session.pop(SESSION_RESUME_KEY, None)
        return redirect(url_for("credit_setup_link_lost"))

    row = rows[0]
    log_event("credit_setup_viewed", application_id=sid,
              payload={"idiq_saved": bool(row.get("idiq_username"))})
    return render_template(
        "credit_setup.html",
        sid=sid,
        business=row.get("business_legal_name"),
        idiq_signup_url=IDIQ_SIGNUP_URL,
        idiq_already_saved=bool(row.get("idiq_username")),
        is_done=(request.args.get("done") == "1"),
    )


@app.route("/credit-setup/credentials", methods=["POST"])
def credit_setup_credentials():
    sid = session.get(SESSION_RESUME_KEY)
    if not sid:
        abort(403)

    username = (request.form.get("idiq_username") or "").strip()
    password = request.form.get("idiq_password") or ""
    if not username and not password:
        log_event("idiq_credentials_skipped", application_id=sid,
                  payload={"page": "credit_setup"})
        return redirect(url_for("credit_setup", done="1"))

    try:
        sb.table("applications").update({
            "idiq_username": username or None,
            "idiq_password_encrypted": encrypt_idiq_password(password) if password else None,
        }).eq("id", sid).execute()
    except Exception as exc:
        log.error("Failed to persist IDIQ creds via credit-setup for %s: %s", sid, exc)
        abort(500, description="Failed to save IDIQ credentials")

    # Presence flags only. The username is in the row and the password is
    # encrypted there; neither belongs in an audit payload.
    log_event("idiq_credentials_saved", application_id=sid,
              payload={"has_username": bool(username), "has_password": bool(password),
                       "page": "credit_setup"})
    return redirect(url_for("credit_setup", done="1"))


@app.route("/credit-setup/link-lost", methods=["GET", "POST"])
def credit_setup_link_lost():
    """Self-service flow for a lost/expired link.
    POST: user submits email; if any application matches, send a fresh resume
    link. Always return generic success so we don't leak which emails exist."""
    sent = False
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        if email and "@" in email:
            try:
                # Find the most recent application with this owner email and
                # send a fresh link there. We don't tell the user whether
                # anything matched.
                # Note: owner_0_email lives inside the JSONB payload column.
                # Postgrest supports `payload->>owner_0_email`.
                res = sb.table("applications").select(
                    "id, business_legal_name"
                ).filter("payload->>owner_0_email", "eq", email).order(
                    "id", desc=True
                ).limit(1).execute()
                rows = res.data or []
                log_event("resume_link_requested",
                          application_id=(rows[0]["id"] if rows else None),
                          payload={"matched": bool(rows)})
                if rows:
                    _email_resume_link(rows[0]["id"], email,
                                       business_name=rows[0].get("business_legal_name") or "")
            except Exception as exc:
                log.warning("Link-lost lookup failed for %r: %s", email, exc)
            sent = True  # always claim success — avoid email enumeration
    return render_template(
        "credit_link_expired.html",
        expired=False,
        sent=sent,
        idiq_signup_url=IDIQ_SIGNUP_URL,
    )


@app.route("/idiq-credentials", methods=["POST"])
def idiq_credentials():
    """Attach IDIQ login info to an already-submitted application.
    The user reaches this from the thank-you page after submitting the main
    application. Username is stored plain (needed for lookup); password is
    Fernet-encrypted with IDIQ_PASSWORD_KEY before persistence."""
    sid = request.form.get("sid", type=int)
    if not sid:
        abort(400)

    username = (request.form.get("idiq_username") or "").strip()
    password = request.form.get("idiq_password") or ""
    if not username and not password:
        # Nothing to do — user skipped. Bounce back to thank-you.
        log_event("idiq_credentials_skipped", application_id=sid,
                  payload={"page": "thank_you"})
        return redirect(url_for("thank_you", sid=sid))

    try:
        sb.table("applications").update({
            "idiq_username": username or None,
            "idiq_password_encrypted": encrypt_idiq_password(password) if password else None,
        }).eq("id", sid).execute()
    except Exception as exc:
        log.error("Failed to persist IDIQ credentials for %s: %s", sid, exc)
        abort(500, description="Failed to save IDIQ credentials")

    log_event("idiq_credentials_saved", application_id=sid,
              payload={"has_username": bool(username), "has_password": bool(password),
                       "page": "thank_you"})
    return redirect(url_for("thank_you", sid=sid, done="1"))


@app.route("/upload-docs", methods=["POST"])
def upload_docs():
    sid = request.form.get("sid", type=int)
    if not sid:
        abort(400)

    saved, attached_paths, failed = _process_uploads(sid, request.files)
    if saved or failed:
        log_event("documents_uploaded", application_id=sid,
                  payload={"types": saved, "files": len(attached_paths),
                           "failed": len(failed)})

    # Email uploaded documents to the internal inboxes (in background)
    if attached_paths:
        try:
            app_res = sb.table("applications").select(
                "business_legal_name, rep_name"
            ).eq("id", sid).execute()
            row = (app_res.data or [{}])[0]
            business_name = row.get("business_legal_name") or ""
            rep_name = row.get("rep_name")

            recipients = _internal_recipients()

            def _bg_send_docs(recips, biz, sid_, rname, files):
                try:
                    ok = send_email_with_pdf(
                        to_emails=recips, business_name=biz,
                        pdf_buffer=None, submission_id=sid_,
                        rep_name=rname, attached_files=files,
                        email_type="docs_update",
                    )
                    if ok:
                        _mark_email_sent(sid_, "docs_email_sent_at")
                except Exception as exc:
                    log.error("Background docs email failed for %s: %s", sid_, exc)

            threading.Thread(
                target=_bg_send_docs,
                args=(recipients, business_name, sid, rep_name, attached_paths),
                daemon=True,
            ).start()
            log.info("Docs email queued for submission %s → %s", sid, recipients)
        except Exception as e:
            log.error("Failed to queue docs email for %s: %s", sid, e)

    return render_template("thank_you.html", sid=sid, uploaded=saved, failed=failed)

# -------------------- JSON APIs for Dashboard --------------------
@app.route("/api/submissions")
@admin_required
def api_submissions():
    try:
        limit = int(request.args.get("limit", "100"))
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        limit, offset = 100, 0
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)

    rep_filter = request.args.get("rep", "").strip()
    q = request.args.get("q", "").strip()

    start = offset
    end = offset + limit - 1

    query = sb.table("applications").select(
        "id, created_at, business_legal_name, industry, loan_amount, owners, payload, company_website, rep_name, rep_email",
        count="exact",
    )

    if rep_filter:
        rep_info = get_rep_info(rep_filter)
        if rep_info:
            query = query.eq("rep_name", rep_info["name"])

    if q:
        # PostgREST `or` filter: escape commas/parens so user input can't break out of the expression.
        safe = q.replace("\\", "\\\\").replace(",", "\\,").replace("(", "\\(").replace(")", "\\)")
        pattern = f"*{safe}*"
        query = query.or_(
            f"business_legal_name.ilike.{pattern},industry.ilike.{pattern},rep_name.ilike.{pattern}"
        )

    res = query.order("id", desc=True).range(start, end).execute()
    rows = res.data or []
    for r in rows:
        if r.get("loan_amount") is not None:
            r["loan_amount"] = float(r["loan_amount"])
    return jsonify({"rows": rows, "total": res.count or 0})

@app.route("/api/submissions/<int:sid>/resend-credit-link", methods=["POST"])
@admin_required
def api_resend_credit_link(sid: int):
    """Admin-triggered: email a fresh resume link for this application.
    Defaults to owner_0_email; admin may override via `email` in the JSON body."""
    body = request.get_json(silent=True) or {}
    override = (body.get("email") or "").strip().lower()

    res = sb.table("applications").select(
        "id, business_legal_name, payload"
    ).eq("id", sid).limit(1).execute()
    rows = res.data or []
    if not rows:
        return jsonify({"error": "Application not found."}), 404

    row = rows[0]
    payload = row.get("payload") or {}
    default_email = (payload.get("owner_0_email") or "").strip().lower()
    to_email = override or default_email
    if not to_email or "@" not in to_email:
        return jsonify({"error": "No valid email — provide one in the override field."}), 400

    ok = _email_resume_link(sid, to_email, business_name=row.get("business_legal_name") or "",
                            actor="admin")
    if not ok:
        return jsonify({"error": "Failed to send email (check provider config)."}), 500
    return jsonify({"ok": True, "sent_to": to_email})


def _get_uploaded_doc_types(sid: int) -> set:
    """Return the set of doc_type strings already on file for a submission."""
    res = sb.table("application_files").select("doc_type").eq("application_id", sid).execute()
    return {r["doc_type"] for r in (res.data or [])}


def _email_docs_reminder(sid: int, to_email: str, business_name: str,
                         missing: list[str]) -> bool:
    """Send the applicant a reminder listing which documents are still needed."""
    if not to_email or "@" not in to_email:
        return False

    token = sign_resume_token(sid)
    link = url_for("resume_application", token=token, _external=True)

    labels = {
        "bank_statement": "4 months of business bank statements (PDF)",
        "voided_check": "Voided check",
        "id_doc": "Driver's license / government-issued ID",
    }
    missing_html = "".join(
        f'<li style="margin:6px 0;color:#1e293b;font-size:14px;">{labels.get(d, d)}</li>'
        for d in missing
    )
    missing_plain = "\n".join(f"  - {labels.get(d, d)}" for d in missing)

    subject = f"Documents needed — {business_name or 'Your Application'}"
    html_body = f"""
<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f1f5f9;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:24px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.06);">
        <tr><td style="background:linear-gradient(135deg,#1e40af,#3b82f6);padding:28px 32px;text-align:center;">
          <h1 style="margin:0;color:#ffffff;font-size:22px;font-weight:700;">Pathway Catalyst</h1>
          <p style="margin:6px 0 0;color:#bfdbfe;font-size:13px;">Documents still needed</p>
        </td></tr>
        <tr><td style="padding:28px 32px;">
          <p style="margin:0 0 12px;color:#1e293b;font-size:15px;">Hi{(' ' + business_name) if business_name else ''},</p>
          <p style="margin:0 0 16px;color:#475569;font-size:14px;line-height:1.6;">
            We're reviewing your business financing application and still need the following
            document(s) to move forward:
          </p>
          <ul style="margin:0 0 20px;padding-left:20px;">{missing_html}</ul>
          <p style="margin:0 0 20px;color:#475569;font-size:14px;line-height:1.6;">
            You can reply directly to this email with the files attached, or use the
            secure link below. The link is valid for 30 days.
          </p>
          <p style="margin:24px 0;text-align:center;">
            <a href="{link}" style="display:inline-block;background:linear-gradient(135deg,#2563eb,#3b82f6);color:#fff;text-decoration:none;padding:13px 28px;border-radius:8px;font-weight:600;font-size:15px;">
              Upload Documents
            </a>
          </p>
          <p style="margin:0;color:#94a3b8;font-size:12px;line-height:1.5;">
            If the button doesn't work, copy and paste this URL into your browser:<br>
            <span style="word-break:break-all;color:#475569;">{link}</span>
          </p>
        </td></tr>
        <tr><td style="background:#f8fafc;padding:18px 32px;border-top:1px solid #e2e8f0;text-align:center;">
          <p style="margin:0 0 4px;color:#64748b;font-size:12px;">Pathway Catalyst &mdash; See the Pathway. Be the Catalyst.</p>
          <p style="margin:0;color:#94a3b8;font-size:11px;font-style:italic;">Powered by CROC</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>
""".strip()
    plain_text = (
        f"Hi{(' ' + business_name) if business_name else ''},\n\n"
        f"We're reviewing your application and still need the following documents:\n\n"
        f"{missing_plain}\n\n"
        f"You can reply to this email with the files attached, or use this link "
        f"(valid 30 days):\n{link}\n\nPowered by CROC"
    )

    try:
        if RESEND_API_KEY:
            return _send_via_resend([to_email], subject, html_body, plain_text,
                                    None, sid, None,
                                    message_id=None, in_reply_to=_application_message_id(sid))
        return _send_via_supabase_fn([to_email], subject, html_body, plain_text,
                                     None, sid, None,
                                     message_id=None, in_reply_to=_application_message_id(sid))
    except Exception:
        try:
            return _send_via_smtp([to_email], subject, html_body, plain_text,
                                  None, sid, None,
                                  message_id=None, in_reply_to=_application_message_id(sid))
        except Exception as e:
            log.error("Docs reminder email failed for sid=%s to=%s: %s", sid, to_email, e)
            return False


@app.route("/api/submissions/<int:sid>/remind-docs", methods=["POST"])
@admin_required
def api_remind_docs(sid: int):
    """Admin-triggered: email the applicant about missing documents.
    Checks which doc types are already uploaded and reminds about the rest."""
    body = request.get_json(silent=True) or {}
    override = (body.get("email") or "").strip().lower()

    res = sb.table("applications").select(
        "id, business_legal_name, payload"
    ).eq("id", sid).limit(1).execute()
    rows = res.data or []
    if not rows:
        return jsonify({"error": "Application not found."}), 404

    row = rows[0]
    payload = row.get("payload") or {}
    default_email = (payload.get("owner_0_email") or "").strip().lower()
    to_email = override or default_email
    if not to_email or "@" not in to_email:
        return jsonify({"error": "No valid email — provide one in the override field."}), 400

    uploaded = _get_uploaded_doc_types(sid)
    all_types = ["bank_statement", "voided_check", "id_doc"]
    missing = [d for d in all_types if d not in uploaded]

    if not missing:
        return jsonify({"ok": True, "message": "All documents already on file.", "missing": []})

    ok = _email_docs_reminder(
        sid, to_email,
        business_name=row.get("business_legal_name") or "",
        missing=missing,
    )
    if not ok:
        return jsonify({"error": "Failed to send email (check provider config)."}), 500
    log_event("docs_reminder_sent", application_id=sid, actor="admin",
              payload={"to": to_email, "missing": missing,
                       "by": session.get("admin_email")})
    return jsonify({"ok": True, "sent_to": to_email, "missing": missing})


@app.route("/api/submissions/<int:sid>")
@admin_required
def api_submission_detail(sid: int):
    app_res = sb.table("applications").select(
        "id, created_at, business_legal_name, industry, loan_amount, owners, payload, company_website, rep_name, rep_email"
    ).eq("id", sid).execute()
    rows = app_res.data or []
    if not rows:
        abort(404)
    app_row = rows[0]
    if app_row.get("loan_amount") is not None:
        app_row["loan_amount"] = float(app_row["loan_amount"])

    files_res = sb.table("application_files").select(
        "id, filename, storage_path, size_bytes, doc_type"
    ).eq("application_id", sid).execute()
    files = files_res.data or []
    for f in files:
        try:
            f["url"] = _get_signed_url(f["storage_path"])
        except Exception as e:
            log.error("Failed to generate signed URL for %s: %s", f["storage_path"], e)
            f["url"] = ""

    app_row["files"] = files
    # Read before the view is recorded, so the modal never opens on its own
    # footprint -- this visit shows up the next time someone looks.
    app_row["events"] = fetch_application_events(sid)
    log_event("admin_viewed_application", application_id=sid, actor="admin",
              payload={"by": session.get("admin_email")})
    return jsonify(app_row)


@app.route("/api/submissions/<int:sid>/pdf")
@admin_required
def api_submission_pdf(sid: int):
    if not PDF_ENABLED:
        abort(501, description="PDF generation is not available on this server.")
    app_res = sb.table("applications").select(
        "id, created_at, payload, rep_name"
    ).eq("id", sid).execute()
    rows = app_res.data or []
    if not rows:
        abort(404)
    row = rows[0]
    payload = row.get("payload") or {}
    pdf_buf = generate_application_pdf(payload, row["id"], row.get("rep_name"))
    if pdf_buf is None:
        abort(500, description="PDF generation failed.")
    pdf_buf.seek(0)
    biz = payload.get("business_legal_name", "application")
    safe_name = re.sub(r"[^A-Za-z0-9_\- ]", "", biz).strip().replace(" ", "_") or "application"
    filename = f"Pathway_Application_{row['id']}_{safe_name}.pdf"
    log_event("admin_downloaded_pdf", application_id=sid, actor="admin",
              payload={"by": session.get("admin_email")})
    return send_file(pdf_buf, mimetype="application/pdf", as_attachment=True, download_name=filename)


_REP_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def _validate_rep_payload(data: dict, *, require_code: bool) -> tuple[Optional[dict], Optional[str]]:
    """Normalize and validate a rep create/edit payload. Returns (clean, error)."""
    if not isinstance(data, dict):
        return None, "Body must be a JSON object."
    clean = {}
    if require_code:
        code = (data.get("code") or "").strip().lower()
        if not _REP_CODE_RE.match(code):
            return None, "Code must be lowercase alphanumeric (with -/_), 1–64 chars."
        clean["code"] = code
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return None, "Name is required."
        clean["name"] = name
    elif require_code:
        return None, "Name is required."
    if "email" in data:
        email = (data.get("email") or "").strip().lower()
        if not _EMAIL_RE.match(email):
            return None, "A valid email is required."
        clean["email"] = email
    elif require_code:
        return None, "Email is required."
    if "active" in data:
        clean["active"] = bool(data["active"])
    return clean, None

@app.route("/api/csrf-token")
@admin_required
def api_csrf_token():
    """Lets static admin pages (dashboard.html, rep-links.html) get a CSRF token for write requests."""
    return jsonify({"token": generate_csrf()})

@app.route("/api/reps", methods=["GET"])
@admin_required
def api_reps():
    """List sales reps with their unique links. Includes inactive by default for admin view."""
    include_inactive = request.args.get("include_inactive", "1") != "0"
    # ?brand=<slug> builds the links on that brand; otherwise the default brand.
    # The page also recomposes links client-side from /api/brands link_base, so
    # switching brands in the picker costs no round trip.
    brand = (get_brand_by_slug(request.args.get("brand", ""), include_inactive=True)
             or get_default_brand())
    reps = list(_get_reps_cached().values())
    if not include_inactive:
        reps = [r for r in reps if r.get("active", True)]
    reps.sort(key=lambda r: (not r.get("active", True), r["code"]))
    return jsonify([
        {
            "code": r["code"],
            "name": r["name"],
            "email": r["email"],
            "active": r.get("active", True),
            "link": brand_rep_link(brand, r["code"]),
        }
        for r in reps
    ])

@app.route("/api/reps", methods=["POST"])
@admin_required
def api_reps_create():
    clean, err = _validate_rep_payload(request.get_json(silent=True) or {}, require_code=True)
    if err:
        return jsonify({"error": err}), 400
    existing = _get_reps_cached().get(clean["code"])
    if existing:
        return jsonify({"error": f"Rep code '{clean['code']}' already exists."}), 409
    try:
        sb.table("sales_reps").insert({
            "code": clean["code"],
            "name": clean["name"],
            "email": clean["email"],
            "active": clean.get("active", True),
        }).execute()
    except Exception as e:
        log.warning("Rep insert failed: %s", e)
        return jsonify({"error": "Failed to create rep."}), 500
    _invalidate_rep_cache()
    return jsonify({"ok": True, "code": clean["code"]}), 201

@app.route("/api/reps/<code>", methods=["PATCH"])
@admin_required
def api_reps_update(code: str):
    code = code.lower().strip()
    if not _get_reps_cached().get(code):
        return jsonify({"error": "Rep not found."}), 404
    clean, err = _validate_rep_payload(request.get_json(silent=True) or {}, require_code=False)
    if err:
        return jsonify({"error": err}), 400
    clean.pop("code", None)
    if not clean:
        return jsonify({"error": "No fields to update."}), 400
    try:
        sb.table("sales_reps").update(clean).eq("code", code).execute()
    except Exception as e:
        log.warning("Rep update failed: %s", e)
        return jsonify({"error": "Failed to update rep."}), 500
    _invalidate_rep_cache()
    return jsonify({"ok": True})

@app.route("/api/reps/<code>", methods=["DELETE"])
@admin_required
def api_reps_deactivate(code: str):
    """Soft-delete: set active=false so historical submissions remain attributable."""
    code = code.lower().strip()
    if not _get_reps_cached().get(code):
        return jsonify({"error": "Rep not found."}), 404
    try:
        sb.table("sales_reps").update({"active": False}).eq("code", code).execute()
    except Exception as e:
        log.warning("Rep deactivate failed: %s", e)
        return jsonify({"error": "Failed to deactivate rep."}), 500
    _invalidate_rep_cache()
    return jsonify({"ok": True})

_BRAND_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_BRANDS_MISSING = ("Branded links are not set up yet. Apply migration "
                   "20260818_add_client_brands.sql to your Supabase project.")

def _reserved_slugs() -> set:
    """First path segments already claimed by real routes.

    `/<client_slug>` is a catch-all, so a brand slugged `admin` would hand reps
    a link that lands on the admin login instead of the form. Read the live URL
    map rather than a hand-kept list, so a route added later can't be shadowed.
    """
    out = {"static"}
    for rule in app.url_map.iter_rules():
        head = rule.rule.lstrip("/").split("/")[0]
        if head and "<" not in head:
            out.add(head.lower())
    return out

def _normalize_domain(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Accept what an admin actually pastes and return a bare host.

    'https://application.croccrm.com/' and 'Application.CrocCRM.com' both
    normalize to 'application.croccrm.com'. Returns (domain|None, error|None);
    an empty value is valid and means "use this app's host with a path slug".
    """
    raw = (raw or "").strip()
    if not raw:
        return None, None
    raw = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", raw)  # strip scheme
    raw = raw.split("/")[0].split("?")[0].split("#")[0]     # strip path/query
    raw = raw.split("@")[-1].split(":")[0]                  # strip creds/port
    domain = raw.strip(".").lower()
    if not domain:
        return None, None
    if len(domain) > 253 or not _DOMAIN_RE.match(domain):
        return None, f"'{raw}' is not a valid domain (expected e.g. application.croccrm.com)."
    return domain, None

def _validate_brand_payload(data: dict, *, require_slug: bool) -> tuple[Optional[dict], Optional[str]]:
    """Normalize and validate a brand create/edit payload. Returns (clean, error)."""
    if not isinstance(data, dict):
        return None, "Body must be a JSON object."
    clean = {}
    if require_slug:
        slug = (data.get("slug") or "").strip().lower()
        if not _BRAND_SLUG_RE.match(slug):
            return None, "Slug must be lowercase alphanumeric (with -/_), 1-64 chars."
        if slug in _reserved_slugs():
            return None, f"'{slug}' is reserved by an existing page — pick another slug."
        clean["slug"] = slug
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return None, "Name is required."
        clean["name"] = name
    elif require_slug:
        return None, "Name is required."
    if "domain" in data:
        domain, err = _normalize_domain(data.get("domain"))
        if err:
            return None, err
        clean["domain"] = domain
    if "active" in data:
        clean["active"] = bool(data["active"])
    if "is_default" in data:
        clean["is_default"] = bool(data["is_default"])
    return clean, None

def _clear_other_defaults(except_slug: str) -> None:
    """Only one brand may be default (enforced by a partial unique index)."""
    sb.table("client_brands").update({"is_default": False}) \
        .eq("is_default", True).neq("slug", except_slug).execute()

def _brand_conflicting_domain(domain: str, except_slug: str = "") -> Optional[str]:
    if not domain:
        return None
    for b in _get_brands_cached():
        if b["domain"] == domain and b["slug"] != except_slug:
            return b["slug"]
    return None

@app.route("/api/brands", methods=["GET"])
@admin_required
def api_brands():
    """Brands available for rep links, default first.

    `link_base` is what /admin/reps concatenates `?rep=<code>` onto, so the
    page never has to know how a brand's URL is shaped.
    """
    include_inactive = request.args.get("include_inactive", "1") != "0"
    brands = [b for b in _get_brands_cached() if include_inactive or b["active"]]
    return jsonify({
        "configured": bool(_brand_cache.get("table_ok", True)),
        "app_host": request.host_url.rstrip("/"),
        "brands": [
            {**b, "link_base": brand_link_base(b), "example": brand_rep_link(b, "tom")}
            for b in brands
        ],
    })

@app.route("/api/brands", methods=["POST"])
@admin_required
def api_brands_create():
    clean, err = _validate_brand_payload(request.get_json(silent=True) or {}, require_slug=True)
    if err:
        return jsonify({"error": err}), 400
    if get_brand_by_slug(clean["slug"], include_inactive=True):
        return jsonify({"error": f"Brand '{clean['slug']}' already exists."}), 409
    dupe = _brand_conflicting_domain(clean.get("domain"), clean["slug"])
    if dupe:
        return jsonify({"error": f"Domain already used by brand '{dupe}'."}), 409
    row = {
        "slug": clean["slug"],
        "name": clean["name"],
        "domain": clean.get("domain"),
        "active": clean.get("active", True),
        "is_default": clean.get("is_default", False),
    }
    try:
        if row["is_default"]:
            _clear_other_defaults(row["slug"])
        sb.table("client_brands").insert(row).execute()
    except Exception as e:
        log.warning("Brand insert failed: %s", e)
        msg = _BRANDS_MISSING if not _brand_cache.get("table_ok", True) else "Failed to create brand."
        return jsonify({"error": msg}), 500
    _invalidate_brand_cache()
    return jsonify({"ok": True, "slug": row["slug"]}), 201

@app.route("/api/brands/<slug>", methods=["PATCH"])
@admin_required
def api_brands_update(slug: str):
    slug = slug.lower().strip()
    existing = get_brand_by_slug(slug, include_inactive=True)
    if not existing:
        return jsonify({"error": "Brand not found."}), 404
    clean, err = _validate_brand_payload(request.get_json(silent=True) or {}, require_slug=False)
    if err:
        return jsonify({"error": err}), 400
    clean.pop("slug", None)
    if not clean:
        return jsonify({"error": "No fields to update."}), 400
    if "domain" in clean:
        dupe = _brand_conflicting_domain(clean["domain"], slug)
        if dupe:
            return jsonify({"error": f"Domain already used by brand '{dupe}'."}), 409
    if clean.get("active") is False and existing["is_default"]:
        return jsonify({"error": "Make another brand the default before deactivating this one."}), 400
    try:
        if clean.get("is_default"):
            _clear_other_defaults(slug)
            clean["active"] = True  # the default must be usable
        sb.table("client_brands").update(clean).eq("slug", slug).execute()
    except Exception as e:
        log.warning("Brand update failed: %s", e)
        return jsonify({"error": "Failed to update brand."}), 500
    _invalidate_brand_cache()
    return jsonify({"ok": True})

@app.route("/api/brands/<slug>", methods=["DELETE"])
@admin_required
def api_brands_deactivate(slug: str):
    """Soft-delete: links already handed out keep resolving (see home_client)."""
    slug = slug.lower().strip()
    existing = get_brand_by_slug(slug, include_inactive=True)
    if not existing:
        return jsonify({"error": "Brand not found."}), 404
    if existing["is_default"]:
        return jsonify({"error": "Make another brand the default before deactivating this one."}), 400
    try:
        sb.table("client_brands").update({"active": False}).eq("slug", slug).execute()
    except Exception as e:
        log.warning("Brand deactivate failed: %s", e)
        return jsonify({"error": "Failed to deactivate brand."}), 500
    _invalidate_brand_cache()
    return jsonify({"ok": True})


# -------------------- Activity Dashboard --------------------
# The per-application timeline answers "what happened to this lead". This
# answers the question it cannot: what happened to everyone who opened a rep
# link and never became a lead at all. Those visits carry a visit_id and no
# application_id, so they appear nowhere else in the admin UI.
# Roughly half of all form opens are crawlers -- 24 in the first day were
# Facebook fetching a link preview. They never run the step beacons, so every
# one lands in the "left on page 1" column and drags the funnel down with it.
# They are marked rather than dropped: the dashboard hides them by default and
# says how many it hid, because "your link is being shared on Facebook" is
# itself worth knowing.
_AUTOMATED_UA = re.compile(
    r"bot|crawl|spider|slurp|headless|python-|curl/|wget|facebookexternalhit|"
    r"semrush|ahrefs|barkrowler|seranking|monitor|uptime|scan|preview|fetch|"
    # Non-browser HTTP clients: test harnesses, SDKs and API tools. None of
    # them is a person filling in a form.
    r"werkzeug|okhttp|java/|go-http|node-fetch|axios|postman|insomnia|libwww",
    re.I)


def _looks_automated(user_agent: Optional[str], ip: Optional[str] = None) -> bool:
    # Loopback behind ProxyFix means the request came from the box itself --
    # a health check, a smoke test, something local. Never an applicant.
    if (ip or "") in ("127.0.0.1", "::1"):
        return True
    ua = (user_agent or "").strip()
    return not ua or bool(_AUTOMATED_UA.search(ua))


ACTIVITY_WINDOWS = (1, 7, 30, 90)      # days; anything else snaps to 7
ACTIVITY_MAX_EVENTS = 20000            # ceiling on one page load
ACTIVITY_FEED_LIMIT = 60               # recent events shown raw
ACTIVITY_VISIT_LIMIT = 300             # recent visits listed


def _visit_rows(days: int) -> tuple[list, list, bool]:
    """Every applicant event in the window, newest first, plus the raw feed.

    Aggregated in Python rather than SQL because PostgREST cannot GROUP BY, and
    a database view would mean a second migration to apply before this page
    worked at all -- which is exactly the deploy-ordering trap the rest of this
    feature avoids. At current volume (a few hundred form views a week) the
    whole window is a cheap single read. If it ever stops being cheap, the fix
    is a Postgres view returning the shape assembled below.
    """
    since = (datetime.now(EASTERN) - timedelta(days=days)).isoformat()
    res = (_events_sb.table("application_events")
           .select("id, created_at, event_type, actor, application_id, "
                   "visit_id, rep_code, brand_slug, ip, user_agent, payload")
           .gte("created_at", since)
           .order("id", desc=True)
           .limit(ACTIVITY_MAX_EVENTS)
           .execute())
    rows = res.data or []
    return rows, rows[:ACTIVITY_FEED_LIMIT], len(rows) >= ACTIVITY_MAX_EVENTS


def _visit_seconds(opened_at: str, last_at: str) -> Optional[int]:
    """How long the visit lasted, for spotting a nine-minute abandonment
    against a three-second bounce."""
    try:
        a = datetime.fromisoformat((opened_at or "").replace("Z", "+00:00"))
        b = datetime.fromisoformat((last_at or "").replace("Z", "+00:00"))
        return max(0, int((b - a).total_seconds()))
    except Exception:
        return None


def _summarize_visits(rows: list, include_automated: bool = False) -> dict:
    """Fold raw events into one record per visit, then per rep link.

    Crawlers are excluded from every figure by default and counted separately.
    Filtering them in the browser instead would leave the tiles disagreeing
    with the table as soon as the visit list is capped, and a funnel that
    counts Facebook's link-preview fetcher as a lead is simply wrong.
    """
    visits: dict = {}
    for r in reversed(rows):                       # oldest first, so first-seen wins
        if r.get("actor") != "applicant":
            continue                               # admin clicks are not link opens
        vid = r.get("visit_id")
        if not vid:
            continue
        v = visits.setdefault(vid, {
            "visit_id": vid, "opened_at": None, "last_at": None,
            "rep_code": None, "brand_slug": None, "furthest_step": 1,
            "submitted": False, "application_id": None, "abandoned": False,
            "rep_resolved": None, "ip": None, "user_agent": None, "events": 0,
            "step_views": 0, "doc_files": 0, "doc_types": [], "idiq": None,
            "credit_setup_opens": 0, "automated": False, "identity": None,
        })
        v["events"] += 1
        if r["event_type"] == "form_step_viewed":
            # Only form_viewed carries the rep code, so step beacons are
            # attributed through the visit they belong to.
            v["step_views"] += 1
        v["last_at"] = r["created_at"]
        payload = r.get("payload") or {}
        if r["event_type"] == "form_viewed":
            v["opened_at"] = v["opened_at"] or r["created_at"]
            v["rep_code"] = r.get("rep_code")
            v["brand_slug"] = r.get("brand_slug")
            v["ip"], v["user_agent"] = r.get("ip"), r.get("user_agent")
            # Absent on a direct visit -- only a link that carried a code can
            # have dropped one.
            v["rep_resolved"] = payload.get("rep_resolved")
        step = payload.get("step")
        if isinstance(step, int):
            v["furthest_step"] = max(v["furthest_step"], step)
        if r["event_type"] == "form_identity_captured":
            # Latest capture wins: they may have gone back, fixed a typo in
            # the email and come forward again.
            captured = {k: payload[k] for k in PUBLIC_IDENTITY_FIELDS if payload.get(k)}
            if captured:
                v["identity"] = captured
        if r["event_type"] == "form_abandoned":
            v["abandoned"] = True
        if r["event_type"] == "application_submitted":
            v["submitted"] = True
            v["application_id"] = r.get("application_id")

    # Only visits that actually started at the form. A visit first seen
    # mid-journey (an old session beaconing after a deploy) has no open to count.
    seen = [v for v in visits.values() if v["opened_at"]]
    for v in seen:
        v["automated"] = _looks_automated(v["user_agent"], v["ip"])
        # What actually happened, in one word the dashboard can filter on.
        if v["submitted"]:
            v["outcome"] = "completed"
        elif v["furthest_step"] >= 2:
            v["outcome"] = "half_filled"
        else:
            v["outcome"] = "left_page_1"
        v["seconds"] = _visit_seconds(v["opened_at"], v["last_at"])

    # Everything after the submit -- uploads, the credit-setup link, IDIQ --
    # happens in a later session opened from an email, which has no visit of
    # its own and carries only an application id. Folded on by application, or
    # it would show up nowhere at all.
    by_app: dict = {}
    for r in rows:
        if r.get("actor") != "applicant":
            continue
        aid = r.get("application_id")
        if not aid:
            continue
        payload = r.get("payload") or {}
        a = by_app.setdefault(aid, {"doc_files": 0, "doc_types": [],
                                    "idiq": None, "credit_setup_opens": 0})
        if r["event_type"] == "documents_uploaded":
            a["doc_files"] += payload.get("files") or 0
            for dt in payload.get("types") or []:
                if dt not in a["doc_types"]:
                    a["doc_types"].append(dt)
        elif r["event_type"] == "idiq_credentials_saved":
            a["idiq"] = "saved"
        elif r["event_type"] == "idiq_credentials_skipped" and not a["idiq"]:
            a["idiq"] = "skipped"
        elif r["event_type"] in ("credit_setup_viewed", "resume_link_opened"):
            a["credit_setup_opens"] += 1
    for v in seen:
        v.update(by_app.get(v.get("application_id"), {}))

    automated_count = sum(1 for v in seen if v["automated"])
    if not include_automated:
        seen = [v for v in seen if not v["automated"]]

    by_rep: dict = {}
    for v in seen:
        key = v["rep_code"] or ""
        g = by_rep.setdefault(key, {
            "rep_code": v["rep_code"], "opens": 0, "submitted": 0,
            "dropped": 0, "unresolved": 0, "step_total": 0,
            "left_after_details": 0, "bounced": 0, "step_views": 0,
        })
        g["opens"] += 1
        g["step_total"] += v["furthest_step"]
        g["step_views"] += v["step_views"]
        if v["submitted"]:
            g["submitted"] += 1
        else:
            g["dropped"] += 1
            # Two very different failures, and lumping them together hides
            # both: someone who typed real details and gave up is a lead worth
            # chasing, someone who never left page one is a traffic problem.
            if v["furthest_step"] >= 2:
                g["left_after_details"] += 1
            else:
                g["bounced"] += 1
        if v["rep_code"] and v["rep_resolved"] is False:
            # Guarded on rep_code as well as the flag: events written before
            # the flag was made conditional still carry rep_resolved=false on
            # visits that never had a code, and a "direct" link cannot drop one.
            g["unresolved"] += 1

    for g in by_rep.values():
        opens = g["opens"] or 1
        g["conversion"] = round(100.0 * g["submitted"] / opens, 1)
        g["avg_step"] = round(g.pop("step_total") / opens, 1)
        # Everyone who got past the entry page, whether or not they finished.
        g["started"] = g["submitted"] + g["left_after_details"]

    daily: dict = {}
    for v in seen:
        day = v["opened_at"][:10]
        d = daily.setdefault(day, {"date": day, "opens": 0, "submitted": 0})
        d["opens"] += 1
        if v["submitted"]:
            d["submitted"] += 1

    submitted = sum(1 for v in seen if v["submitted"])
    left_after = sum(1 for v in seen if not v["submitted"] and v["furthest_step"] >= 2)
    return {
        "totals": {
            "opens": len(seen),
            "submitted": submitted,
            "dropped": len(seen) - submitted,
            "left_after_details": left_after,
            "half_filled_identified": sum(
                1 for v in seen
                if not v["submitted"] and v["furthest_step"] >= 2 and v.get("identity")),
            "bounced": len(seen) - submitted - left_after,
            "started": submitted + left_after,
            "step_views": sum(v["step_views"] for v in seen),
            "conversion": round(100.0 * submitted / len(seen), 1) if seen else 0.0,
            "unresolved": sum(1 for v in seen
                          if v["rep_code"] and v["rep_resolved"] is False),
        },
        "by_rep": sorted(by_rep.values(), key=lambda g: (-g["opens"], g["rep_code"] or "")),
        "daily": [daily[k] for k in sorted(daily)],
        "visits": sorted(seen, key=lambda v: v["opened_at"], reverse=True)[:ACTIVITY_VISIT_LIMIT],
        "automated_excluded": 0 if include_automated else automated_count,
        "automated_seen": automated_count,
    }


def _attach_application_detail(visits: list) -> None:
    """Fill in business name, amount and IDIQ state for visits that submitted."""
    ids = sorted({v["application_id"] for v in visits if v.get("application_id")})
    if not ids:
        return
    try:
        res = sb.table("applications").select(
            "id, business_legal_name, loan_amount, idiq_username"
        ).in_("id", ids).execute()
        by_id = {row["id"]: row for row in (res.data or [])}
    except Exception as exc:
        log.warning("Could not attach application detail to visits: %s", exc)
        return
    for v in visits:
        row = by_id.get(v.get("application_id"))
        if not row:
            continue
        v["business_name"] = row.get("business_legal_name")
        try:
            v["loan_amount"] = float(row["loan_amount"]) if row.get("loan_amount") is not None else None
        except (TypeError, ValueError):
            v["loan_amount"] = None
        # The row is the authority on credentials; the event only says one was
        # submitted at some point, and a later edit would not re-fire it.
        if row.get("idiq_username"):
            v["idiq"] = "saved"


@app.route("/api/activity/summary")
@admin_required
def api_activity_summary():
    try:
        days = int(request.args.get("days", "7"))
    except ValueError:
        days = 7
    if days not in ACTIVITY_WINDOWS:
        days = 7

    if not _events_table_ok:
        return jsonify({"available": False,
                        "reason": "Activity tracing is off — the "
                                  "application_events table is missing."}), 200
    try:
        rows, feed, truncated = _visit_rows(days)
    except Exception as exc:
        log.warning("Activity summary failed: %s", exc)
        return jsonify({"available": False,
                        "reason": "Could not read the activity trail."}), 200

    include_automated = request.args.get("bots") == "1"
    out = _summarize_visits(rows, include_automated=include_automated)
    # A completed visit is only useful if you can see whose it is. Abandoned
    # visits stay anonymous by design -- nothing typed into the form reaches
    # the server until submit -- so the name column is blank for them.
    _attach_application_detail(out["visits"])
    # Every rep, not only those with traffic in the window. A link nobody
    # opened all week is the finding you most want to see, and it can only be
    # seen if it is selectable -- filtering to it and getting zeros is the
    # answer, not an empty dropdown.
    out["known_reps"] = sorted(
        ({"code": code, "name": r.get("name"), "active": bool(r.get("active", True))}
         for code, r in _get_reps_cached().items()),
        key=lambda r: (not r["active"], r["code"]))
    out.update({
        "available": True,
        "window_days": days,
        "include_automated": include_automated,
        "truncated": truncated,
        "event_count": len(rows),
        "feed": feed,
        "generated_at": datetime.now(EASTERN).isoformat(),
    })
    return jsonify(out)


@app.route("/admin/activity")
@admin_required
def admin_activity():
    return send_from_directory(str(APP_DIR / "public"), "activity.html")


# -------------------- Admin Login --------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin_authed"):
        return redirect(request.args.get("next") or url_for("admin_static_dashboard"))

    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        if (
            ADMIN_EMAIL
            and ADMIN_PASSWORD_HASH
            and email == ADMIN_EMAIL
            and check_password_hash(ADMIN_PASSWORD_HASH, password)
        ):
            session.clear()
            session["admin_authed"] = True
            session["admin_email"] = email
            log_event("admin_signed_in", actor="admin", payload={"email": email})
            return redirect(request.args.get("next") or url_for("admin_static_dashboard"))
        log.warning("Failed admin login attempt for email=%r from %s", email, request.remote_addr)
        # Kept alongside the log line: the log rotates, the trail does not, and
        # a run of these against one IP is the thing worth being able to look up.
        log_event("admin_sign_in_failed", actor="admin", payload={"email": email})
        error = "Invalid email or password."

    return render_template("login.html", error=error), (401 if error else 200)

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))

# Admin dashboard pages
@app.route("/admin")
@admin_required
def admin_static_dashboard():
    return send_from_directory(str(APP_DIR / "public"), "dashboard.html")

@app.route("/admin/reps")
@admin_required
def admin_rep_links():
    return send_from_directory(str(APP_DIR / "public"), "rep-links.html")

# Cache-control: discourage going back to a stale form after Thank You
@app.after_request
def add_no_store_headers(resp):
    try:
        # JSON as well as HTML: the admin APIs return per-request state, and
        # the activity page polls one of them every 30 seconds. With no cache
        # headers at all a browser may serve a heuristically cached response,
        # which shows up as a dashboard that has quietly stopped moving.
        if resp.mimetype in ("text/html", "application/json"):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
    except Exception:
        pass
    return resp

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=True)
