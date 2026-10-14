"""Step 1 - Export Salesforce data and download S3 attachments into database."""
from __future__ import annotations

import hashlib
import mimetypes
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from sqlalchemy.orm import Session

from migration.clients.s3 import S3Client, construct_migration_s3_url, parse_s3_url
from migration.clients.salesforce import SalesforceClient, chunks, quote_soql, soql_ids
from migration.config import MigrationSettings
from migration.db.models import (
    MigrationAttachment,
    SFAccount,
    SFCase,
    SFContact,
    SFEmailMessage,
    SFRecordType,
    SFS3Record,
    SFTask,
    SFUser,
)
from migration.engine.comment_builder import parse_dt
from migration.engine.field_mapper import INTERNAL_COMMENT_FIELDS, normalize_label, s
from migration.engine.identity_resolver import sha12

PHONE_SOURCE_VALUES = {"phone", "telephone", "tel", "call"}
URL_RE = re.compile(r"https?://[^\s<>\"']+", flags=re.I)


def is_phone_case(case: Dict[str, Any]) -> bool:
    values = [case.get("BP_Source__c"), case.get("Origin")]
    for val in values:
        if normalize_label(val) in PHONE_SOURCE_VALUES:
            return True
    return False


def extract_urls_from_value(value: Any) -> List[str]:
    raw = s(value)
    if not raw:
        return []
    urls: List[str] = []
    for match in URL_RE.findall(raw):
        url = match.rstrip(",.;:)]}")
        if url and url not in urls:
            urls.append(url)
    return urls


def safe_local_filename(filename: str, prefix: str = "") -> str:
    cleaned = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", str(filename)).strip().strip(".")
    if not cleaned:
        cleaned = "attachment"
    if prefix:
        cleaned = f"{prefix}_{cleaned}"
    return cleaned[:180]


def extract_comment_id(record: Dict[str, Any]) -> Tuple[str, str]:
    record_id = s(record.get("BP_RecordId__c"))
    if record_id.startswith("02s"):
        return record_id, "BP_RecordId__c"
    for key in ("BP_S3_URL__c", "BP_Fallback_URL__c"):
        raw = s(record.get(key))
        if "02s" in raw:
            match = re.search(r"\b(02s[A-Za-z0-9]{12,15})\b", raw)
            if match:
                return match.group(1), f"URL regex in {key}"
    return "", ""


def build_case_select_fields(sf: SalesforceClient, session: Session) -> List[str]:
    """Build bounded SELECT list including mapped fields from database."""
    core_fields = {
        "Id", "AccountId", "ContactId", "CaseNumber", "CreatedById", "CreatedDate",
        "Description", "OwnerId", "Priority", "Status", "Subject", "SuppliedCompany",
        "SuppliedEmail", "SuppliedName", "SuppliedPhone", "ContactEmail", "Origin", "ParentId",
        "Type", "RecordTypeId", "To_Address__c", "BP_Group__c", "BP_Product__c", "Status_B2B__c", "BP_Source__c",
    }
    core_fields.update(INTERNAL_COMMENT_FIELDS)

    # Add all mapped fields from DB
    from migration.db.models import MappingField
    db_fields = session.query(MappingField.salesforce_api_name).distinct().all()
    for (api_name,) in db_fields:
        if api_name and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_name):
            core_fields.add(api_name)

    available = sf.describe_fields("Case")
    fields = sorted(f for f in core_fields if f in available)
    if "Id" not in fields or "CreatedDate" not in fields or "Subject" not in fields:
        raise RuntimeError("Required Case fields (Id, CreatedDate, Subject) not found in Salesforce describe metadata.")
    return fields


