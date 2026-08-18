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
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import datetime
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
    send_from_directory, send_file, abort, session
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash
from supabase import create_client, Client
from dotenv import load_dotenv, find_dotenv

# PDF generation
try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        Image, HRFlowable, BaseDocTemplate, Frame, PageTemplate
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

def current_brand_name() -> Optional[str]:
    """Brand for the host this request came in on, for pages with no slug.

    A merchant who started on application.croccrm.com stays on that host
    through /thank-you, so the name follows them. Path-slug brands can't be
    recovered here (the slug is only on the entry URL) and fall back to the
    default, which is what those links rendered before brands existed.
    """
    brand = get_brand_by_host(request.host) or get_default_brand()
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

    doc.build(elements)
    buffer.seek(0)
    return buffer


def _build_email_content(business_name, submission_id, rep_name, attached_files,
                         email_type="new_application", resume_url=None, pdf_url=None):
    """Build shared email HTML, plain text, and subject.

    `resume_url` is only rendered for applicant_receipt emails — it links the
    merchant back to the credit-setup page without re-filling the application.
    `pdf_url`: when set, the PDF was too large to attach and is instead provided
    as a signed download link embedded in the email body.
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
              <tr>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#64748b;font-size:13px;width:140px;">Business</td>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#1e293b;font-size:14px;font-weight:600;">{business_name}</td>
              </tr>
              <tr>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#64748b;font-size:13px;">Application ID</td>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#1e293b;font-size:14px;font-weight:600;">{submission_id}</td>
              </tr>
              <tr>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#64748b;font-size:13px;">Submitted</td>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#1e293b;font-size:14px;">{submitted}</td>
              </tr>
              {rep_row_html}
              <tr>
                <td style="padding:8px 0;color:#64748b;font-size:13px;">Attachments</td>
                <td style="padding:8px 0;color:#1e293b;font-size:14px;">{attachments_text}</td>
              </tr>
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
    plain_text = (
        f"{alert_text}\n\nBusiness: {business_name}\n"
        f"Application ID: {submission_id}\nSubmitted: {submitted}\n"
        f"{plain_rep_line}\nAttachments: {attachments_text}\n"
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
def _render_form(client_name=None):
    rep_code = request.args.get("rep", "").strip()
    rep_info = get_rep_info(rep_code)
    rep_sig = sign_rep_code(rep_code) if rep_code else ""
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
    return _render_form(client_name=current_brand_name())

@app.route("/<client_slug>")
def home_client(client_slug):
    # Branded per-client entry point, e.g. /pathway-catalyst?rep=tom. Inactive
    # brands still render so links already in reps' hands don't break; unknown
    # slugs 404 so this doesn't shadow real assets or typo'd URLs.
    brand = get_brand_by_slug(client_slug, include_inactive=True)
    if not brand:
        abort(404)
    return _render_form(client_name=brand["name"])

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

    if PDF_ENABLED:
        try:
            rep_name = rep_info["name"] if rep_info else None
            log.info("Generating PDF for submission %s (rep=%s)", submission_id, rep_name)
            pdf_buffer = generate_application_pdf(form, submission_id, rep_name)
            if pdf_buffer:
                pdf_buffer.seek(0)
                pdf_bytes = pdf_buffer.read()
                recipients = [TEAM_EMAIL]
                if rep_info and rep_info["email"]:
                    recipients.append(rep_info["email"])

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

    if saved_paths:
        try:
            app_res = sb.table("applications").select(
                "business_legal_name, rep_name, rep_email"
            ).eq("id", sid).execute()
            row = (app_res.data or [{}])[0]
            business_name = row.get("business_legal_name") or ""
            rep_name = row.get("rep_name")
            rep_email = row.get("rep_email")

            recipients = [TEAM_EMAIL]
            if rep_email:
                recipients.append(rep_email)

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

    # Process inline file uploads (bank statements / voided check / ID).
    # All optional per the new flow; failures are logged but don't block.
    _saved_types, saved_paths, _failed = _process_uploads(submission_id, request.files)
    if _failed:
        log.warning("Submission %s had upload failures: %s", submission_id, _failed)

    # Generate PDF and email to team + rep + applicant (background so user doesn't wait)
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

                recipients = [TEAM_EMAIL]
                if rep_info and rep_info["email"]:
                    recipients.append(rep_info["email"])

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


def _email_resume_link(sid: int, to_email: str, business_name: str = "") -> bool:
    """Send the merchant a fresh 30-day resume link. Returns True on success."""
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


@app.route("/resume")
def resume_application():
    """Validate a magic-link token; on success stash sid in the session and
    redirect to the standalone credit-setup page. On failure/expiry, show a
    page that lets the merchant request a fresh link by email."""
    token = request.args.get("token", "")
    sid, status = verify_resume_token(token)
    if status == "ok" and sid:
        session[SESSION_RESUME_KEY] = sid
        return redirect(url_for("credit_setup"))
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
        return redirect(url_for("credit_setup", done="1"))

    try:
        sb.table("applications").update({
            "idiq_username": username or None,
            "idiq_password_encrypted": encrypt_idiq_password(password) if password else None,
        }).eq("id", sid).execute()
    except Exception as exc:
        log.error("Failed to persist IDIQ creds via credit-setup for %s: %s", sid, exc)
        abort(500, description="Failed to save IDIQ credentials")

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
        return redirect(url_for("thank_you", sid=sid))

    try:
        sb.table("applications").update({
            "idiq_username": username or None,
            "idiq_password_encrypted": encrypt_idiq_password(password) if password else None,
        }).eq("id", sid).execute()
    except Exception as exc:
        log.error("Failed to persist IDIQ credentials for %s: %s", sid, exc)
        abort(500, description="Failed to save IDIQ credentials")

    return redirect(url_for("thank_you", sid=sid, done="1"))


@app.route("/upload-docs", methods=["POST"])
def upload_docs():
    sid = request.form.get("sid", type=int)
    if not sid:
        abort(400)

    saved, attached_paths, failed = _process_uploads(sid, request.files)

    # Email uploaded documents to team + rep (in background)
    if attached_paths:
        try:
            app_res = sb.table("applications").select(
                "business_legal_name, rep_name, rep_email"
            ).eq("id", sid).execute()
            row = (app_res.data or [{}])[0]
            business_name = row.get("business_legal_name") or ""
            rep_name = row.get("rep_name")
            rep_email = row.get("rep_email")

            recipients = [TEAM_EMAIL]
            if rep_email:
                recipients.append(rep_email)

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

    ok = _email_resume_link(sid, to_email, business_name=row.get("business_legal_name") or "")
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
            return redirect(request.args.get("next") or url_for("admin_static_dashboard"))
        log.warning("Failed admin login attempt for email=%r from %s", email, request.remote_addr)
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
        if resp.mimetype == "text/html":
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
    except Exception:
        pass
    return resp

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=True)
