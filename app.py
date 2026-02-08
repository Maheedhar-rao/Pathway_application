#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Public form → Thank You (optional uploads). Separate admin dashboard.
JSON APIs power the dashboard. Stores data in Supabase (REST), no DB URL.
Each sales rep gets a unique link that tracks their submissions.
"""
from __future__ import annotations

import hashlib
import hmac
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
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        Image, HRFlowable, BaseDocTemplate, Frame, PageTemplate
    )
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
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
    "yuly": {"name": "Yuly", "email": "deals@pathwaycatalyst.com"},
    "tom": {"name": "Tom", "email": "tom@pathwaycatalyst.com"},
    "troy": {"name": "Troy", "email": "troy@pathwaycatalyst.com"},
    "adrian": {"name": "Adrian", "email": "adrian@pathwaycatalyst.com"},
    "frank": {"name": "Frank", "email": "frank@pathwaycatalyst.com"},
    "andres": {"name": "Andres", "email": "andres@pathwaycatalyst.com"}
}

def get_rep_info(rep_code: str) -> Optional[dict]:
    """Get rep info by code, case-insensitive."""
    if not rep_code:
        return None
    return SALES_REPS.get(rep_code.lower().strip())

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
    elements.append(Paragraph(f"<b>Submitted:</b> {datetime.now().strftime('%B %d, %Y at %I:%M %p')}", meta_style))
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
        ["Phone", form_data.get("business_phone", "")],
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
        ["Email", form_data.get("owner_0_email", "")],
        ["Mobile", form_data.get("owner_0_mobile", "")],
        ["FICO Score", form_data.get("owner_0_fico", "N/A")],
        ["MCA Balances", form_data.get("owner_0_mca_balances", "N/A")],
    ]
    elements.append(_styled_section_table(owner_data))

    # Owner home address
    owner_addr = f"{form_data.get('owner_0_addr1', '')} {form_data.get('owner_0_addr2', '')}".strip()
    owner_city_state = f"{form_data.get('owner_0_city', '')}, {form_data.get('owner_0_state', '')} {form_data.get('owner_0_zip', '')}"
    if owner_addr:
        elements.append(Paragraph("Owner Home Address", section_style))
        elements.append(_styled_section_table([
            ["Street", owner_addr],
            ["City / State / ZIP", owner_city_state],
        ]))

    # ── Second Owner (if present) ──
    if form_data.get("has_owner_1") == "Yes":
        elements.append(Paragraph("Second Owner", section_style))
        owner2_data = [
            ["Name", f"{form_data.get('owner_1_first', '')} {form_data.get('owner_1_last', '')}"],
            ["Ownership %", f"{form_data.get('owner_1_pct', '')}%"],
            ["Date of Birth", form_data.get("owner_1_dob", "")],
            ["SSN", form_data.get("owner_1_ssn", "")],
            ["Email", form_data.get("owner_1_email", "")],
            ["Mobile", form_data.get("owner_1_mobile", "")],
            ["FICO Score", form_data.get("owner_1_fico", "N/A")],
            ["MCA Balances", form_data.get("owner_1_mca_balances", "N/A")],
        ]
        elements.append(_styled_section_table(owner2_data))

    # ── Property Information ──
    elements.append(Paragraph("Property &amp; Location", section_style))
    prop_data = [
        ["Owns Real Estate", form_data.get("own_real_estate", "")],
        ["Own Home Location", form_data.get("own_home_location", "")],
        ["Own Business Location", form_data.get("own_business_location", "")],
        ["Residence Tenure", form_data.get("residence_tenure", "N/A")],
        ["Business Location Tenure", form_data.get("business_location_tenure", "N/A")],
    ]
    elements.append(_styled_section_table(prop_data))

    # ── Signature & Authorization ──
    elements.append(Paragraph("Authorization &amp; Signature", section_style))
    elements.append(Paragraph(
        "The applicant has agreed to electronic records and signatures as permitted under applicable law.",
        ParagraphStyle('AuthText', parent=styles['Normal'], fontSize=9, textColor=BRAND_GRAY, spaceAfter=10)
    ))

    sig_info = [
        ["Print Name", form_data.get("signature_print_name", "")],
        ["Date Signed", form_data.get("signature_date", "")],
    ]
    elements.append(_styled_section_table(sig_info))

    # Render hand signature image
    sig_data = form_data.get("signature_data", "")
    if sig_data and sig_data.startswith("data:image/png;base64,"):
        import base64 as _b64
        raw = _b64.b64decode(sig_data.split(",", 1)[1])
        sig_buf = BytesIO(raw)
        sig_img = Image(sig_buf, width=3.2*inch, height=1.2*inch)
        sig_img.hAlign = 'LEFT'
        elements.append(Spacer(1, 8))
        elements.append(sig_img)
        elements.append(HRFlowable(width="50%", thickness=0.5, color=BRAND_DARK, spaceAfter=4))
        elements.append(Paragraph("Applicant Signature", ParagraphStyle(
            'SigLabel', parent=styles['Normal'], fontSize=9, textColor=BRAND_GRAY
        )))

    doc.build(elements)
    buffer.seek(0)
    return buffer


def send_email_with_pdf(
    to_emails: List[str],
    business_name: str,
    pdf_buffer: BytesIO,
    submission_id: int,
    rep_name: str = None,
    attached_files: List[Path] = None
):
    """Send email with PDF attachment and uploaded documents to specified recipients."""
    if not EMAIL_ENABLED:
        print(f"Email disabled. Would send to {', '.join(to_emails)}")
        return False

    if not to_emails:
        return False

    try:
        msg = MIMEMultipart('mixed')
        msg['From'] = EMAIL_FROM
        msg['To'] = ', '.join(to_emails)
        msg['Subject'] = f"New Application: {business_name} (ID: {submission_id})"

        # Email body
        rep_line = f"Referred by: {rep_name}" if rep_name else "Direct submission (no rep)"
        doc_count = len(attached_files) if attached_files else 0
        submitted = datetime.now().strftime('%B %d, %Y at %I:%M %p')

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
                  <p style="margin:0;font-size:15px;font-weight:600;color:#1e40af;">New Loan Application Received</p>
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
              <tr>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#64748b;font-size:13px;">Representative</td>
                <td style="padding:8px 0;border-bottom:1px solid #e2e8f0;color:#1e293b;font-size:14px;">{rep_line}</td>
              </tr>
              <tr>
                <td style="padding:8px 0;color:#64748b;font-size:13px;">Attachments</td>
                <td style="padding:8px 0;color:#1e293b;font-size:14px;">Application PDF + {doc_count} bank statement(s)</td>
              </tr>
            </table>

            <p style="color:#475569;font-size:14px;line-height:1.6;margin:0 0 20px;">
              Please find the complete application summary and uploaded bank statements attached to this email.
              You can also view full details in the admin dashboard.
            </p>

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

        # Attach both HTML and plain-text fallback
        alt_part = MIMEMultipart('alternative')
        plain_text = f"Congratulations! New Application Received\n\nBusiness: {business_name}\nApplication ID: {submission_id}\nSubmitted: {submitted}\n{rep_line}\n\nAttachments: Application PDF + {doc_count} bank statement(s)\n\nPowered by CROC"
        alt_part.attach(MIMEText(plain_text, 'plain'))
        alt_part.attach(MIMEText(html_body, 'html'))
        msg.attach(alt_part)

        # Attach PDF summary
        if pdf_buffer:
            pdf_buffer.seek(0)
            pdf_attachment = MIMEApplication(pdf_buffer.read(), _subtype='pdf')
            pdf_attachment.add_header('Content-Disposition', 'attachment', filename=f'application_{submission_id}.pdf')
            msg.attach(pdf_attachment)

        # Attach uploaded documents (bank statements, etc.)
        if attached_files:
            for file_path in attached_files:
                if file_path.exists():
                    with open(file_path, 'rb') as fp:
                        file_attachment = MIMEApplication(fp.read(), _subtype='pdf')
                        file_attachment.add_header(
                            'Content-Disposition', 'attachment',
                            filename=file_path.name
                        )
                        msg.attach(file_attachment)

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
        'esign_consent',
        'signature_data','signature_date','signature_print_name',
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
    rep_sig = sign_rep_code(rep_code) if rep_code else ""
    return render_template("form.html", rep_code=rep_code, rep_info=rep_info, rep_sig=rep_sig)

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

    # Get rep info from hidden field — verify HMAC to prevent tampering
    rep_code = form.get("rep_code", "").strip()
    rep_sig = form.get("rep_sig", "").strip()
    if rep_code and not verify_rep_code(rep_code, rep_sig):
        rep_code = ""  # reject tampered rep code
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
        rep_sig = sign_rep_code(rep_code) if rep_code else ""
        return render_template("form.html", errors=errors, form=form, rep_code=rep_code, rep_info=rep_info, rep_sig=rep_sig), 400

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
    saved_files: List[Path] = []
    for f in request.files.getlist("bank_files"):
        if not f or not f.filename:
            continue
        safe = f.filename.replace("/", "_").replace("\\", "_")
        dest = UPLOAD_DIR / f"{submission_id}__bank_statement__{safe}"
        f.save(dest)
        saved_files.append(dest)
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
                    rep_name=rep_name,
                    attached_files=saved_files
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
