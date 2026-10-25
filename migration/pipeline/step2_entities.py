"""Step 2 - Bulk-optimized Organizations and End Users migration directly into database."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from migration.clients.zendesk import ZendeskClient
from migration.config import MigrationSettings
from migration.db.models import (
    SFAccount,
    SFCase,
    SFContact,
    SFEmailMessage,
    SFUser,
    ZendeskOrganization,
    ZendeskUser,
)
from migration.engine.field_mapper import normalize_label, s
from migration.engine.identity_resolver import IdentityResolver, norm, sha12, valid_email


from datetime import datetime, timedelta

def run_step2_entities(
    settings: MigrationSettings,
    session: Session,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    ticket_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Execute Step 2: Sync Zendesk Organizations and End Users into database tables."""
    print("=" * 68)
    print("STEP 2: MIGRATE ORGANIZATIONS & END USERS (BULK OPTIMIZED & DB STORED)")
    if ticket_ids:
        print(f"Ticket IDs Scope: {len(ticket_ids)} specified ticket(s)")
    if start_date or end_date:
        print(f"Date Scope: {start_date or 'beginning'} -> {end_date or 'latest'}")
    print("=" * 68)

    zd = ZendeskClient(
        subdomain=settings.zendesk.subdomain,
        client_id=settings.zendesk.client_id,
        client_secret=settings.zendesk.client_secret,
        scope=settings.zendesk.scope,
    )
    print("Authenticating with Zendesk...")
    zd.authenticate()
    print("Zendesk authentication successful.")

    # -------------------------------------------------------------------------
    # 1. Organizations Sync
    # -------------------------------------------------------------------------
    if ticket_ids:
        from sqlalchemy import or_
        scoped_case_accs = session.query(SFCase.account_id).filter(
            or_(SFCase.id.in_(ticket_ids), SFCase.case_number.in_(ticket_ids))
        ).distinct().all()
        rel_account_ids = {r[0] for r in scoped_case_accs if r[0]}
        accounts = session.query(SFAccount).filter(SFAccount.id.in_(rel_account_ids)).all() if rel_account_ids else []
    else:
        accounts = session.query(SFAccount).all()
    print(f"Loaded {len(accounts):,} Salesforce Accounts from database.", flush=True)

    # Check already synced organizations in DB
    existing_db_orgs = {
        org.salesforce_account_id: org
        for org in session.query(ZendeskOrganization).filter(ZendeskOrganization.status.in_(["completed", "existing"])).all()
    }
    org_map: Dict[str, int] = {aid: org.zendesk_org_id for aid, org in existing_db_orgs.items() if org.zendesk_org_id}

    org_candidates: List[Tuple[SFAccount, str, str]] = []
    seen_account_ids: Set[str] = set()

    for acc in accounts:
        aid = acc.id
        if not aid or aid in seen_account_ids:
            continue
        seen_account_ids.add(aid)

        if aid in org_map:
            continue

        ext_id = f"sf_account_{aid}"
        org_candidates.append((acc, aid, ext_id))

    print(
        f"Organizations: {len(org_map):,} already synced in DB; {len(org_candidates):,} candidates to verify/create.",
        flush=True,
    )

    if org_candidates:
        candidate_ext_ids = [ext for _, _, ext in org_candidates]
        # Check Zendesk by external IDs
        existing_zd_orgs = zd.show_many_by_external_ids("/api/v2/organizations/show_many", "organizations", candidate_ext_ids)

        to_create_orgs: List[Tuple[SFAccount, str, str]] = []
        for acc, aid, ext_id in org_candidates:
            found = existing_zd_orgs.get(ext_id.casefold())
            if found and found.get("id"):
                z_id = int(found["id"])
                org_map[aid] = z_id
                db_org = session.query(ZendeskOrganization).filter_by(salesforce_account_id=aid).first()
                if not db_org:
                    session.add(ZendeskOrganization(salesforce_account_id=aid, name=acc.name, external_id=ext_id, zendesk_org_id=z_id, status="existing"))
                else:
                    db_org.zendesk_org_id = z_id
                    db_org.status = "existing"
            else:
                to_create_orgs.append((acc, aid, ext_id))

        if to_create_orgs:
            print(f"Organizations: bulk creating {len(to_create_orgs):,} new organizations...", flush=True)
            org_payloads = [
                {"name": acc.name or f"Salesforce {ext_id}", "external_id": ext_id}
                for acc, _, ext_id in to_create_orgs
            ]
            zd.bulk_create("/api/v2/organizations/create_many", "organizations", org_payloads, "Organization bulk create")

            # Verify created organizations
            refreshed_orgs = zd.show_many_by_external_ids(
                "/api/v2/organizations/show_many", "organizations", [ext for _, _, ext in to_create_orgs]
            )
            for acc, aid, ext_id in to_create_orgs:
                found = refreshed_orgs.get(ext_id.casefold())
                if found and found.get("id"):
                    z_id = int(found["id"])
                    org_map[aid] = z_id
                    db_org = session.query(ZendeskOrganization).filter_by(salesforce_account_id=aid).first()
                    if not db_org:
                        session.add(ZendeskOrganization(salesforce_account_id=aid, name=acc.name, external_id=ext_id, zendesk_org_id=z_id, status="completed"))
                    else:
                        db_org.zendesk_org_id = z_id
                        db_org.status = "completed"
                else:
                    # Single record fallback
                    try:
                        created_org = zd.create_or_update_organization(acc.name, ext_id)
                        z_id = int(created_org["id"])
                        org_map[aid] = z_id
                        db_org = session.query(ZendeskOrganization).filter_by(salesforce_account_id=aid).first()
                        if not db_org:
                            session.add(ZendeskOrganization(salesforce_account_id=aid, name=acc.name, external_id=ext_id, zendesk_org_id=z_id, status="completed"))
                        else:
                            db_org.zendesk_org_id = z_id
                            db_org.status = "completed"
                    except Exception as exc:
                        db_org = session.query(ZendeskOrganization).filter_by(salesforce_account_id=aid).first()
                        if not db_org:
                            session.add(ZendeskOrganization(salesforce_account_id=aid, name=acc.name, external_id=ext_id, status="failed", error_message=str(exc)))
                        else:
                            db_org.status = "failed"
                            db_org.error_message = str(exc)

        session.commit()
    print(f"Organizations sync complete. Total mapped organizations in DB: {len(org_map):,}.", flush=True)

    # -------------------------------------------------------------------------
    # 2. End Users Discovery & Sync (Memory-Optimized Projections)
    # -------------------------------------------------------------------------
    # Project ONLY the lightweight columns required for discovery.
    # CRITICAL: Exclude raw_data_json, custom_fields_json, and description.
    from sqlalchemy import literal_column, or_

    case_query = session.query(
        SFCase.id,
        SFCase.case_number,
        SFCase.contact_id,
        SFCase.account_id,
        literal_column("raw_data_json->>'ContactEmail'").label("contact_email"),
        literal_column("raw_data_json->>'SuppliedEmail'").label("supplied_email"),
        literal_column("raw_data_json->>'SuppliedName'").label("supplied_name"),
        literal_column("raw_data_json->>'SuppliedPhone'").label("supplied_phone"),
    )
    if ticket_ids:
        case_query = case_query.filter(
            or_(SFCase.id.in_(ticket_ids), SFCase.case_number.in_(ticket_ids))
        )
    if start_date:
        case_query = case_query.filter(SFCase.created_date >= f"{start_date}T00:00:00Z")
    if end_date:
        parsed_end = datetime.strptime(end_date, "%Y-%m-%d").date()
        next_day = parsed_end + timedelta(days=1)
        case_query = case_query.filter(SFCase.created_date < f"{next_day.isoformat()}T00:00:00Z")

    print("Querying candidate Cases from database for identity discovery...", flush=True)
    case_rows = case_query.all()
    print(f"Loaded {len(case_rows):,} candidate Case headers.", flush=True)

    case_ids_set = {r.id for r in case_rows}
    contact_ids_set = {r.contact_id for r in case_rows if r.contact_id}

    if contact_ids_set:
        contacts = session.query(
            SFContact.id,
            SFContact.name,
            SFContact.email,
            SFContact.phone,
            SFContact.mobile_phone,
        ).filter(SFContact.id.in_(contact_ids_set)).all()
        contacts_by_id = {
            c.id: {
                "id": c.id,
                "name": c.name,
                "email": c.email,
                "phone": c.phone,
                "mobile_phone": c.mobile_phone,
            }
            for c in contacts
        }
    else:
        contacts_by_id = {}

    # MEMORY OPTIMIZATION:
    # Query ONLY incoming emails and ONLY the 3 needed columns (parent_id, from_address, from_name).
    # Completely avoids loading multi-gigabyte html_body, text_body, and raw_data_json into RAM.
    print("Querying incoming email message senders from database...", flush=True)
    emails_by_case: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    email_rows = session.query(
        SFEmailMessage.parent_id,
        SFEmailMessage.from_address,
        SFEmailMessage.from_name,
    ).filter(SFEmailMessage.incoming == True).all()

    for em_parent_id, em_from_addr, em_from_name in email_rows:
        if em_parent_id in case_ids_set:
            emails_by_case[em_parent_id].append({
                "incoming": True,
                "from_address": em_from_addr,
                "from_name": em_from_name,
            })
    del email_rows

    cases_list = [
        {
            "id": r.id,
            "case_number": r.case_number,
            "contact_id": r.contact_id,
            "account_id": r.account_id,
            "contact_email": r.contact_email,
            "supplied_email": r.supplied_email,
            "supplied_name": r.supplied_name,
            "supplied_phone": r.supplied_phone,
        }
        for r in case_rows
    ]
    del case_rows

    sf_users = session.query(SFUser.id, SFUser.email).all()
    protected_agent_emails = {norm(u.email) for u in sf_users if valid_email(u.email)}
    protected_agent_emails.add(norm(settings.agent_comment_email))

    resolver = IdentityResolver(synthetic_domain=settings.synthetic_user_domain)
    identities = resolver.discover_identities(
        cases_list, contacts_by_id, emails_by_case, protected_agent_emails
    )
    del cases_list
    del emails_by_case
    del contacts_by_id
    print(f"Identities discovered: {len(identities):,} unique customer identities.", flush=True)

    # Check existing users in DB table (only lightweight projected fields)
    existing_user_rows = session.query(
        ZendeskUser.identity_key,
        ZendeskUser.source_email,
        ZendeskUser.zendesk_user_id,
    ).filter(
        ZendeskUser.status.in_(["completed", "existing"]),
        ZendeskUser.zendesk_user_id.isnot(None),
    ).all()
    user_state: Dict[str, int] = {r.identity_key: r.zendesk_user_id for r in existing_user_rows if r.identity_key}
    email_map: Dict[str, int] = {norm(r.source_email): r.zendesk_user_id for r in existing_user_rows if r.source_email}
    all_db_identity_keys: Set[str] = {r[0] for r in session.query(ZendeskUser.identity_key).all()}

    missing_unique_emails: Dict[str, Dict[str, Any]] = {}
    identity_keys_by_email: Dict[str, List[str]] = defaultdict(list)

    new_user_records_count = 0
    for identity_key, identity in sorted(identities.items()):
        if identity_key in user_state:
            continue

        src_email = valid_email(identity.get("source_email"))
        if not src_email:
            continue

        ext_id = "sf_customer_" + sha12(identity_key)
        clean_email_key = norm(src_email)

        # If email already mapped to an existing user in this run
        if clean_email_key in email_map and not identity.get("is_synthetic"):
            uid = email_map[clean_email_key]
            user_state[identity_key] = uid
            if identity_key not in all_db_identity_keys:
                session.add(
                    ZendeskUser(
                        identity_key=identity_key,
                        salesforce_contact_id=identity.get("salesforce_contact_id"),
                        source_email=src_email,
                        zendesk_email=src_email,
                        phone=identity.get("phone"),
                        salesforce_account_id=identity.get("account_id"),
                        zendesk_org_id=org_map.get(identity.get("account_id")),
                        external_id=ext_id,
                        zendesk_user_id=uid,
                        is_synthetic=bool(identity.get("is_synthetic")),
                        status="existing",
                    )
                )
                all_db_identity_keys.add(identity_key)
                new_user_records_count += 1
                if new_user_records_count % 1000 == 0:
                    session.commit()
            continue

        # Queue for verification/creation
        identity_keys_by_email[clean_email_key].append(identity_key)
        missing_unique_emails.setdefault(
            clean_email_key,
            {
                "name": identity.get("name") or "Imported User",
                "email": src_email,
                "external_id": ext_id,
                "role": "end-user",
                "skip_verify_email": True,
                "phone": identity.get("phone"),
                "_account_id": identity.get("account_id"),
                "_identity_key": identity_key,
                "_is_synthetic": bool(identity.get("is_synthetic")),
            },
        )

    if new_user_records_count > 0:
        session.commit()

    print(
        f"End Users: {len(user_state):,} already resolved in DB; "
        f"{len(missing_unique_emails):,} unique emails to check/create in Zendesk.",
        flush=True,
    )

    if missing_unique_emails:
        pending_payloads: List[Dict[str, Any]] = []
        pending_meta: List[Dict[str, Any]] = []
        for email_key, item in missing_unique_emails.items():
            payload = {k: v for k, v in item.items() if not k.startswith("_") and v not in (None, "")}
            org_id = org_map.get(item.get("_account_id"))
            if org_id:
                payload["organization_id"] = int(org_id)
            pending_payloads.append(payload)
            pending_meta.append({"email_key": email_key, **item})

        # Process in batches of 200 (2 bulk creation jobs at a time) to keep memory flat
        batch_size = 200
        for chunk_start in range(0, len(pending_payloads), batch_size):
            chunk_payloads = pending_payloads[chunk_start : chunk_start + batch_size]
            chunk_meta = pending_meta[chunk_start : chunk_start + batch_size]

            # Try bulk creation
            zd.bulk_create("/api/v2/users/create_many", "users", chunk_payloads, "End User bulk create")

            # Verify created users by external ID
            refreshed_users = zd.show_many_by_external_ids(
                "/api/v2/users/show_many", "users", [u["external_id"] for u in chunk_payloads if u.get("external_id")]
            )

            for meta, payload in zip(chunk_meta, chunk_payloads):
                ext_id = payload.get("external_id")
                user = refreshed_users.get(ext_id.casefold()) if ext_id else None

                if not user:
                    # Fallback to single user creation
                    try:
                        org_id = payload.get("organization_id")
                        source_phone = payload.get("phone")
                        try:
                            user = zd.create_end_user(
                                payload.get("name"), payload.get("email"), ext_id,
                                int(org_id) if org_id else None, phone=source_phone
                            )
                        except Exception as create_err:
                            err_str = str(create_err)
                            if "422" in err_str and "phone" in err_str.lower():
                                user = zd.create_end_user(
                                    payload.get("name"), payload.get("email"), ext_id,
                                    int(org_id) if org_id else None, phone=None
                                )
                            else:
                                user = zd.find_user_by_external_id(ext_id)
                                if not user and not meta.get("_is_synthetic"):
                                    user = zd.find_user_by_email(payload.get("email"))
                                if not user:
                                    raise create_err
                    except Exception as exc:
                        email_key = meta["email_key"]
                        for ikey in identity_keys_by_email.get(email_key, []):
                            ident = identities[ikey]
                            db_u = session.query(ZendeskUser).filter_by(identity_key=ikey).first()
                            if not db_u:
                                session.add(
                                    ZendeskUser(
                                        identity_key=ikey,
                                        salesforce_contact_id=ident.get("salesforce_contact_id"),
                                        source_email=ident.get("source_email"),
                                        salesforce_account_id=ident.get("account_id"),
                                        external_id=ext_id,
                                        is_synthetic=bool(ident.get("is_synthetic")),
                                        status="failed",
                                        error_message=str(exc),
                                    )
                                )
                            else:
                                db_u.status = "failed"
                                db_u.error_message = str(exc)
                        continue

                if not user or not user.get("id"):
                    continue

                uid = int(user["id"])
                source_email = payload.get("email")
                email_map[norm(source_email)] = uid

                for ikey in identity_keys_by_email.get(meta["email_key"], []):
                    ident = identities[ikey]
                    user_state[ikey] = uid
                    db_u = session.query(ZendeskUser).filter_by(identity_key=ikey).first()
                    if not db_u:
                        session.add(
                            ZendeskUser(
                                identity_key=ikey,
                                salesforce_contact_id=ident.get("salesforce_contact_id"),
                                source_email=ident.get("source_email"),
                                zendesk_email=user.get("email") or source_email,
                                phone=ident.get("phone"),
                                salesforce_account_id=ident.get("account_id"),
                                zendesk_org_id=org_map.get(ident.get("account_id")),
                                external_id=ext_id,
                                zendesk_user_id=uid,
                                is_synthetic=bool(ident.get("is_synthetic")),
                                status="completed",
                            )
                        )
                    else:
                        db_u.zendesk_user_id = uid
                        db_u.zendesk_email = user.get("email") or source_email
                        db_u.status = "completed"

            session.commit()

    total_users_success = session.query(ZendeskUser).filter(ZendeskUser.status.in_(["completed", "existing"])).count()
    total_users_failed = session.query(ZendeskUser).filter_by(status="failed").count()
    print(f"End Users sync complete: {total_users_success:,} active in DB, {total_users_failed:,} failed.")

    return {
        "organizations_mapped": len(org_map),
        "users_mapped": total_users_success,
        "users_failed": total_users_failed,
    }
