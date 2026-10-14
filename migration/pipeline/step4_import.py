"""Step 4 & 5 - Import prepared tickets into Zendesk using Bulk Import API and database tracking."""
from __future__ import annotations

import mimetypes
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from migration.clients.zendesk import ZendeskBulkImportError, ZendeskClient
from migration.config import MigrationSettings
from migration.db.models import MigrationAttachment, MigrationAuditLog, MigrationTicket, SFCase
from migration.engine.field_mapper import s


def strip_private_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Clean internal tracking keys from payload before sending to Zendesk."""
    clean = dict(payload)
    for k in list(clean.keys()):
        if k.startswith("_"):
            clean.pop(k)

    if "comments" in clean and isinstance(clean["comments"], list):
        clean_comments = []
        for c in clean["comments"]:
            if isinstance(c, dict):
                cc = dict(c)
                for ck in list(cc.keys()):
                    if ck.startswith("_"):
                        cc.pop(ck)
                clean_comments.append(cc)
            else:
                clean_comments.append(c)
        clean["comments"] = clean_comments

    return clean


def run_step4_import(
    settings: MigrationSettings,
    session: Session,
    ticket_type: Optional[str] = None,  # "standard", "phone", or None for all
    archive_immediately: Optional[bool] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute Ticket Import in batches of up to 100 with runtime attachment linking and exact external_id verification."""
    label = f"{ticket_type.upper() if ticket_type else 'ALL'} TICKETS"
    print("=" * 68)
    print(f"IMPORTING {label} INTO ZENDESK (BULK IMPORT & DATABASE TRACKED)")
    if start_date or end_date:
        print(f"Date Scope: {start_date or 'ALL'} -> {end_date or 'ALL'}")
    print("=" * 68)

    archive = archive_immediately if archive_immediately is not None else settings.archive_immediately
    zd = ZendeskClient(
        subdomain=settings.zendesk.subdomain,
        client_id=settings.zendesk.client_id,
        client_secret=settings.zendesk.client_secret,
        scope=settings.zendesk.scope,
    )
    zd.authenticate()

    akarsh = zd.find_user_by_email(settings.agent_comment_email)
    if not akarsh:
        raise RuntimeError(f"Zendesk agent not found for {settings.agent_comment_email}.")
    akarsh_user_id = int(akarsh["id"])

    # Query tickets to import from DB (filtered by ticket_type and date range)
    query = session.query(MigrationTicket).filter(
        MigrationTicket.status.in_(["prepared", "importing", "failed"])
    )
    if ticket_type:
        query = query.filter_by(ticket_type=ticket_type)
    if start_date or end_date:
        query = query.join(SFCase, MigrationTicket.case_id == SFCase.id)
        if start_date:
            query = query.filter(SFCase.created_date >= f"{start_date}T00:00:00Z")
        if end_date:
            parsed_end = datetime.strptime(end_date, "%Y-%m-%d").date()
            next_day = parsed_end + timedelta(days=1)
            query = query.filter(SFCase.created_date < f"{next_day.isoformat()}T00:00:00Z")
    tickets = query.order_by(MigrationTicket.id.asc()).all()

    if not tickets:
        print(f"No {label.lower()} pending import in database.")
        completed_query = session.query(MigrationTicket).filter(
            MigrationTicket.status.in_(["completed", "existing", "completed_with_warnings"])
        )
        if ticket_type:
            completed_query = completed_query.filter_by(ticket_type=ticket_type)
        if start_date or end_date:
            completed_query = completed_query.join(SFCase, MigrationTicket.case_id == SFCase.id)
            if start_date:
                completed_query = completed_query.filter(SFCase.created_date >= f"{start_date}T00:00:00Z")
            if end_date:
                parsed_end = datetime.strptime(end_date, "%Y-%m-%d").date()
                next_day = parsed_end + timedelta(days=1)
        count = completed_query.count()
        return {"total": count, "already_completed": count, "completed": count, "completed_with_warnings": 0, "existing": count, "failed": 0, "manual_review_required": 0}

    print(f"Found {len(tickets):,} candidate tickets in database for {label}.", flush=True)

    # Pre-index attachments by case_id from DB
    attachments = session.query(MigrationAttachment).all()
    attachments_by_case: Dict[str, List[MigrationAttachment]] = {}
    for att in attachments:
        attachments_by_case.setdefault(att.case_id, []).append(att)

    bulk_enabled = True
    batch_size = settings.zd_bulk_import_batch_size

    def upload_attachment(att: MigrationAttachment) -> Optional[str]:
        if att.upload_token and att.upload_status == "success":
            return att.upload_token

        local_path = settings.workspace_dir / att.local_file_path if att.local_file_path else None
        if not local_path or not local_path.is_file():
            att.upload_status = "failed"
            att.upload_error = "Local file not found on disk"
            return None

        mime = att.content_type or mimetypes.guess_type(att.file_name)[0] or "application/octet-stream"
        try:
            token = zd.upload_file(local_path, att.file_name or local_path.name, content_type=mime)
            att.upload_token = token
            att.upload_status = "success"
            att.upload_error = ""
            return token
        except Exception as exc:
            att.upload_status = "failed"
            att.upload_error = str(exc)
            return None

    def link_attachments_to_payload(case_id: str, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], int, int]:
        case_atts = attachments_by_case.get(case_id, [])
        if not case_atts:
            return payload, 0, 0

        comments = payload.setdefault("comments", [])
        comment_by_sf_id = {
            s(c.get("_salesforce_email_message_id")): c
            for c in comments
            if isinstance(c, dict) and s(c.get("_salesforce_email_message_id"))
        }

        uploaded_count = 0
        failed_count = 0
        case_level_tokens: List[str] = []
        case_level_names: List[str] = []
        failed_att_urls: List[str] = []

        for att in case_atts:
            if att.download_status != "success":
                failed_count += 1
                if att.migration_s3_url:
                    failed_att_urls.append(att.migration_s3_url)
                continue

            token = upload_attachment(att)
            if token:
                uploaded_count += 1
                if att.comment_id and att.comment_id in comment_by_sf_id:
                    comment_by_sf_id[att.comment_id].setdefault("uploads", []).append(token)
                else:
                    case_level_tokens.append(token)
                    case_level_names.append(att.file_name or "Attachment")
            else:
                failed_count += 1
                if att.migration_s3_url:
                    failed_att_urls.append(att.migration_s3_url)

        if case_level_tokens:
            note_lines = ["Salesforce S3 Attachments", ""]
            if case_level_names:
                note_lines.append("Attachments:")
                note_lines.extend(f"- {name}" for name in case_level_names)
            comments.append({
                "author_id": int(akarsh_user_id),
                "public": False,
                "value": "\n".join(note_lines),
                "uploads": case_level_tokens,
            })

        if failed_att_urls:
            fail_lines = [
                "Salesforce S3 Attachment Notice",
                "",
                "The following S3 attachment(s) could not be downloaded/uploaded during migration:",
            ]
            for u in failed_att_urls:
                fail_lines.append(f"- {u}")
            comments.append({
                "author_id": int(akarsh_user_id),
                "public": False,
                "value": "\n".join(fail_lines),
            })

        return payload, uploaded_count, failed_count

    def process_bulk_batch(batch_items: List[Dict[str, Any]]) -> None:
        nonlocal bulk_enabled
        if not batch_items:
            return

        api_payloads = [item["clean_payload"] for item in batch_items]
        first_num = batch_items[0]["ticket_row"].case_number or batch_items[0]["ticket_row"].case_id
        last_num = batch_items[-1]["ticket_row"].case_number or batch_items[-1]["ticket_row"].case_id
        print(f"  [Bulk Import] Submitting batch of {len(batch_items)} tickets ({first_num} -> {last_num})...", flush=True)

        try:
            job = zd.submit_bulk_ticket_import_job(api_payloads, archive_immediately=archive)
            job_state = zd.wait_for_job(job, "Bulk Ticket Import")
        except ZendeskBulkImportError as exc:
            if exc.status_code == 403:
                bulk_enabled = False
                print("  [Bulk Import] HTTP 403 received; falling back to single ticket import.", flush=True)
                for item in batch_items:
                    process_single_fallback(item, fallback_reason="HTTP 403 bulk endpoint unavailable")
                return
            if exc.status_code in {400, 413, 422} and len(batch_items) > 1:
                mid = len(batch_items) // 2
                print(f"  [Bulk Import] HTTP {exc.status_code}; splitting into {mid} and {len(batch_items) - mid} sub-batches.", flush=True)
                process_bulk_batch(batch_items[:mid])
                process_bulk_batch(batch_items[mid:])
                return
            for item in batch_items:
                mark_failure(item, str(exc), "BULK_REJECTED", exc.status_code)
            return
        except Exception as exc:
            print(f"  [Bulk Import] Job error: {exc}. Reconciling items individually...", flush=True)
            for item in batch_items:
                check_recovery_or_mark_review(item, str(exc))
            return

        # Process results by index
        results = job_state.get("results", [])
        by_index: Dict[int, Dict[str, Any]] = {}
        for res in results:
            if isinstance(res, dict) and "index" in res:
                try:
                    by_index[int(res["index"])] = res
                except (ValueError, TypeError):
                    pass
        if not by_index and len(results) == len(batch_items):
            by_index = {i: r for i, r in enumerate(results) if isinstance(r, dict)}

        for idx, item in enumerate(batch_items):
            res = by_index.get(idx)
            z_id = res.get("id") if res else None
            if z_id:
                mark_success(item, int(z_id))
            else:
                check_recovery_or_mark_review(item, f"Bulk job result missing: {res}")

    def mark_success(item: Dict[str, Any], zendesk_id: int) -> None:
        ticket_row: MigrationTicket = item["ticket_row"]
        case_id = ticket_row.case_id
        try:
            verified_ext_id = zd.validate_ticket_external_id(zendesk_id, case_id)
            final_status = "completed_with_warnings" if item["failed_atts"] > 0 else "completed"
            ticket_row.status = final_status
            ticket_row.zendesk_ticket_id = zendesk_id
            ticket_row.zendesk_external_id = verified_ext_id
            ticket_row.attempts = (ticket_row.attempts or 0) + 1
            ticket_row.completed_at = datetime.now(timezone.utc)
            ticket_row.error_type = ""
            ticket_row.failure_reason = ""
            ticket_row.error_detail = ""
        except Exception as exc:
            ticket_row.status = "manual_review_required"
            ticket_row.zendesk_ticket_id = zendesk_id
            ticket_row.error_type = "ID_MISMATCH"
            ticket_row.failure_reason = str(exc)[:1000]
            ticket_row.error_detail = str(exc)

    def mark_failure(item: Dict[str, Any], error: str, error_type: str = "IMPORT_FAILED", status_code: Any = "") -> None:
        ticket_row: MigrationTicket = item["ticket_row"]
        ticket_row.status = "failed"
        ticket_row.error_type = error_type
        ticket_row.http_status = str(status_code)
        ticket_row.failure_reason = error.splitlines()[0][:1000] if error else "Failed"
        ticket_row.error_detail = error
        ticket_row.attempts = (ticket_row.attempts or 0) + 1

    def check_recovery_or_mark_review(item: Dict[str, Any], message: str) -> None:
        ticket_row: MigrationTicket = item["ticket_row"]
        case_id = ticket_row.case_id
        found = zd.find_ticket_by_external_id(case_id)
        if found and found.get("id"):
            mark_success(item, int(found["id"]))
            return

        ticket_row.status = "manual_review_required"
        ticket_row.error_type = "BULK_RESULT_UNRESOLVED"
        ticket_row.failure_reason = message[:1000]
        ticket_row.error_detail = message
        ticket_row.attempts = (ticket_row.attempts or 0) + 1

    def process_single_fallback(item: Dict[str, Any], fallback_reason: str = "") -> None:
        ticket_row: MigrationTicket = item["ticket_row"]
        case_id = ticket_row.case_id
        ticket_id = 0
        try:
            created = zd.import_single_ticket(item["clean_payload"], archive_immediately=archive)
            ticket_id = int(created["id"])
            mark_success(item, ticket_id)
        except Exception as exc:
            detail = f"{fallback_reason}\n{exc}" if fallback_reason else str(exc)
            if ticket_id:
                ticket_row.status = "manual_review_required"
                ticket_row.zendesk_ticket_id = ticket_id
                ticket_row.failure_reason = detail[:1000]
                ticket_row.error_detail = detail
            else:
                mark_failure(item, detail, "SINGLE_IMPORT_FAILED")

    # -------------------------------------------------------------------------
    # INTELLIGENT BATCH PRE-CHECK:
    # Check if candidate tickets are already in Zendesk using fast batch lookup
    # -------------------------------------------------------------------------
    unverified = [t for t in tickets if not t.zendesk_ticket_id]
    if unverified:
        print(f"Pre-checking {len(unverified):,} candidate tickets against Zendesk using intelligent batch lookup...", flush=True)
        existing_in_zd: Dict[str, Dict[str, Any]] = {}

        if start_date and end_date and len(unverified) >= 20:
            print(f"  [Zendesk] Fast-scanning tickets created in date range {start_date} -> {end_date}...", flush=True)
            existing_in_zd = zd.find_tickets_in_date_range(start_date, end_date)
            print(f"  [Zendesk] Found {len(existing_in_zd):,} matching tickets in Zendesk for date range.", flush=True)

        unresolved_cids = [t.case_id for t in unverified if t.case_id not in existing_in_zd]
        if unresolved_cids and len(unresolved_cids) <= 3000:
            print(f"  [Zendesk] Checking {len(unresolved_cids):,} remaining tickets via chunked search (30 IDs/req)...", flush=True)
            chunk_results = zd.batch_find_tickets_by_external_ids(unresolved_cids, chunk_size=30)
            existing_in_zd.update(chunk_results)

        matched_count = 0
        for t in unverified:
            ext_data = existing_in_zd.get(t.case_id) or existing_in_zd.get(t.case_id.lower())
            if ext_data and ext_data.get("id"):
                t.status = "existing"
                t.zendesk_ticket_id = int(ext_data["id"])
                t.zendesk_external_id = str(ext_data.get("external_id") or t.case_id)
                t.completed_at = datetime.now(timezone.utc)
                matched_count += 1
        if matched_count > 0:
            session.commit()
            print(f"Database synchronized: {matched_count:,} tickets were already present in Zendesk and marked 'existing'.", flush=True)

    tickets_to_import = [t for t in tickets if t.status in ("prepared", "failed", "importing") and not t.zendesk_ticket_id]
    if not tickets_to_import:
        print(f"All {len(tickets):,} {label.lower()} are confirmed in Zendesk! Zero import calls needed.")
        return {
            "total": len(tickets),
            "already_completed": len(tickets),
            "completed": len(tickets),
            "completed_with_warnings": 0,
            "existing": len(tickets),
            "failed": 0,
            "manual_review_required": 0,
        }

    print(f"Proceeding to import {len(tickets_to_import):,} remaining new tickets into Zendesk...", flush=True)

    pending_batch: List[Dict[str, Any]] = []

    for index, t in enumerate(tickets_to_import, start=1):
        case_id = t.case_id

        raw_payload = dict(t.prepared_payload_json or {})
        if not raw_payload:
            t.status = "failed"
            t.error_type = "EMPTY_PAYLOAD"
            t.failure_reason = "Prepared payload is empty."
            continue

        raw_payload["external_id"] = case_id
        t.started_at = datetime.now(timezone.utc)
        t.status = "importing"

        payload_with_atts, uploaded, failed = link_attachments_to_payload(case_id, raw_payload)
        clean_payload = strip_private_metadata(payload_with_atts)

        item = {
            "ticket_row": t,
            "clean_payload": clean_payload,
            "uploaded_atts": uploaded,
            "failed_atts": failed,
        }

        if not bulk_enabled:
            process_single_fallback(item)
        else:
            pending_batch.append(item)
            if len(pending_batch) >= batch_size:
                process_bulk_batch(pending_batch)
                session.commit()
                pending_batch.clear()

        if index % 100 == 0 or index == len(tickets_to_import):
            session.commit()
            completed = session.query(MigrationTicket).filter(
                MigrationTicket.status.in_(["completed", "existing", "completed_with_warnings"])
            )
            if ticket_type:
                completed = completed.filter_by(ticket_type=ticket_type)
            if start_date or end_date:
                completed = completed.join(SFCase, MigrationTicket.case_id == SFCase.id)
                if start_date:
                    completed = completed.filter(SFCase.created_date >= f"{start_date}T00:00:00Z")
                if end_date:
                    parsed_end = datetime.strptime(end_date, "%Y-%m-%d").date()
                    next_day = parsed_end + timedelta(days=1)
                    completed = completed.filter(SFCase.created_date < f"{next_day.isoformat()}T00:00:00Z")
            print(f"  [Progress] {index:,}/{len(tickets_to_import):,} new tickets imported; {completed.count():,} finished in date range...", flush=True)

    if pending_batch:
        process_bulk_batch(pending_batch)
        session.commit()
        pending_batch.clear()

    session.commit()

    # Compute final statistics directly from database scoped by date range
    final_query = session.query(MigrationTicket)
    if ticket_type:
        final_query = final_query.filter_by(ticket_type=ticket_type)
    if start_date or end_date:
        final_query = final_query.join(SFCase, MigrationTicket.case_id == SFCase.id)
        if start_date:
            final_query = final_query.filter(SFCase.created_date >= f"{start_date}T00:00:00Z")
        if end_date:
            parsed_end = datetime.strptime(end_date, "%Y-%m-%d").date()
            next_day = parsed_end + timedelta(days=1)
            final_query = final_query.filter(SFCase.created_date < f"{next_day.isoformat()}T00:00:00Z")

    completed_total = final_query.filter(MigrationTicket.status == "completed").count()
    completed_warnings = final_query.filter(MigrationTicket.status == "completed_with_warnings").count()
    existing_total = final_query.filter(MigrationTicket.status == "existing").count()
    failed_total = final_query.filter(MigrationTicket.status == "failed").count()
    review_total = final_query.filter(MigrationTicket.status == "manual_review_required").count()

    print("=" * 68)
    print(f"STEP 4/5 COMPLETED: {label}")
    print(f"Successfully Imported: {completed_total:,}")
    print(f"Imported with Attachment Warnings: {completed_warnings:,}")
    print(f"Already Existing in Zendesk: {existing_total:,}")
    print(f"Failed: {failed_total:,}")
    print(f"Manual Review Required: {review_total:,}")
    print("=" * 68)

    return {
        "completed": completed_total,
        "completed_with_warnings": completed_warnings,
        "existing": existing_total,
        "failed": failed_total,
        "manual_review_required": review_total,
    }
