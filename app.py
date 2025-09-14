#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Public form → Thank You (optional uploads). Separate admin dashboard.
JSON APIs power the dashboard. Stores data in Supabase (REST), no DB URL.
"""
from __future__ import annotations

import os
import re
import json
from datetime import datetime
from pathlib import Path
from typing import List
from urllib.parse import quote

from flask import (
    Flask, request, redirect, url_for, render_template, jsonify,
    send_from_directory, abort
)
from supabase import create_client, Client
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv()) 

APP_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# ---- Config (no DB URL needed) ----------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE = os.environ.get("SUPABASE_SERVICE_ROLE")
if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE:
    raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE in your environment.")

sb: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("APP_SECRET", "dev-secret")

# -------------------- Validation --------------------
SSN_RE = re.compile(r'^(?!000|666|9\d\d)(\d{3})-(?!00)(\d{2})-(?!0000)(\d{4})$')
EIN_RE = re.compile(r'^(?!00)\d{2}-\d{7}$')
PHONE_RE = re.compile(r'^\+?1?\s*\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}$')
ZIP_RE = re.compile(r'^\d{5}(-\d{4})?$')
STATE_RE = re.compile(r'^[A-Za-z]{2}$')

def validate_fields(form: dict) -> dict:
    errors = {}
    req = [
        'business_legal_name','industry','legal_entity','business_start_date','ein',
        'company_address1','company_city','company_state','company_zip',
        'owner_0_first','owner_0_last','owner_0_pct','owner_0_dob','owner_0_ssn','owner_0_email','owner_0_mobile',
        'own_real_estate'
    ]
    for k in req:
        if not form.get(k):
            errors[k] = 'Required'
    if form.get('own_real_estate') == 'Yes':
        for k in ['residence_tenure', 'business_location_tenure']:
            if not form.get(k):
                errors[k] = 'Required'

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
    return errors

# -------------------- Public Pages --------------------
@app.route("/")
def home():
    return render_template("form.html")

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
    form = {k: v.strip() if isinstance(v, str) else v for k, v in request.form.items()}
    errors = validate_fields(form)
    if errors:
        return render_template("form.html", errors=errors, form=form), 400

    business_legal_name = form.get("business_legal_name") or ""
    industry = form.get("industry") or ""
    try:
        loan_amount = float(form.get("loan_amount") or 0)
    except Exception:
        loan_amount = 0.0

    owners: List[str] = []
    first = form.get("owner_0_first", "").strip()
    last = form.get("owner_0_last", "").strip()
    if first or last:
        owners.append((first + " " + last).strip())

    # Insert into Supabase
    payload = {
        "business_legal_name": business_legal_name,
        "industry": industry,
        "loan_amount": loan_amount,
        "owners": owners,              # jsonb
        "payload": form,               # jsonb
        "ein": form.get("ein"),
        "business_phone": form.get("business_phone"),
    }
    ins = sb.table("applications").insert(payload).execute()
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
            "storage_path": str(dest),  # local path; you can switch to Supabase Storage later
            "size_bytes": size,
            "doc_type": "bank_statement",
        }).execute()

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

    # Supabase uses 0-based inclusive ranges
    start = offset
    end = offset + limit - 1
    res = (
        sb.table("applications")
        .select("id, created_at, business_legal_name, industry, loan_amount, owners, payload")
        .order("id", desc=True)
        .range(start, end)
        .execute()
    )
    rows = res.data or []
    # Ensure JSON serializable (loan_amount may already be numeric)
    for r in rows:
        if r.get("loan_amount") is not None:
            r["loan_amount"] = float(r["loan_amount"])
    return jsonify(rows)

@app.route("/api/submissions/<int:sid>")
def api_submission_detail(sid: int):
    app_res = sb.table("applications").select(
        "id, created_at, business_legal_name, industry, loan_amount, owners, payload"
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
    # Convert storage_path into previewable URL (served locally by /uploads)
    for f in files:
        path_name = Path(f["storage_path"]).name
        f["url"] = f"/uploads/{quote(path_name)}"

    app_row["files"] = files
    return jsonify(app_row)

# Optional: serve static admin dashboard page
@app.route("/admin")
def admin_static_dashboard():
    return send_from_directory(str(APP_DIR / "public"), "dashboard.html")

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
