#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Public form → Thank You (optional uploads). Separate admin dashboard.
JSON APIs power the dashboard. Stores data in Supabase (REST), no DB URL.
Each sales rep gets a unique link that tracks their submissions.
"""
from __future__ import annotations

import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import List, Optional
from urllib.parse import quote

from flask import (
    Flask, request, redirect, url_for, render_template, jsonify,
    send_from_directory, abort
)
from supabase import create_client, Client
from dotenv import load_dotenv, find_dotenv

# PDF generation
try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    PDF_ENABLED = True
except ImportError:
    PDF_ENABLED = False
    print("Warning: reportlab not installed. PDF generation disabled. Run: pip install reportlab")

load_dotenv(find_dotenv())

APP_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

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

# Main team email - receives ALL submissions
TEAM_EMAIL = os.environ.get("TEAM_EMAIL", "team@pathwaycatalyst.com")

# ---- Sales Rep Configuration ------------------------------------------------
# Each rep has a unique code, name, and email
# URL format: /?rep=<code>  e.g., /?rep=john
SALES_REPS = {
    "john": {"name": "John Smith", "email": "john@example.com"},
    "sarah": {"name": "Sarah Johnson", "email": "sarah@example.com"},
    "mike": {"name": "Mike Williams", "email": "mike@example.com"},
    "emily": {"name": "Emily Davis", "email": "emily@example.com"},
    "david": {"name": "David Brown", "email": "david@example.com"},
    "lisa": {"name": "Lisa Miller", "email": "lisa@example.com"},
    "james": {"name": "James Wilson", "email": "james@example.com"},
    "anna": {"name": "Anna Taylor", "email": "anna@example.com"},
    "chris": {"name": "Chris Anderson", "email": "chris@example.com"},
    "kate": {"name": "Kate Thomas", "email": "kate@example.com"},
    "yuly": {"name": "Yuly", "email": "tech@pathwaycatalyst.com"},
    "tom": {"name": "Tom", "email": "tom@pathwaycatalyst.com"},
}

def get_rep_info(rep_code: str) -> Optional[dict]:
    """Get rep info by code, case-insensitive."""
    if not rep_code:
        return None
    return SALES_REPS.get(rep_code.lower().strip())

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("APP_SECRET", "dev-secret")

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

def generate_application_pdf(form_data: dict, submission_id: int, rep_name: str = None) -> BytesIO:
    """Generate a PDF summary of the application."""
    if not PDF_ENABLED:
        return None

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.5*inch, bottomMargin=0.5*inch)
    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=18, spaceAfter=20, textColor=colors.HexColor('#1e40af'))
    section_style = ParagraphStyle('Section', parent=styles['Heading2'], fontSize=14, spaceBefore=15, spaceAfter=10, textColor=colors.HexColor('#1e40af'))
    normal_style = styles['Normal']

    elements = []

    # Header
    elements.append(Paragraph("Pathway Catalyst - Loan Application", title_style))
    elements.append(Paragraph(f"Application ID: {submission_id}", normal_style))
    elements.append(Paragraph(f"Submitted: {datetime.now().strftime('%B %d, %Y at %I:%M %p')}", normal_style))
    if rep_name:
        elements.append(Paragraph(f"Sales Representative: {rep_name}", normal_style))
    elements.append(Spacer(1, 20))

    # Business Information
    elements.append(Paragraph("Business Information", section_style))
    biz_data = [
        ["Business Legal Name:", form_data.get("business_legal_name", "")],
        ["DBA Name:", form_data.get("business_dba", "")],
        ["Industry:", form_data.get("industry", "")],
        ["Legal Entity:", form_data.get("legal_entity", "")],
        ["Business Start Date:", form_data.get("business_start_date", "")],
        ["EIN:", form_data.get("ein", "")],
        ["Website:", form_data.get("company_website", "")],
        ["Phone:", form_data.get("business_phone", "")],
        ["Loan Amount:", f"${form_data.get('loan_amount', '0'):,}" if form_data.get('loan_amount') else ""],
        ["Loan Purpose:", form_data.get("loan_purpose", "")],
    ]
    biz_table = Table(biz_data, colWidths=[2*inch, 4.5*inch])
    biz_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    elements.append(biz_table)

    # Company Address
    elements.append(Paragraph("Company Address", section_style))
    addr = f"{form_data.get('company_address1', '')} {form_data.get('company_address2', '')}".strip()
    city_state = f"{form_data.get('company_city', '')}, {form_data.get('company_state', '')} {form_data.get('company_zip', '')}"
    elements.append(Paragraph(addr, normal_style))
    elements.append(Paragraph(city_state, normal_style))
    elements.append(Spacer(1, 10))

    # Owner Information
    elements.append(Paragraph("Primary Owner", section_style))
    owner_data = [
        ["Name:", f"{form_data.get('owner_0_first', '')} {form_data.get('owner_0_last', '')}"],
        ["Ownership %:", f"{form_data.get('owner_0_pct', '')}%"],
        ["DOB:", form_data.get("owner_0_dob", "")],
        ["SSN:", form_data.get("owner_0_ssn", "")],
        ["Email:", form_data.get("owner_0_email", "")],
        ["Mobile:", form_data.get("owner_0_mobile", "")],
        ["FICO:", form_data.get("owner_0_fico", "N/A")],
    ]
    owner_table = Table(owner_data, colWidths=[2*inch, 4.5*inch])
    owner_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(owner_table)

    # Second Owner (if present)
    if form_data.get("has_owner_1") == "Yes":
        elements.append(Paragraph("Second Owner", section_style))
        owner2_data = [
            ["Name:", f"{form_data.get('owner_1_first', '')} {form_data.get('owner_1_last', '')}"],
            ["Ownership %:", f"{form_data.get('owner_1_pct', '')}%"],
            ["DOB:", form_data.get("owner_1_dob", "")],
            ["SSN:", form_data.get("owner_1_ssn", "")],
            ["Email:", form_data.get("owner_1_email", "")],
            ["Mobile:", form_data.get("owner_1_mobile", "")],
        ]
        owner2_table = Table(owner2_data, colWidths=[2*inch, 4.5*inch])
        owner2_table.setStyle(TableStyle([
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        elements.append(owner2_table)

    # Property Information
    elements.append(Paragraph("Property Information", section_style))
    prop_data = [
        ["Owns Real Estate:", form_data.get("own_real_estate", "")],
        ["Own Home Location:", form_data.get("own_home_location", "")],
        ["Own Business Location:", form_data.get("own_business_location", "")],
        ["Residence:", form_data.get("residence_tenure", "N/A")],
        ["Business Location:", form_data.get("business_location_tenure", "N/A")],
    ]
    prop_table = Table(prop_data, colWidths=[2*inch, 4.5*inch])
    prop_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(prop_table)

    # E-Sign Consent
    elements.append(Spacer(1, 20))
    elements.append(Paragraph("Electronic Signature Consent: Yes", normal_style))

    doc.build(elements)
    buffer.seek(0)
    return buffer


def send_email_with_pdf(
    to_emails: List[str],
    business_name: str,
    pdf_buffer: BytesIO,
    submission_id: int,
    rep_name: str = None
):
    """Send email with PDF attachment to specified recipients."""
    if not EMAIL_ENABLED:
        print(f"Email disabled. Would send to {', '.join(to_emails)}")
        return False

    if not to_emails:
        return False

    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_FROM
        msg['To'] = ', '.join(to_emails)
        msg['Subject'] = f"New Application: {business_name} (ID: {submission_id})"

        # Email body
        rep_line = f"\nReferred by: {rep_name}" if rep_name else "\nDirect submission (no rep)"
        body = f"""