def run_step1_export(
    settings: MigrationSettings,
    session: Session,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    max_tickets: Optional[int] = None,
) -> Dict[str, Any]:
    """Execute Step 1: Export Salesforce data and download attachments into database."""
    print("=" * 68)
    print("STEP 1: SALESFORCE DATA EXPORT & ATTACHMENTS (DATABASE PERSISTED)")
    print("=" * 68)

    limit_val = max_tickets if max_tickets is not None else settings.max_tickets
    sf = SalesforceClient(
        base_url=settings.salesforce.base_url,
        client_id=settings.salesforce.consumer_key,
        client_secret=settings.salesforce.consumer_secret,
        api_version=settings.salesforce.api_version,
    )
    print("Authenticating with Salesforce...")
    sf.authenticate()
    print("Salesforce authentication successful.")

    # Build date filters
    filters: List[str] = []
    if start_date:
        parsed_start = datetime.strptime(start_date, "%Y-%m-%d").date()
        filters.append(f"CreatedDate >= {start_date}T00:00:00Z")
    if end_date:
        parsed_end = datetime.strptime(end_date, "%Y-%m-%d").date()
        next_day = parsed_end + timedelta(days=1)
        filters.append(f"CreatedDate < {next_day.isoformat()}T00:00:00Z")

    where_clause = f" WHERE {' AND '.join(filters)}" if filters else ""
    fields = build_case_select_fields(sf, session)

    case_soql = f"SELECT {', '.join(fields)} FROM Case{where_clause} ORDER BY CreatedDate ASC"
    if limit_val > 0:
        case_soql += f" LIMIT {limit_val}"

    print(f"Executing Case query: {case_soql[:160]}...")
    cases = sf.query(case_soql, progress_label="Cases")
    if not cases:
        print("No Salesforce Cases returned for the specified filters.")
        return {"cases": 0}

    print(f"Exported {len(cases):,} Cases from Salesforce.")
    case_ids = [s(c.get("Id")) for c in cases if s(c.get("Id"))]
    account_ids = sorted({s(c.get("AccountId")) for c in cases if s(c.get("AccountId"))})
    contact_ids = sorted({s(c.get("ContactId")) for c in cases if s(c.get("ContactId"))})

    # Fetch related EmailMessages
    print("Fetching related EmailMessages in batches...")
    email_messages = sf.query_by_id_chunks(
        "SELECT Id, ParentId, CreatedById, CreatedDate, LastModifiedDate, "
        "MessageDate, Incoming, FromAddress, ToAddress, CcAddress, BccAddress, "
        "FromName, Subject, TextBody, HtmlBody, Status, IsPrivateDraft, HasAttachment, Headers "
        "FROM EmailMessage WHERE ParentId IN ({ids}) ORDER BY ParentId, MessageDate ASC, CreatedDate ASC",
        case_ids,
        batch_size=settings.export_batch_size,
        label="EmailMessages",
    )
    print(f"Exported {len(email_messages):,} EmailMessages.")

    # Fetch related S3 records
    print("Fetching related Salesforce BP_S3__c records...")
    s3_records = sf.query_by_id_chunks(
        "SELECT Id, Name, CreatedDate, BP_Case__c, BP_RecordId__c, BP_S3_URL__c, BP_Fallback_URL__c, BP_Upload_Complete__c "
        "FROM BP_S3__c WHERE BP_Case__c IN ({ids}) AND IsDeleted = false ORDER BY BP_Case__c, CreatedDate ASC",
        case_ids,
        batch_size=settings.export_batch_size,
        label="S3 Records",
    )
    print(f"Exported {len(s3_records):,} S3 Records.")

    # Fetch related Contacts
    print("Fetching related Contacts...")
    contacts = sf.query_by_id_chunks(
        "SELECT Id, AccountId, Name, FirstName, LastName, Email, Phone, MobilePhone, "
        "MailingStreet, MailingCity, MailingState, MailingPostalCode, MailingCountry, "
        "OwnerId, CreatedDate, LastModifiedDate FROM Contact WHERE Id IN ({ids})",
        contact_ids,
        batch_size=settings.export_batch_size,
        label="Contacts",
    )
    print(f"Exported {len(contacts):,} Contacts.")

    # Fetch related Accounts
    print("Fetching related Accounts...")
    accounts = sf.query_by_id_chunks(
        "SELECT Id, Name, Type, ParentId, BillingStreet, BillingCity, BillingState, "
        "BillingPostalCode, BillingCountry, Phone, Website, Industry, NumberOfEmployees, "
        "Description, OwnerId, CreatedDate, LastModifiedDate FROM Account WHERE Id IN ({ids})",
        account_ids,
        batch_size=settings.export_batch_size,
        label="Accounts",
    )
    print(f"Exported {len(accounts):,} Accounts.")

    # Fetch related Users
    user_ids = sorted(
        {s(c.get("OwnerId")) for c in cases if s(c.get("OwnerId"))}
        | {s(m.get("CreatedById")) for m in email_messages if s(m.get("CreatedById"))}
    )
    print(f"Fetching {len(user_ids):,} related Salesforce Users...")
    users = sf.query_by_id_chunks(
        "SELECT Id, Name, Email, Username, IsActive, UserType, Department, Title FROM User WHERE Id IN ({ids})",
        user_ids,
        batch_size=settings.export_batch_size,
        label="Users",
    )
    print(f"Exported {len(users):,} Users.")

    # Fetch Record Types
    rt_ids = sorted({s(c.get("RecordTypeId")) for c in cases if s(c.get("RecordTypeId"))})
    record_types = sf.query_by_id_chunks(
        "SELECT Id, Name, DeveloperName, SobjectType, IsActive FROM RecordType WHERE Id IN ({ids})",
        rt_ids,
        batch_size=settings.export_batch_size,
        label="Record Types",
    )
    print(f"Exported {len(record_types):,} Record Types.")

    # Phone Cases & Tasks
    phone_cases = [c for c in cases if is_phone_case(c)]
    phone_tasks: List[Dict[str, Any]] = []
    if phone_cases:
        phone_case_ids = [s(c.get("Id")) for c in phone_cases if s(c.get("Id"))]
        task_fields = sf.describe_fields("Task")
        pref_fields = {
            "Id", "WhatId", "WhoId", "Subject", "Description", "CreatedDate", "CreatedById",
            "Status", "Priority", "Type", "TaskSubtype", "CallType", "CallDisposition",
            "CallDurationInSeconds", "ActivityDate", "IsDeleted",
        }
        for fn in task_fields:
            lowered = fn.lower()
            if any(token in lowered for token in ("record", "recording", "call", "voicemail")) and any(token in lowered for token in ("url", "link", "record", "recording", "call", "voicemail")):
                pref_fields.add(fn)
        task_select = sorted(f for f in pref_fields if f in task_fields)

        print(f"Fetching Tasks for {len(phone_cases):,} phone-origin Cases...")
        phone_tasks = sf.query_by_id_chunks(
            f"SELECT {', '.join(task_select)} FROM Task WHERE WhatId IN ({{ids}}) AND IsDeleted = false ORDER BY WhatId, CreatedDate ASC",
            phone_case_ids,
            batch_size=settings.export_batch_size,
            label="Phone Tasks",
        )
        print(f"Exported {len(phone_tasks):,} Phone Tasks.")

    # ---------------------------------------------------------
    # Persist all exported data into database tables (in chunks)
    # ---------------------------------------------------------
    print("Saving exported data into database tables...", flush=True)

    # 1. Accounts
    if accounts:
        for batch in chunks(accounts, 500):
            bids = [s(acc.get("Id")) for acc in batch if s(acc.get("Id"))]
            existing_accs = {a.id: a for a in session.query(SFAccount).filter(SFAccount.id.in_(bids)).all()}
            for acc in batch:
                aid = s(acc.get("Id"))
                if not aid:
                    continue
                if aid not in existing_accs:
                    session.add(SFAccount(id=aid, name=s(acc.get("Name")), type=s(acc.get("Type")), raw_data_json=acc))
                else:
                    existing_accs[aid].name = s(acc.get("Name"))
                    existing_accs[aid].type = s(acc.get("Type"))
                    existing_accs[aid].raw_data_json = acc
            session.commit()

    # 2. Contacts
    if contacts:
        for batch in chunks(contacts, 500):
            bids = [s(con.get("Id")) for con in batch if s(con.get("Id"))]
            existing_cons = {c.id: c for c in session.query(SFContact).filter(SFContact.id.in_(bids)).all()}
            for con in batch:
                cid = s(con.get("Id"))
                if not cid:
                    continue
                if cid not in existing_cons:
                    session.add(
                        SFContact(
                            id=cid,
                            account_id=s(con.get("AccountId")),
                            name=s(con.get("Name")),
                            first_name=s(con.get("FirstName")),
                            last_name=s(con.get("LastName")),
                            email=s(con.get("Email")),
                            phone=s(con.get("Phone")),
                            mobile_phone=s(con.get("MobilePhone")),
                            raw_data_json=con,
                        )
                    )
                else:
                    existing_cons[cid].name = s(con.get("Name"))
                    existing_cons[cid].email = s(con.get("Email"))
                    existing_cons[cid].phone = s(con.get("Phone"))
                    existing_cons[cid].mobile_phone = s(con.get("MobilePhone"))
                    existing_cons[cid].raw_data_json = con
            session.commit()

    # 3. Users
    if users:
        for batch in chunks(users, 500):
            bids = [s(u.get("Id")) for u in batch if s(u.get("Id"))]
            existing_users = {u.id: u for u in session.query(SFUser).filter(SFUser.id.in_(bids)).all()}
            for u in batch:
                uid = s(u.get("Id"))
                if not uid or uid in existing_users:
                    continue
                session.add(
                    SFUser(
                        id=uid,
                        name=s(u.get("Name")),
                        email=s(u.get("Email")),
                        username=s(u.get("Username")),
                        is_active=bool(u.get("IsActive")),
                        user_type=s(u.get("UserType")),
                    )
                )
            session.commit()

    # 4. Record Types
    if record_types:
        for batch in chunks(record_types, 500):
            bids = [s(rt.get("Id")) for rt in batch if s(rt.get("Id"))]
            existing_rts = {rt.id: rt for rt in session.query(SFRecordType).filter(SFRecordType.id.in_(bids)).all()}
            for rt in batch:
                rt_id = s(rt.get("Id"))
                if not rt_id or rt_id in existing_rts:
                    continue
                session.add(
                    SFRecordType(
                        id=rt_id,
                        name=s(rt.get("Name")),
                        developer_name=s(rt.get("DeveloperName")),
                        sobject_type=s(rt.get("SobjectType")),
                        is_active=bool(rt.get("IsActive")),
                    )
                )
            session.commit()

    # 5. Cases
    if cases:
        saved_cases = 0
        for batch in chunks(cases, 500):
            bids = [s(c.get("Id")) for c in batch if s(c.get("Id"))]
            existing_cases = {c.id: c for c in session.query(SFCase).filter(SFCase.id.in_(bids)).all()}
            for c in batch:
                cid = s(c.get("Id"))
                if not cid:
                    continue
                is_phone = is_phone_case(c)
                if cid not in existing_cases:
                    session.add(
                        SFCase(
                            id=cid,
                            case_number=s(c.get("CaseNumber")),
                            account_id=s(c.get("AccountId")),
                            contact_id=s(c.get("ContactId")),
                            owner_id=s(c.get("OwnerId")),
                            created_date=parse_dt(c.get("CreatedDate")),
                            subject=s(c.get("Subject")),
                            description=s(c.get("Description")),
                            status=s(c.get("Status")),
                            priority=s(c.get("Priority")),
                            origin=s(c.get("Origin")),
                            type=s(c.get("Type")),
                            record_type_id=s(c.get("RecordTypeId")),
                            is_phone_case=is_phone,
                            raw_data_json=c,
                        )
                    )
                else:
                    existing_cases[cid].subject = s(c.get("Subject"))
                    existing_cases[cid].status = s(c.get("Status"))
                    existing_cases[cid].is_phone_case = is_phone
                    existing_cases[cid].raw_data_json = c
            session.commit()
            saved_cases += len(batch)
            if saved_cases % 2500 == 0 or saved_cases == len(cases):
                print(f"  [Database] Stored {saved_cases:,}/{len(cases):,} Cases...", flush=True)

    # 6. EmailMessages
    if email_messages:
        saved_emails = 0
        for batch in chunks(email_messages, 500):
            bids = [s(em.get("Id")) for em in batch if s(em.get("Id"))]
            existing_email_ids = {r[0] for r in session.query(SFEmailMessage.id).filter(SFEmailMessage.id.in_(bids)).all()}
            for em in batch:
                em_id = s(em.get("Id"))
                if not em_id or em_id in existing_email_ids:
                    continue
                session.add(
                    SFEmailMessage(
                        id=em_id,
                        parent_id=s(em.get("ParentId")),
                        created_by_id=s(em.get("CreatedById")),
                        created_date=parse_dt(em.get("CreatedDate")),
                        message_date=parse_dt(em.get("MessageDate")),
                        incoming=bool(em.get("Incoming")),
                        from_address=s(em.get("FromAddress")),
                        to_address=s(em.get("ToAddress")),
                        cc_address=s(em.get("CcAddress")),
                        bcc_address=s(em.get("BccAddress")),
                        from_name=s(em.get("FromName")),
                        subject=s(em.get("Subject")),
                        text_body=s(em.get("TextBody")),
                        html_body=s(em.get("HtmlBody")),
                        status=s(em.get("Status")),
                        is_private_draft=bool(em.get("IsPrivateDraft")),
                        has_attachment=bool(em.get("HasAttachment")),
                        raw_data_json=em,
                    )
                )
            session.commit()
            saved_emails += len(batch)
            if saved_emails % 2500 == 0 or saved_emails == len(email_messages):
                print(f"  [Database] Stored {saved_emails:,}/{len(email_messages):,} EmailMessages...", flush=True)

    # 7. S3 Records
    if s3_records:
        for batch in chunks(s3_records, 500):
            bids = [s(s3r.get("Id")) for s3r in batch if s(s3r.get("Id"))]
            existing_s3_ids = {r[0] for r in session.query(SFS3Record.id).filter(SFS3Record.id.in_(bids)).all()}
            for s3r in batch:
                s3_id = s(s3r.get("Id"))
                if not s3_id or s3_id in existing_s3_ids:
                    continue
                session.add(
                    SFS3Record(
                        id=s3_id,
                        name=s(s3r.get("Name")),
                        bp_case_id=s(s3r.get("BP_Case__c")),
                        bp_record_id=s(s3r.get("BP_RecordId__c")),
                        bp_s3_url=s(s3r.get("BP_S3_URL__c")),
                        bp_fallback_url=s(s3r.get("BP_Fallback_URL__c")),
                        created_date=parse_dt(s3r.get("CreatedDate")),
                    )
                )
            session.commit()

    # 8. Phone Tasks
    if phone_tasks:
        for batch in chunks(phone_tasks, 500):
            bids = [s(t.get("Id")) for t in batch if s(t.get("Id"))]
            existing_task_ids = {r[0] for r in session.query(SFTask.id).filter(SFTask.id.in_(bids)).all()}
            for t in batch:
                tid = s(t.get("Id"))
                if not tid or tid in existing_task_ids:
                    continue
                recording_urls = []
                for key in t.keys():
                    lowered = key.lower()
                    if key == "Description" or any(token in lowered for token in ("recording", "record", "call", "voicemail")) and any(token in lowered for token in ("url", "link", "record", "recording", "call", "voicemail")):
                        for u in extract_urls_from_value(t.get(key)):
                            if u not in recording_urls:
                                recording_urls.append(u)
                session.add(
                    SFTask(
                        id=tid,
                        what_id=s(t.get("WhatId")),
                        who_id=s(t.get("WhoId")),
                        subject=s(t.get("Subject")),
                        description=s(t.get("Description")),
                        created_date=parse_dt(t.get("CreatedDate")),
                        recording_urls_json=recording_urls,
                    )
                )
            session.commit()

    print("Database staging tables populated successfully.")

    # ---------------------------------------------------------
    # Concurrent S3 Attachment Download & DB Tracking
    # ---------------------------------------------------------
    files_dir = settings.workspace_dir / "attachments" / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    target_base = settings.s3_base_url

    if s3_records and target_base:
        print(f"Processing {len(s3_records):,} S3 attachment records with concurrency...", flush=True)
        s3_client = S3Client(
            access_key_id=settings.aws.access_key_id,
            secret_access_key=settings.aws.secret_access_key,
            region=settings.aws.region,
        )

        # Check existing attachment records in DB to reuse
        existing_attachments = {
            a.migration_s3_url: a
            for a in session.query(MigrationAttachment).filter(MigrationAttachment.download_status == "success").all()
        }

        download_jobs: Dict[str, Dict[str, Any]] = {}
        for s3r in s3_records:
            source_url = s(s3r.get("BP_S3_URL__c")) or s(s3r.get("BP_Fallback_URL__c"))
            if not source_url:
                continue
            migration_url = construct_migration_s3_url(source_url, target_base)
            if not migration_url:
                continue

            # If already in DB and file exists on disk, reuse
            prior = existing_attachments.get(migration_url)
            if prior and prior.local_file_path and (settings.workspace_dir / prior.local_file_path).is_file():
                continue

            download_jobs[migration_url] = s3r

        print(
            f"  Unique S3 URLs to download: {len(download_jobs):,}; "
            f"reused existing: {len(s3_records) - len(download_jobs):,}.",
            flush=True,
        )

        def download_worker(url: str, record: Dict[str, Any]) -> Dict[str, Any]:
            case_id = s(record.get("BP_Case__c"))
            case_folder = safe_local_filename(case_id or "unknown_case")
            name = safe_local_filename(s(record.get("Name")) or Path(parse_s3_url(url)[1]).name or f"attachment_{s(record.get('Id'))}")
            filename = safe_local_filename(name, prefix=sha12(s(record.get("Id")) or url)[-8:])
            local_path = files_dir / case_folder / filename

            try:
                meta = s3_client.download_file(url, local_path)
                return {
                    "url": url,
                    "case_id": case_id,
                    "record_id": s(record.get("Id")),
                    "local_file_path": str(local_path.relative_to(settings.workspace_dir)),
                    "file_name": name,
                    "bucket": meta.get("bucket"),
                    "key": meta.get("key"),
                    "size": meta.get("size"),
                    "content_type": meta.get("content_type"),
                    "sha256": meta.get("sha256"),
                    "status": "success",
                    "error": "",
                }
            except Exception as exc:
                return {
                    "url": url,
                    "case_id": case_id,
                    "record_id": s(record.get("Id")),
                    "local_file_path": "",
                    "file_name": name,
                    "bucket": "",
                    "key": "",
                    "size": 0,
                    "content_type": "",
                    "sha256": "",
                    "status": "failed",
                    "error": str(exc),
                }

        download_results: Dict[str, Dict[str, Any]] = {}
        if download_jobs:
            with ThreadPoolExecutor(max_workers=settings.s3_download_concurrency) as pool:
                futures = {pool.submit(download_worker, url, rec): url for url, rec in download_jobs.items()}
                for future in as_completed(futures):
                    res = future.result()
                    download_results[res["url"]] = res

        # Insert or update MigrationAttachment records in DB
        existing_s3_att_ids = {
            r[0]
            for r in session.query(MigrationAttachment.s3_record_id).filter(MigrationAttachment.s3_record_id.isnot(None)).all()
        }
        att_batch = 0
        for s3r in s3_records:
            s3_rec_id = s(s3r.get("Id"))
            if not s3_rec_id or s3_rec_id in existing_s3_att_ids:
                continue
            source_url = s(s3r.get("BP_S3_URL__c")) or s(s3r.get("BP_Fallback_URL__c"))
            migration_url = construct_migration_s3_url(source_url, target_base) if source_url else ""
            comment_id, _ = extract_comment_id(s3r)
            case_id = s(s3r.get("BP_Case__c"))

            res = download_results.get(migration_url)
            prior = existing_attachments.get(migration_url)

            if res:
                att = MigrationAttachment(
                    case_id=case_id,
                    comment_id=comment_id,
                    s3_record_id=s3_rec_id,
                    file_name=res["file_name"],
                    original_s3_url=source_url,
                    migration_s3_url=migration_url,
                    local_file_path=res["local_file_path"],
                    bucket=res["bucket"],
                    key=res["key"],
                    file_size_bytes=res["size"],
                    content_type=res["content_type"],
                    sha256=res["sha256"],
                    download_status=res["status"],
                    download_error=res["error"],
                    upload_status="pending",
                )
                session.add(att)
                existing_s3_att_ids.add(s3_rec_id)
                att_batch += 1
            elif prior:
                session.add(
                    MigrationAttachment(
                        case_id=case_id,
                        comment_id=comment_id,
                        s3_record_id=s3_rec_id,
                        file_name=prior.file_name,
                        original_s3_url=source_url,
                        migration_s3_url=migration_url,
                        local_file_path=prior.local_file_path,
                        bucket=prior.bucket,
                        key=prior.key,
                        file_size_bytes=prior.file_size_bytes,
                        content_type=prior.content_type,
                        sha256=prior.sha256,
                        download_status="success",
                        upload_status="pending",
                    )
                )
                existing_s3_att_ids.add(s3_rec_id)
                att_batch += 1

            if att_batch >= 500:
                session.commit()
                att_batch = 0
        session.commit()

    total_att_success = session.query(MigrationAttachment).filter_by(download_status="success").count()
    total_att_failed = session.query(MigrationAttachment).filter_by(download_status="failed").count()
    print(f"S3 attachments status: {total_att_success:,} downloaded/ready, {total_att_failed:,} failed.")

    summary = {
        "cases": len(cases),
        "phone_cases": len(phone_cases),
        "phone_tasks": len(phone_tasks),
        "email_messages": len(email_messages),
        "s3_records": len(s3_records),
        "contacts": len(contacts),
        "accounts": len(accounts),
        "users": len(users),
        "attachments_downloaded": total_att_success,
    }
    return summary
