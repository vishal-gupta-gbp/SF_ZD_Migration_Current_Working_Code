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


def run_step2_entities(settings: MigrationSettings, session: Session) -> Dict[str, Any]:
    """Execute Step 2: Sync Zendesk Organizations and End Users into database tables."""
    print("=" * 68)
    print("STEP 2: MIGRATE ORGANIZATIONS & END USERS (BULK OPTIMIZED & DB STORED)")
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
    # 2. End Users Discovery & Sync
    # -------------------------------------------------------------------------
    cases = session.query(SFCase).all()
    contacts = session.query(SFContact).all()
    emails = session.query(SFEmailMessage).all()
    sf_users = session.query(SFUser).all()

    contacts_by_id = {c.id: {"id": c.id, "name": c.name, "email": c.email, "phone": c.phone, "mobile_phone": c.mobile_phone} for c in contacts}
    emails_by_case: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for em in emails:
        emails_by_case[em.parent_id].append({
            "id": em.id,
            "incoming": em.incoming,
            "from_address": em.from_address,
            "from_name": em.from_name,
        })

    cases_list = [
        {
            "id": c.id,
            "case_number": c.case_number,
            "contact_id": c.contact_id,
            "account_id": c.account_id,
            "raw_data": c.raw_data_json or {},
        }
        for c in cases
    ]

    protected_agent_emails = {norm(u.email) for u in sf_users if valid_email(u.email)}
    protected_agent_emails.add(norm(settings.agent_comment_email))

    resolver = IdentityResolver(synthetic_domain=settings.synthetic_user_domain)
    identities = resolver.discover_identities(
        cases_list, contacts_by_id, emails_by_case, protected_agent_emails
    )
    print(f"Identities discovered: {len(identities):,} unique customer identities.", flush=True)

    # Check existing users in DB table
    existing_db_users = {
        u.identity_key: u
        for u in session.query(ZendeskUser).filter(ZendeskUser.status.in_(["completed", "existing"])).all()
    }
    user_state: Dict[str, int] = {k: u.zendesk_user_id for k, u in existing_db_users.items() if u.zendesk_user_id}
    email_map: Dict[str, int] = {norm(u.source_email): u.zendesk_user_id for u in existing_db_users.values() if u.source_email and u.zendesk_user_id}

    missing_unique_emails: Dict[str, Dict[str, Any]] = {}
    identity_keys_by_email: Dict[str, List[str]] = defaultdict(list)

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
            db_u = session.query(ZendeskUser).filter_by(identity_key=identity_key).first()
            if not db_u:
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

        # Try bulk creation
        zd.bulk_create("/api/v2/users/create_many", "users", pending_payloads, "End User bulk create")

        # Verify created users by external ID
        refreshed_users = zd.show_many_by_external_ids(
            "/api/v2/users/show_many", "users", [u["external_id"] for u in pending_payloads if u.get("external_id")]
        )

        for meta, payload in zip(pending_meta, pending_payloads):
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
