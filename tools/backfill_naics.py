"""
Backfill NAICS/SIC classification onto existing applications.

Run once after applying migration 20260813_add_naics_columns.sql. For every row
whose `naics` column is still NULL, this re-runs the shared classifier on the
stored business name + industry and writes the three match-key columns
(naics, sic, naics_bucket). It also patches payload["naics"] so the JSON blob and
the columns agree, exactly as a live submission would have stored them.

Reuses classify_naics() and the Supabase client from app.py, so the result is
identical to what intake produces -- same rules, same defensive fallbacks.

Idempotent: only touches rows where naics is NULL, so re-running resumes where a
previous run stopped and never reclassifies a row that already has a code. Rows
with no usable name/industry, or that the classifier can't place, are left NULL
and reported at the end.

Usage:
    python tools/backfill_naics.py --dry-run     # preview, writes nothing
    python tools/backfill_naics.py               # apply
    python tools/backfill_naics.py --limit 50    # cap rows processed (testing)
"""
import argparse
import os
import sys

# Allow `python tools/backfill_naics.py` from the repo root: put the project
# root (this file's parent's parent) on the path so `import app` resolves.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import sb, classify_naics

PAGE = 500


def _iter_unclassified(limit=None):
    """Yield applications where the naics column is still NULL, oldest first.

    Pages by an id cursor (id > last seen) rather than an offset, so it works
    whether or not rows get written between pages -- a plain `--dry-run` (which
    writes nothing) advances correctly instead of re-fetching the same batch.
    """
    last_id = 0
    fetched = 0
    while True:
        want = PAGE if limit is None else min(PAGE, limit - fetched)
        if want <= 0:
            return
        res = (
            sb.table("applications")
            .select("id, business_legal_name, industry, payload")
            .is_("naics", "null")
            .gt("id", last_id)
            .order("id")
            .limit(want)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return
        for row in rows:
            yield row
            last_id = row["id"]
        fetched += len(rows)
        if len(rows) < want:
            return


def _inputs(row):
    """Best-effort name + industry, falling back to the payload copy."""
    payload = row.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    name = (row.get("business_legal_name") or payload.get("business_legal_name") or "").strip()
    industry = (row.get("industry") or payload.get("industry") or "").strip()
    return name, industry, payload


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill NAICS/SIC onto applications.")
    ap.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    ap.add_argument("--limit", type=int, default=None, help="Max rows to process.")
    args = ap.parse_args()

    updated = skipped_noinput = skipped_noresult = errors = 0

    for row in _iter_unclassified(limit=args.limit):
        rid = row["id"]
        name, industry, payload = _inputs(row)

        if not name and not industry:
            skipped_noinput += 1
            print(f"  id={rid}: SKIP (no name/industry)")
            continue

        info = classify_naics(name, industry)
        if not info:
            skipped_noresult += 1
            print(f"  id={rid}: unclassified {name!r} / {industry!r}")
            continue

        cols = {
            "naics": info.get("naics"),
            "sic": info.get("sic"),
            "naics_bucket": info.get("bucket"),
        }
        if args.dry_run:
            print(f"  id={rid}: would set {cols['naics']} / SIC {cols['sic']} / {cols['naics_bucket']}  ({name})")
            updated += 1
            continue

        try:
            # Keep the JSON blob consistent with the new columns.
            new_payload = dict(payload)
            new_payload["naics"] = info
            sb.table("applications").update({**cols, "payload": new_payload}).eq("id", rid).execute()
            updated += 1
            print(f"  id={rid}: {cols['naics']} / SIC {cols['sic']} / {cols['naics_bucket']}  ({name})")
        except Exception as e:  # never let one bad row abort the whole run
            errors += 1
            print(f"  id={rid}: ERROR {e}", file=sys.stderr)

    verb = "would update" if args.dry_run else "updated"
    print(
        f"\nDone. {verb}={updated}  "
        f"unclassified={skipped_noresult}  no-input={skipped_noinput}  errors={errors}"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