New Loan Application Received

Business: {business_name}
Application ID: {submission_id}
Submitted: {datetime.now().strftime('%B %d, %Y at %I:%M %p')}{rep_line}

Please find the application summary attached as a PDF.

You can view the full details in the admin dashboard.

Best regards,
Pathway Catalyst System
        """
        msg.attach(MIMEText(body, 'plain'))

        # Attach PDF - need to read and reset buffer for multiple sends
        if pdf_buffer:
            pdf_buffer.seek(0)
            pdf_attachment = MIMEApplication(pdf_buffer.read(), _subtype='pdf')
            pdf_attachment.add_header('Content-Disposition', 'attachment', filename=f'application_{submission_id}.pdf')
            msg.attach(pdf_attachment)

        # Send email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)

        print(f"Email sent to {', '.join(to_emails)}")
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False


def validate_fields(form: dict) -> dict:
    errors = {}

    # Base required fields
    req = [
        'business_legal_name','industry','legal_entity','business_start_date','ein',
        'company_address1','company_city','company_state','company_zip',
        'owner_0_first','owner_0_last','owner_0_pct','owner_0_dob','owner_0_ssn','owner_0_email','owner_0_mobile',
        'own_real_estate','own_home_location','own_business_location',
        # New required field from PDF concept
        'esign_consent',
    ]
    for k in req:
        if not form.get(k):
            errors[k] = 'Required'

    # Real estate conditional fields
    if form.get('own_real_estate') == 'Yes':
        for k in ['residence_tenure', 'business_location_tenure']:
            if not form.get(k):
                errors[k] = 'Required'

    # Second owner conditional required fields
    has_owner_1 = (form.get('has_owner_1') or 'No').strip()
    if has_owner_1 == 'Yes':
        owner1_req = [
            'owner_1_first','owner_1_last','owner_1_pct','owner_1_dob','owner_1_ssn',
            'owner_1_email','owner_1_mobile',
            'owner_1_addr1','owner_1_city','owner_1_state','owner_1_zip'
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

    if form.get('company_state') and not STATE_RE.match(form['company_state']):
        errors['company_state'] = 'Use 2-letter state'

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
        if form.get('owner_1_state') and not STATE_RE.match(form['owner_1_state']):
            errors['owner_1_state'] = 'Use 2-letter state'

    # E-sign consent must be explicitly "Yes"
    if form.get('esign_consent') and form.get('esign_consent') != 'Yes':
        errors['esign_consent'] = 'Consent is required'

    return errors

# -------------------- Public Pages --------------------
@app.route("/")
def home():
    rep_code = request.args.get("rep", "").strip()
    rep_info = get_rep_info(rep_code)
    return render_template("form.html", rep_code=rep_code, rep_info=rep_info)

@app.route("/thank-you")
def thank_you():
    sid = request.args.get("sid", type=int)
    business = None
    if sid:
        res = sb.table("applications").select("business_legal_name").eq("id", sid).limit(1).execute()
        rows = res.data or []
        if rows:
            business = rows[0].get("business_legal_name")
    return render_template("thank_you.html", sid=sid, business=business)

# Serve uploaded files inline (so PDFs preview)
@app.route("/uploads/<path:path>")
def uploaded_file(path: str):
    return send_from_directory(UPLOAD_DIR, path, as_attachment=False)

# -------------------- Submission Endpoints --------------------
@app.route("/submit", methods=["POST"])
def submit():
    # Normalize request.form into a clean dict
    form = {k: (v.strip() if isinstance(v, str) else v) for k, v in request.form.items()}

    # Get rep info from hidden field
    rep_code = form.get("rep_code", "").strip()
    rep_info = get_rep_info(rep_code)

    # Enforce default for has_owner_1 if not present
    if "has_owner_1" not in form or not form.get("has_owner_1"):
        form["has_owner_1"] = "No"

    errors = validate_fields(form)

    # Validate bank statement upload (required)
    bank_files = request.files.getlist("bank_files")
    has_bank_file = any(f and f.filename for f in bank_files)
    if not has_bank_file:
        errors['bank_files'] = 'At least one bank statement is required'

    if errors:
        return render_template("form.html", errors=errors, form=form, rep_code=rep_code, rep_info=rep_info), 400

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

    # Insert into Supabase
    db_payload = {
        "business_legal_name": business_legal_name,
        "industry": industry,
        "loan_amount": loan_amount,
        "owners": owners,              # jsonb
        "payload": form,               # jsonb
        "ein": form.get("ein"),
        "business_phone": form.get("business_phone"),
        "company_website": form.get("company_website"),
    }

    # Add rep info if available
    if rep_info:
        db_payload["rep_name"] = rep_info["name"]
        db_payload["rep_email"] = rep_info["email"]

    ins = sb.table("applications").insert(db_payload).execute()
    if not ins.data:
        abort(500, description="Insert failed")
    submission_id = ins.data[0]["id"]

    # Save initial bank statements locally + metadata to Supabase
    for f in request.files.getlist("bank_files"):
        if not f or not f.filename:
            continue
        safe = f.filename.replace("/", "_").replace("\\", "_")
        dest = UPLOAD_DIR / f"{submission_id}__bank_statement__{safe}"
        f.save(dest)
        size = dest.stat().st_size
        sb.table("application_files").insert({
            "application_id": submission_id,
            "filename": safe,
            "storage_path": str(dest),
            "size_bytes": size,
            "doc_type": "bank_statement",
        }).execute()

    # Generate PDF and email to team + rep (if applicable)
    if PDF_ENABLED:
        try:
            rep_name = rep_info["name"] if rep_info else None
            pdf_buffer = generate_application_pdf(form, submission_id, rep_name)

            if pdf_buffer:
                # Build recipient list: always include team, add rep if exists
                recipients = [TEAM_EMAIL]
                if rep_info and rep_info["email"]:
                    recipients.append(rep_info["email"])

                send_email_with_pdf(
                    to_emails=recipients,
                    business_name=business_legal_name,
                    pdf_buffer=pdf_buffer,
                    submission_id=submission_id,
                    rep_name=rep_name
                )
        except Exception as e:
            print(f"Failed to generate/send PDF: {e}")

    return redirect(url_for("thank_you", sid=submission_id))

@app.route("/upload-docs", methods=["POST"])
def upload_docs():
    sid = request.form.get("sid", type=int)
    if not sid:
        abort(400)
    saved = []
    for field, dtype in [("voided_check", "voided_check"), ("id_doc", "id_doc")]:
        f = request.files.get(field)
        if f and f.filename:
            safe = f.filename.replace("/", "_").replace("\\", "_")
            dest = UPLOAD_DIR / f"{sid}__{dtype}__{safe}"
            f.save(dest)
            size = dest.stat().st_size
            sb.table("application_files").insert({
                "application_id": sid,
                "filename": safe,
                "storage_path": str(dest),
                "size_bytes": size,
                "doc_type": dtype,
            }).execute()
            saved.append(dtype)
    return render_template("thank_you.html", sid=sid, uploaded=saved)

# -------------------- JSON APIs for Dashboard --------------------
@app.route("/api/submissions")
def api_submissions():
    try:
        limit = int(request.args.get("limit", "100"))
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        limit, offset = 100, 0

    # Optional filter by rep
    rep_filter = request.args.get("rep", "").strip()

    start = offset
    end = offset + limit - 1

    query = sb.table("applications").select(
        "id, created_at, business_legal_name, industry, loan_amount, owners, payload, company_website, rep_name, rep_email"
    )

    if rep_filter:
        rep_info = get_rep_info(rep_filter)
        if rep_info:
            query = query.eq("rep_name", rep_info["name"])

    res = query.order("id", desc=True).range(start, end).execute()
    rows = res.data or []
    for r in rows:
        if r.get("loan_amount") is not None:
            r["loan_amount"] = float(r["loan_amount"])
    return jsonify(rows)

@app.route("/api/submissions/<int:sid>")
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
        path_name = Path(f["storage_path"]).name
        f["url"] = f"/uploads/{quote(path_name)}"

    app_row["files"] = files
    return jsonify(app_row)

@app.route("/api/reps")
def api_reps():
    """List all sales reps with their unique links."""
    base_url = request.host_url.rstrip("/")
    reps_list = []
    for code, info in SALES_REPS.items():
        reps_list.append({
            "code": code,
            "name": info["name"],
            "email": info["email"],
            "link": f"{base_url}/?rep={code}"
        })
    return jsonify(reps_list)

# Optional: serve static admin dashboard page
@app.route("/admin")
def admin_static_dashboard():
    return send_from_directory(str(APP_DIR / "public"), "dashboard.html")

@app.route("/admin/reps")
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
