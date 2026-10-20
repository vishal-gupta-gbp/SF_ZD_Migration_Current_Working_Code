"""Reconcile and backfill attachments for already-migrated Zendesk tickets.

Usage:
  # Check/reconcile a specific Zendesk ticket:
  .venv/bin/python reconcile_migrated_attachments.py --ticket-id 217842

  # Check/reconcile multiple Zendesk tickets:
  .venv/bin/python reconcile_migrated_attachments.py --ticket-ids 217842,215464

  # Check/reconcile by Salesforce Case Number:
  .venv/bin/python reconcile_migrated_attachments.py --case-number 03028068

  # Dry-run list only (shows which tickets have pending attachments):
  .venv/bin/python reconcile_migrated_attachments.py --dry-run --limit 20

  # Reconcile up to N tickets with pending attachments:
  .venv/bin/python reconcile_migrated_attachments.py --limit 50
"""
from __future__ import annotations

import argparse
import hashlib
import mimetypes
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from sqlalchemy import create_engine, text

from migration.clients.s3 import S3Client, parse_s3_url
from migration.clients.zendesk import ZendeskClient
from migration.config import load_config
from migration.engine.field_mapper import s


def download_attachment_file(
    s3_client: S3Client,
    att: Dict[str, Any],
    fallback_url: Optional[str],
    target_path: Path,
) -> bool:
    """Download attachment from S3 (with candidate folder resolution) or Salesforce ContentVersion fallback."""
    if target_path.exists() and target_path.stat().st_size > 0:
        return True

    target_path.parent.mkdir(parents=True, exist_ok=True)
    url = s(att.get("migration_s3_url")) or s(att.get("original_s3_url"))

    if url:
        try:
            s3_client.download_file(url, target_path)
            if target_path.exists() and target_path.stat().st_size > 0:
                return True
        except Exception as exc:
            print(f"    S3 download error for {url}: {exc}")

    # Fallback to Salesforce ContentVersion HTTP download
    if fallback_url:
        try:
            print(f"    Attempting download from Salesforce fallback URL...")
            resp = requests.get(fallback_url, timeout=30, stream=True)
            if resp.status_code == 200:
                with open(target_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
                if target_path.exists() and target_path.stat().st_size > 0:
                    return True
            else:
                print(f"    Fallback URL returned HTTP {resp.status_code}")
        except Exception as exc:
            print(f"    Fallback download failed: {exc}")

    return False


def reconcile_ticket(
    zd_client: ZendeskClient,
    s3_client: S3Client,
    engine: Any,
    workspace_dir: Path,
    zendesk_ticket_id: int,
    case_id: str,
    case_number: str,
    dry_run: bool = False,
) -> bool:
    print(f"\nProcessing Zendesk #{zendesk_ticket_id} (SF Case: {case_number} / {case_id})...")

    with engine.connect() as conn:
        atts = conn.execute(
            text("""
                SELECT ma.*, s3r.bp_fallback_url
                FROM migration_attachments ma
                LEFT JOIN sf_s3_records s3r ON s3r.id = ma.s3_record_id
                WHERE ma.case_id = :cid
                ORDER BY ma.id
            """),
            {"cid": case_id},
        ).mappings().fetchall()

    if not atts:
        print("  No attachments found for this case in migration_attachments.")
        return False

    pending_atts = [a for a in atts if a["upload_status"] != "success"]
    print(f"  Total attachments: {len(atts)}; Pending: {len(pending_atts)}")

    if not pending_atts:
        print("  All attachments already marked success!")
        return True

    if dry_run:
        for a in pending_atts:
            print(f"    [DRY-RUN] Would process: {a['file_name']} (URL: {a['migration_s3_url']})")
        return True

    upload_tokens = []
    corrected_urls = []
    success_att_ids = []

    for a in pending_atts:
        fname = a["file_name"] or f"attachment_{a['id']}"
        local_rel = a["local_file_path"] or f"attachments/files/{case_id}/{fname}"
        local_full = (workspace_dir / local_rel).resolve()

        # Download if not already present
        ok = download_attachment_file(s3_client, a, a.get("bp_fallback_url"), local_full)
        if not ok:
            print(f"    FAILED to acquire file for {fname} from S3 and fallback.")
            continue

        # Upload to Zendesk
        try:
            token = zd_client.upload_file(local_full, fname, a["content_type"])
            print(f"    Uploaded {fname} to Zendesk -> token: {token}")
            upload_tokens.append(token)
            corrected_urls.append(a["migration_s3_url"] or a["original_s3_url"])
            success_att_ids.append((a["id"], token, str(local_full.relative_to(workspace_dir))))
        except Exception as exc:
            print(f"    Failed to upload {fname} to Zendesk: {exc}")

    if not upload_tokens:
        print("  No attachments could be uploaded.")
        return False

    # Post internal note to Zendesk
    body_text = (
        "Salesforce S3 Attachments (Resolved)\n\n"
        "The attachments for this case have been successfully retrieved and attached:\n"
        + "\n".join(f"- {u}" for u in corrected_urls)
    )

    payload = {
        "ticket": {
            "comment": {
                "public": False,
                "body": body_text,
                "uploads": upload_tokens,
            }
        }
    }

    resp = zd_client.request("PUT", f"/api/v2/tickets/{zendesk_ticket_id}.json", json_body=payload)
    if resp.status_code in (200, 201):
        print(f"  Successfully attached {len(upload_tokens)} files to Zendesk Ticket #{zendesk_ticket_id}!")
        # Update DB
        with engine.begin() as conn:
            for att_id, tok, lpath in success_att_ids:
                conn.execute(
                    text("""
                        UPDATE migration_attachments 
                        SET upload_status = 'success', 
                            download_status = 'success',
                            upload_token = :tok, 
                            local_file_path = :lpath,
                            download_error = '',
                            updated_at = NOW() 
                        WHERE id = :id
                    """),
                    {"tok": tok, "lpath": lpath, "id": att_id},
                )
            conn.execute(
                text("""
                    UPDATE migration_tickets 
                    SET status = 'completed', 
                        audit_json = (COALESCE(audit_json::jsonb, '{}'::jsonb) || '{"attachments_resolved": true}'::jsonb)::json,
                        updated_at = NOW() 
                    WHERE zendesk_ticket_id = :tid
                """),
                {"tid": zendesk_ticket_id},
            )
        return True
    else:
        print(f"  Zendesk ticket update failed (HTTP {resp.status_code}): {resp.text[:500]}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Reconcile and backfill attachments for migrated Zendesk tickets.")
    parser.add_argument("--ticket-id", type=int, help="Single Zendesk ticket ID to reconcile")
    parser.add_argument("--ticket-ids", type=str, help="Comma-separated list of Zendesk ticket IDs")
    parser.add_argument("--case-number", type=str, help="Salesforce case number to reconcile")
    parser.add_argument("--limit", type=int, default=10, help="Max tickets to process if no specific ID given (default: 10)")
    parser.add_argument("--dry-run", action="store_true", help="Print affected tickets without modifying Zendesk")
    args = parser.parse_args()

    cfg = load_config()
    engine = create_engine(cfg.database_url)
    s3 = S3Client(
        access_key_id=cfg.aws.access_key_id,
        secret_access_key=cfg.aws.secret_access_key,
        region=cfg.aws.region,
    )
    zd = ZendeskClient(
        subdomain=cfg.zendesk.subdomain,
        client_id=cfg.zendesk.client_id,
        client_secret=cfg.zendesk.client_secret,
        scope=cfg.zendesk.scope,
    )
    zd.authenticate()

    with engine.connect() as conn:
        if args.ticket_id:
            query = text("SELECT zendesk_ticket_id, case_id, case_number FROM migration_tickets WHERE zendesk_ticket_id = :tid")
            params = {"tid": args.ticket_id}
            tickets = conn.execute(query, params).mappings().fetchall()
        elif args.ticket_ids:
            tids = [int(x.strip()) for x in args.ticket_ids.split(",") if x.strip()]
            query = text("SELECT zendesk_ticket_id, case_id, case_number FROM migration_tickets WHERE zendesk_ticket_id = ANY(:tids)")
            params = {"tids": tids}
            tickets = conn.execute(query, params).mappings().fetchall()
        elif args.case_number:
            query = text("SELECT zendesk_ticket_id, case_id, case_number FROM migration_tickets WHERE case_number = :cnum")
            params = {"cnum": args.case_number}
            tickets = conn.execute(query, params).mappings().fetchall()
        else:
            query = text("""
                SELECT mt.zendesk_ticket_id, mt.case_id, mt.case_number, count(ma.id) as pending_cnt
                FROM migration_tickets mt
                JOIN migration_attachments ma ON ma.case_id = mt.case_id
                WHERE mt.zendesk_ticket_id IS NOT NULL
                  AND ma.upload_status != 'success'
                GROUP BY mt.zendesk_ticket_id, mt.case_id, mt.case_number
                ORDER BY mt.zendesk_ticket_id
                LIMIT :lim
            """)
            params = {"lim": args.limit}
            tickets = conn.execute(query, params).mappings().fetchall()

    print(f"Found {len(tickets)} tickets matching criteria.")
    if not tickets:
        return

    success_cnt = 0
    for t in tickets:
        ok = reconcile_ticket(
            zd_client=zd,
            s3_client=s3,
            engine=engine,
            workspace_dir=cfg.workspace_dir,
            zendesk_ticket_id=t["zendesk_ticket_id"],
            case_id=t["case_id"],
            case_number=t["case_number"],
            dry_run=args.dry_run,
        )
        if ok:
            success_cnt += 1

    print(f"\nDone. Successfully reconciled {success_cnt}/{len(tickets)} tickets.")


if __name__ == "__main__":
    main()
