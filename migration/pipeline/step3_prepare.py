"""Step 3 - Prepare ticket payloads and store in database migration_tickets table."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import or_
from sqlalchemy.orm import Session

from migration.clients.zendesk import ZendeskClient
from migration.config import MigrationSettings
from migration.db.models import (
    MigrationAuditLog,
    MigrationTicket,
    SFCase,
    SFContact,
    SFEmailMessage,
    SFRecordType,
    SFS3Record,
    SFTask,
    SFUser,
    ZendeskOrganization,
    ZendeskUser,
)
from migration.engine.field_mapper import (
    BRAND_PRODUCT_MAP,
    DatabaseFieldMapper,
    normalize_label,
    resolve_brand_and_product,
    s,
)
from migration.engine.identity_resolver import norm, valid_email
from migration.engine.payload_builder import build_ticket_payload


def run_step3_prepare(
    settings: MigrationSettings,
    session: Session,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    max_tickets: Optional[int] = None,
    ticket_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Execute Step 3: Prepare Zendesk ticket payloads and persist in migration_tickets DB table."""
    print("=" * 68)
    print("STEP 3: PREPARE TICKET PAYLOADS (DATABASE PERSISTED)")
    print("=" * 68)

    zd = ZendeskClient(
        subdomain=settings.zendesk.subdomain,
        client_id=settings.zendesk.client_id,
        client_secret=settings.zendesk.client_secret,
        scope=settings.zendesk.scope,
    )
    zd.authenticate()

    akarsh = zd.find_user_by_email(settings.agent_comment_email)
    if not akarsh:
        raise RuntimeError(f"Zendesk agent not found for comment author {settings.agent_comment_email}.")
    akarsh_user_id = int(akarsh["id"])

    # Load DatabaseFieldMapper
    mapper = DatabaseFieldMapper(session)
    print(f"Loaded {len(mapper.field_rows):,} field mappings and {len(mapper.value_rows):,} value mappings from database.")

    # Load Organization & User mappings from DB
    org_map = {
        org.salesforce_account_id: org.zendesk_org_id
        for org in session.query(ZendeskOrganization).filter(ZendeskOrganization.status.in_(["completed", "existing"])).all()
        if org.zendesk_org_id
    }
    customer_users = {
        u.identity_key: u.zendesk_user_id
        for u in session.query(ZendeskUser).filter(ZendeskUser.status.in_(["completed", "existing"])).all()
        if u.zendesk_user_id
    }
    customer_user_by_email = {
        norm(u.source_email): u.zendesk_user_id
        for u in session.query(ZendeskUser).filter(ZendeskUser.status.in_(["completed", "existing"])).all()
        if u.source_email and u.zendesk_user_id
    }

    # Load Cases from DB (filtered by ticket_ids or date range if provided)
    case_query = session.query(SFCase).order_by(SFCase.created_date.asc())
    if ticket_ids:
        case_query = case_query.filter(
            or_(
                SFCase.id.in_(ticket_ids),
                SFCase.case_number.in_(ticket_ids),
            )
        )
    if start_date:
        case_query = case_query.filter(SFCase.created_date >= f"{start_date}T00:00:00Z")
    if end_date:
        parsed_end = datetime.strptime(end_date, "%Y-%m-%d").date()
        next_day = parsed_end + timedelta(days=1)
        case_query = case_query.filter(SFCase.created_date < f"{next_day.isoformat()}T00:00:00Z")

    limit_val = max_tickets if max_tickets is not None else settings.max_tickets
    if limit_val > 0:
        case_query = case_query.limit(limit_val)
    cases = case_query.all()
    filter_desc = []
    if ticket_ids:
        filter_desc.append(f"{len(ticket_ids)} ticket IDs")
    if start_date or end_date:
        filter_desc.append(f"date range {start_date} -> {end_date}")
    date_str = f" for ({', '.join(filter_desc)})" if filter_desc else ""
    print(f"Loaded {len(cases):,} Cases from database{date_str}.", flush=True)

    # Load related entities from DB
    contacts = session.query(SFContact).all()
    contacts_by_id = {c.id: {"id": c.id, "name": c.name, "email": c.email, "phone": c.phone, "mobile_phone": c.mobile_phone} for c in contacts}

    sf_users = session.query(SFUser).all()
    protected_agent_emails: Set[str] = {norm(u.email) for u in sf_users if valid_email(u.email)}
    protected_agent_emails.add(norm(settings.agent_comment_email))

    agent_user_by_sf_user_id: Dict[str, int] = {}
    agent_user_by_email: Dict[str, int] = {}
    owner_assignee_map: Dict[str, int] = {}

    # Pre-cache Zendesk agents in 1 API call to avoid dozens of roundtrips
    try:
        zd_agents = zd.list_all("/api/v2/users.json?role[]=agent&role[]=admin", "users", "agents")
        zd_agent_by_email = {norm(u.get("email")): int(u["id"]) for u in zd_agents if u.get("email") and u.get("id")}
    except Exception as exc:
        print(f"  [Zendesk] Note: could not pre-list agents: {exc}", flush=True)
        zd_agent_by_email = {}

    for sf_u in sf_users:
        em = valid_email(sf_u.email)
        if not em:
            continue
        clean = norm(em)
        agent_id = zd_agent_by_email.get(clean)
        if not agent_id:
            z_agent = zd.find_user_by_email(em)
            if z_agent and z_agent.get("id"):
                agent_id = int(z_agent["id"])
                zd_agent_by_email[clean] = agent_id
        if agent_id:
            agent_user_by_sf_user_id[sf_u.id] = agent_id
            agent_user_by_email[clean] = agent_id

    for c in cases:
        if c.owner_id and c.owner_id in agent_user_by_sf_user_id:
            owner_assignee_map[c.owner_id] = agent_user_by_sf_user_id[c.owner_id]

    # Pre-cache groups and brands (cached in memory)
    group_map: Dict[str, int] = {}
    for c in cases:
        grp = c.raw_data_json.get("BP_Group__c") if c.raw_data_json else None
        if grp and normalize_label(grp) not in group_map:
            mapped_grp = mapper.mapped_zendesk_value("BP_Group__c", grp) or grp
            zg = zd.find_group_by_name(mapped_grp)
            if zg and zg.get("id"):
                group_map[normalize_label(grp)] = int(zg["id"])
                group_map[normalize_label(mapped_grp)] = int(zg["id"])

    # Pre-cache brand mappings from static map and dynamic lookup
    brand_map: Dict[str, int] = {}
    for k, v in BRAND_PRODUCT_MAP.items():
        brand_map[normalize_label(k)] = int(v["brand_id"])
        brand_map[normalize_label(v["brand_name"])] = int(v["brand_id"])

    for c in cases:
        prd = c.raw_data_json.get("BP_Product__c") if c.raw_data_json else None
        if prd and normalize_label(prd) not in brand_map:
            bp_info = resolve_brand_and_product(prd)
            if bp_info:
                brand_map[normalize_label(prd)] = int(bp_info["brand_id"])
                continue
            mapped_brand = mapper.mapped_zendesk_value("BP_Product__c", prd) or prd
            zb = zd.find_brand_by_name(mapped_brand)
            if zb and zb.get("id"):
                brand_map[normalize_label(prd)] = int(zb["id"])
                brand_map[normalize_label(mapped_brand)] = int(zb["id"])

    record_types = session.query(SFRecordType).all()
    record_types_by_id = {rt.id: {"id": rt.id, "name": rt.name} for rt in record_types}

    prep_report: List[Dict[str, Any]] = []
    custom_field_report: List[Dict[str, Any]] = []
    prepared_count = 0
    failed_count = 0
    standard_count = 0
    phone_count = 0

    print("Building ticket payloads in memory-safe batches...", flush=True)

    batch_size = 500
    for batch_start in range(0, len(cases), batch_size):
        batch_cases = cases[batch_start : batch_start + batch_size]
        batch_case_ids = [c.id for c in batch_cases]

        # Query related records ONLY for the current batch of cases to prevent OOM
        batch_emails = session.query(SFEmailMessage).filter(SFEmailMessage.parent_id.in_(batch_case_ids)).all()
        emails_by_case: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for em in batch_emails:
            emails_by_case[em.parent_id].append(em.raw_data_json or {
                "id": em.id,
                "created_date": em.created_date,
                "message_date": em.message_date,
                "incoming": em.incoming,
                "from_address": em.from_address,
                "html_body": em.html_body,
                "text_body": em.text_body,
                "created_by_id": em.created_by_id,
                "is_private_draft": em.is_private_draft,
            })
        del batch_emails

        batch_tasks = session.query(SFTask).filter(SFTask.what_id.in_(batch_case_ids)).all()
        tasks_by_case: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for t in batch_tasks:
            tasks_by_case[t.what_id].append({
                "id": t.id,
                "subject": t.subject,
                "description": t.description,
                "recording_urls_json": t.recording_urls_json or [],
            })
        del batch_tasks

        batch_s3 = session.query(SFS3Record).filter(SFS3Record.bp_case_id.in_(batch_case_ids)).all()
        s3_by_case: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for s3r in batch_s3:
            s3_by_case[s3r.bp_case_id].append({
                "id": s3r.id,
                "bp_s3_url": s3r.bp_s3_url,
                "bp_fallback_url": s3r.bp_fallback_url,
            })
        del batch_s3

        # Query existing tickets only for this batch
        batch_existing_tickets = {
            t.case_id: t
            for t in session.query(MigrationTicket).filter(MigrationTicket.case_id.in_(batch_case_ids)).all()
        }

        for c in batch_cases:
            cid = c.id
            source_type = "phone" if c.is_phone_case else "standard"
            case_data = dict(c.raw_data_json or {})
            case_data.update({
                "id": c.id,
                "case_number": c.case_number,
                "account_id": c.account_id,
                "contact_id": c.contact_id,
                "owner_id": c.owner_id,
                "created_date": c.created_date,
                "subject": c.subject,
                "description": c.description,
                "status": c.status,
                "priority": c.priority,
                "origin": c.origin,
                "type": c.type,
                "record_type_id": c.record_type_id,
            })

            # Skip regenerating payload if ticket was already imported successfully
            existing_t = batch_existing_tickets.get(cid)
            if existing_t and existing_t.status in {"completed", "existing", "completed_with_warnings"}:
                prepared_count += 1
                if source_type == "phone":
                    phone_count += 1
                else:
                    standard_count += 1
                continue

            try:
                payload, audit = build_ticket_payload(
                    case=case_data,
                    email_messages=emails_by_case.get(cid, []),
                    s3_records=s3_by_case.get(cid, []),
                    mapper=mapper,
                    contacts_by_id=contacts_by_id,
                    customer_users=customer_users,
                    customer_user_by_email=customer_user_by_email,
                    org_map=org_map,
                    owner_assignee_map=owner_assignee_map,
                    group_map=group_map,
                    brand_map=brand_map,
                    record_types_by_id=record_types_by_id,
                    akarsh_user_id=akarsh_user_id,
                    agent_user_by_sf_user_id=agent_user_by_sf_user_id,
                    agent_user_by_email=agent_user_by_email,
                    protected_agent_emails=protected_agent_emails,
                    prep_report=prep_report,
                    custom_field_report=custom_field_report,
                    phone_tasks=tasks_by_case.get(cid, []),
                    synthetic_domain=settings.synthetic_user_domain,
                )

                if not existing_t:
                    db_ticket = MigrationTicket(
                        case_id=cid,
                        case_number=c.case_number,
                        ticket_type=source_type,
                        status="prepared",
                        prepared_payload_json=payload,
                        audit_json=audit,
                    )
                    session.add(db_ticket)
                    batch_existing_tickets[cid] = db_ticket
                else:
                    existing_t.ticket_type = source_type
                    existing_t.status = "prepared"
                    existing_t.prepared_payload_json = payload
                    existing_t.audit_json = audit

                prepared_count += 1
                if source_type == "phone":
                    phone_count += 1
                else:
                    standard_count += 1

            except Exception as exc:
                failed_count += 1
                err_msg = str(exc)
                if not existing_t:
                    db_ticket = MigrationTicket(
                        case_id=cid,
                        case_number=c.case_number,
                        ticket_type=source_type,
                        status="failed",
                        error_type="PREPARATION_FAILED",
                        failure_reason=err_msg[:1000],
                        error_detail=err_msg,
                    )
                    session.add(db_ticket)
                    batch_existing_tickets[cid] = db_ticket
                else:
                    existing_t.status = "failed"
                    existing_t.error_type = "PREPARATION_FAILED"
                    existing_t.failure_reason = err_msg[:1000]
                    existing_t.error_detail = err_msg

                session.add(
                    MigrationAuditLog(
                        case_id=cid,
                        stage="PREPARE",
                        level="ERROR",
                        status="FAILED",
                        message=f"Payload preparation failed: {err_msg}",
                    )
                )

        session.commit()
        print(f"  [Prepare] Processed {min(batch_start + batch_size, len(cases)):,}/{len(cases):,} Cases; prepared {prepared_count:,}, failed {failed_count:,}...", flush=True)
    session.commit()
    print("=" * 68)
    print("STEP 3 COMPLETE: TICKET PAYLOADS PREPARED IN DATABASE")
    print(f"Total Cases: {len(cases):,}")
    print(f"Payloads Ready: {prepared_count:,} ({standard_count:,} standard, {phone_count:,} phone)")
    print(f"Preparation Failures: {failed_count:,}")
    print("=" * 68)

    return {
        "cases": len(cases),
        "payloads": prepared_count,
        "standard_cases": standard_count,
        "phone_cases": phone_count,
        "failed": failed_count,
    }
