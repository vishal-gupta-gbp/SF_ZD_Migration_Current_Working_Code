"""Optimized Step 2 for the existing Salesforce -> Zendesk modular migration.

Requires the existing migration_core.py in the same folder. Uses its configuration,
Zendesk client, source identity rules, and output helpers; it does not change ticket
payloads or the field/value mapping.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

import migration_core as mc

BATCH_SIZE = 100
SAVE_EVERY = 1000
JOB_POLL_SECONDS = 3
JOB_TIMEOUT_SECONDS = 60 * 60


def _chunks(items: List[Any], size: int = BATCH_SIZE):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _response_json(zd, method: str, path: str, *, params=None, body=None) -> Dict[str, Any]:
    response = zd.request(method, path, params=params, json_body=body)
    if response.status_code not in {200, 201, 202}:
        raise RuntimeError(
            f"Zendesk API {method} {path} failed: HTTP {response.status_code}: "
            f"{response.text[:1200]}"
        )
    if not response.text:
        return {}
    return response.json()


def _list_all(zd, path: str, key: str, label: str) -> List[Dict[str, Any]]:
    """Cursor-paginate the Zendesk list endpoint; supports next-page style too."""
    print(f"Loading existing Zendesk {label} (paged lookup)...", flush=True)
    url = path
    params = {"page[size]": 100}
    records: List[Dict[str, Any]] = []
    pages = 0
    while url:
        response = zd.request("GET", url, params=params)
        if response.status_code != 200:
            # Some endpoints/accounts may not accept cursor parameters; fall back to
            # the standard per_page/next_page paging style.
            if pages == 0 and params and "page[size]" in params:
                params = {"per_page": 100}
                response = zd.request("GET", path, params=params)
            if response.status_code != 200:
                raise RuntimeError(
                    f"Zendesk {label} listing failed: HTTP {response.status_code}: "
                    f"{response.text[:1200]}"
                )
        data = response.json()
        records.extend(data.get(key, []))
        pages += 1
        if pages % 25 == 0:
            print(f"  {label}: loaded {len(records):,} records ({pages} pages)...", flush=True)
        links = data.get("links") or {}
        next_url = links.get("next") or data.get("next_page")
        if next_url:
            url = next_url
            params = None
        else:
            # Cursor responses commonly expose links.next. A response without it
            # is complete; do not try to synthesize a cursor.
            url = ""
    print(f"  {label}: {len(records):,} records loaded.", flush=True)
    return records


def _show_many_by_external_ids(zd, endpoint: str, records: List[Dict[str, Any]], label: str) -> Dict[str, Dict[str, Any]]:
    """Look up records using Zendesk show_many in chunks of 100 external IDs."""
    result: Dict[str, Dict[str, Any]] = {}
    ids = list(dict.fromkeys(mc.s(r.get("external_id")) for r in records if mc.s(r.get("external_id"))))
    for index, batch in enumerate(_chunks(ids), start=1):
        data = _response_json(zd, "GET", endpoint, params={"external_ids": ",".join(batch)})
        collection_key = "organizations" if "organizations" in endpoint else "users"
        for record in data.get(collection_key, []):
            ext = mc.s(record.get("external_id")).casefold()
            if ext:
                result[ext] = record
        if index % 50 == 0:
            print(f"  {label}: checked {min(index * BATCH_SIZE, len(ids)):,}/{len(ids):,} IDs...", flush=True)
    return result


def _wait_for_job(zd, job: Dict[str, Any], label: str) -> Dict[str, Any]:
    status_info = job.get("job_status") or job
    job_id = mc.s(status_info.get("id"))
    if not job_id:
        raise RuntimeError(f"Zendesk did not return a job ID for {label}: {job}")
    deadline = time.time() + JOB_TIMEOUT_SECONDS
    last_progress = None
    while time.time() < deadline:
        data = _response_json(zd, "GET", f"/api/v2/job_statuses/{job_id}.json")
        state = data.get("job_status") or data
        status = mc.s(state.get("status")).lower()
        progress = state.get("progress")
        total = state.get("total")
        marker = (status, progress, total)
        if marker != last_progress:
            shown_progress = progress if progress is not None else "?"
            shown_total = total if total is not None else "?"
            print(f"  {label} job {job_id}: {status or 'pending'} ({shown_progress}/{shown_total})", flush=True)
            last_progress = marker
        if status == "completed":
            return state
        if status in {"failed", "killed"}:
            raise RuntimeError(f"Zendesk {label} job {job_id} ended with status={status}: {state.get('message', '')}")
        time.sleep(JOB_POLL_SECONDS)
    raise TimeoutError(f"Timed out waiting for Zendesk {label} job {job_id}; check Zendesk job status before rerunning.")


def _bulk_create(zd, endpoint: str, collection: str, payloads: List[Dict[str, Any]], label: str) -> bool:
    """Submit batches of <=100. Return False if bulk imports are disabled (HTTP 403)."""
    if not payloads:
        return True
    total_batches = (len(payloads) + BATCH_SIZE - 1) // BATCH_SIZE
    for batch_no, batch in enumerate(_chunks(payloads), start=1):
        print(f"  {label}: submitting batch {batch_no}/{total_batches} ({len(batch)} records)...", flush=True)
        response = zd.request("POST", endpoint, json_body={collection: batch})
        if response.status_code == 403:
            print(f"  Bulk {label.lower()} is not enabled (HTTP 403). Falling back to safe single-record API calls.", flush=True)
            return False
        if response.status_code not in {200, 201, 202}:
            # Stop and let the caller handle records one-by-one, which isolates bad
            # records and also handles account-specific endpoint restrictions.
            print(f"  Bulk {label.lower()} batch rejected (HTTP {response.status_code}); falling back for this batch.", flush=True)
            return False
        try:
            _wait_for_job(zd, response.json(), label)
        except Exception as exc:
            # A job can partially succeed. The caller refreshes the directory and
            # only falls back for records which are still missing.
            print(f"  Warning: {label} job reported an issue: {exc}", flush=True)
    return True


def _extract_id_map(records: List[Dict[str, Any]], key: str = "external_id") -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        value = mc.s(rec.get(key)).casefold()
        if value:
            out[value] = rec
    return out


def migrate_entities_bulk(cfg: Dict[str, Any], script_dir, mapping_path=None) -> Dict[str, Any]:
    """Drop-in replacement for migration_core.migrate_entities_modular with bulk API usage."""
    workspace = mc.modular_workspace(cfg, script_dir)
    source_dir = workspace / "salesforce_export"
    state_dir = workspace / "state"
    out_dir = workspace / "entity_migration"
    state_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    required_files = ["Accounts.csv", "Contacts.csv", "Cases.csv", "EmailMessages.csv", "Salesforce_Users.csv"]
    missing = [name for name in required_files if not (source_dir / name).exists()]
    if missing:
        raise RuntimeError(f"Salesforce export is missing: {', '.join(missing)}. Run Step 1 first.")

    accounts = mc.read_csv(source_dir / "Accounts.csv")
    contacts = mc.read_csv(source_dir / "Contacts.csv")
    cases = mc.read_csv(source_dir / "Cases.csv")
    emails = mc.read_csv(source_dir / "EmailMessages.csv")
    sf_users = mc.read_csv(source_dir / "Salesforce_Users.csv")
    print(
        f"Step 2 source data: {len(cases):,} Cases, {len(accounts):,} Accounts, "
        f"{len(contacts):,} Contacts, {len(emails):,} EmailMessages.", flush=True
    )

    zd = mc._modular_zendesk_client(cfg)

    # ---- Organizations: one paged directory read, then bulk-create only missing IDs.
    org_state_path = state_dir / "organization_map.json"
    org_map: Dict[str, int] = mc.load_json(org_state_path) if org_state_path.exists() else {}
    org_directory = _list_all(zd, "/api/v2/organizations.json", "organizations", "organizations")
    org_by_external_id = _extract_id_map(org_directory)
    org_rows: List[Dict[str, Any]] = []
    org_candidates: List[Tuple[Dict[str, Any], str, str]] = []
    seen_account_ids = set()
    for account in accounts:
        aid = mc.s(account.get("Id"))
        if not aid or aid in seen_account_ids:
            continue
        seen_account_ids.add(aid)
        ext_id = f"sf_account_{aid}"
        existing = org_by_external_id.get(ext_id.casefold())
        if existing and existing.get("id"):
            org_map[aid] = int(existing["id"])
            org_rows.append({"salesforce_account_id": aid, "zendesk_organization_id": int(existing["id"]), "status": "existing"})
        else:
            org_candidates.append((account, aid, ext_id))

    print(f"Organizations: {len(org_rows):,} already exist; {len(org_candidates):,} need creation.", flush=True)
    org_payloads = [
        {"name": mc.s(account.get("Name")) or f"Salesforce {ext_id}", "external_id": ext_id}
        for account, _aid, ext_id in org_candidates
    ]
    # If bulk jobs are unavailable/rejected, fall back to the original idempotent API.
    bulk_orgs_ok = _bulk_create(zd, "/api/v2/organizations/create_many", "organizations", org_payloads, "Organization bulk create")
    # Refresh only source external IDs to capture IDs for records created by jobs.
    wanted_orgs = [{"external_id": ext} for _, _, ext in org_candidates]
    refreshed_orgs = _show_many_by_external_ids(zd, "/api/v2/organizations/show_many", wanted_orgs, "organization verification")
    for account, aid, ext_id in org_candidates:
        found = refreshed_orgs.get(ext_id.casefold())
        if found and found.get("id"):
            org_map[aid] = int(found["id"])
            org_rows.append({"salesforce_account_id": aid, "zendesk_organization_id": int(found["id"]), "status": "completed"})
            continue
        # Fallback for rejected/partially failed bulk rows. It also makes reruns safe.
        try:
            org = zd.create_or_update_organization(mc.s(account.get("Name")), ext_id)
            if not org.get("id"):
                raise RuntimeError("Zendesk organization response did not contain an ID")
            org_map[aid] = int(org["id"])
            org_rows.append({"salesforce_account_id": aid, "zendesk_organization_id": int(org["id"]), "status": "completed"})
        except Exception as exc:
            org_rows.append({"salesforce_account_id": aid, "zendesk_organization_id": "", "status": "failed", "message": str(exc)})
    mc.save_json(org_state_path, org_map)
    mc.modular_excel_write(out_dir / "Organization_Migration_Log.xlsx", org_rows, sheet_name="Organizations")
    mc.write_csv(out_dir / "Organization_Migration_Log.csv", org_rows)
    org_success = sum(1 for r in org_rows if r.get("status") in {"completed", "existing"})
    org_failed = sum(1 for r in org_rows if r.get("status") == "failed")
    print(f"Organizations complete: {org_success:,} success/existing; {org_failed:,} failed.", flush=True)

    # ---- Build customer identities using the exact same source selection rules.
    contacts_by_id = {mc.s(c.get("Id")): c for c in contacts if mc.s(c.get("Id"))}
    emails_by_case: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for msg in emails:
        parent_id = mc.s(msg.get("ParentId"))
        if parent_id:
            emails_by_case[parent_id].append(msg)
    protected = {mc.norm(u.get("Email")) for u in sf_users if mc.valid_email(u.get("Email"))}
    protected.add(mc.norm(mc.AGENT_COMMENT_EMAIL))

    identities: Dict[str, Dict[str, Any]] = {}
    for index, case in enumerate(cases, start=1):
        cid = mc.s(case.get("Id"))
        contact_id = mc.s(case.get("ContactId"))
        contact = contacts_by_id.get(contact_id) if contact_id else None
        case_messages = emails_by_case.get(cid, [])
        source_email = mc.customer_source_email(case, contact, case_messages, protected)
        phone = (
            mc.s((contact or {}).get("Phone"))
            or mc.s((contact or {}).get("MobilePhone"))
            or mc.s(case.get("SuppliedPhone"))
        )
        if mc.valid_email(source_email):
            if contact_id:
                identity_key = f"contact:{contact_id}"
            else:
                identity_key = f"email:{mc.norm(source_email)}"
            identities.setdefault(identity_key, {
                "source_email": source_email,
                "name": mc.user_name(case, contact),
                "account_id": mc.s(case.get("AccountId")),
                "phone": phone,
                "salesforce_contact_id": contact_id,
                "case_id": cid,
            })
        else:
            # Preserve the original production rule: one deterministic synthetic
            # requester per Case when the source has no usable email address.
            identity_key = f"case:{cid}"
            synthetic_email = f"sfcase_{mc.sha12(cid)}@{mc.SYNTHETIC_USER_DOMAIN}"
            identities.setdefault(identity_key, {
                "source_email": synthetic_email,
                "name": mc.user_name(case, contact),
                "account_id": mc.s(case.get("AccountId")),
                "phone": phone,
                "salesforce_contact_id": contact_id,
                "case_id": cid,
                "synthetic_email": True,
            })
        for msg in case_messages:
            if not mc.bool_value(msg.get("Incoming")):
                continue
            incoming_email = mc.first_email(msg.get("FromAddress"))
            if not mc.valid_email(incoming_email) or mc.norm(incoming_email) in protected:
                continue
            identity_key = f"email:{mc.norm(incoming_email)}"
            identities.setdefault(identity_key, {
                "source_email": incoming_email,
                "name": mc.s(msg.get("FromName")) or "Salesforce Customer",
                "account_id": mc.s(case.get("AccountId")),
                "phone": phone,
                "salesforce_contact_id": contact_id,
                "case_id": cid,
            })
        if index % 5000 == 0:
            print(f"Identity discovery: processed {index:,}/{len(cases):,} Cases; {len(identities):,} identities so far.", flush=True)

    # Current Zendesk user directory is indexed in memory. This replaces one or two
    # per-user search requests with a paged read, and exact email matching avoids
    # accidentally mapping a source email to a fuzzy search result.
    zd_users = _list_all(zd, "/api/v2/users.json", "users", "users")
    users_by_external_id = _extract_id_map(zd_users)
    users_by_email: Dict[str, Dict[str, Any]] = {}
    for user in zd_users:
        email = mc.norm(user.get("email"))
        if email:
            users_by_email.setdefault(email, user)

    user_state_path = state_dir / "customer_user_map.json"
    user_email_state_path = state_dir / "customer_user_by_source_email.json"
    user_state: Dict[str, int] = mc.load_json(user_state_path) if user_state_path.exists() else {}
    email_map: Dict[str, int] = mc.load_json(user_email_state_path) if user_email_state_path.exists() else {}
    user_rows: List[Dict[str, Any]] = []
    missing_unique_emails: Dict[str, Dict[str, Any]] = {}
    identity_resolution: Dict[str, Dict[str, Any]] = {}
    identity_keys_by_email: Dict[str, List[str]] = defaultdict(list)

    for count, (identity_key, identity) in enumerate(sorted(identities.items()), start=1):
        source_email = mc.valid_email(identity.get("source_email"))
        if not source_email:
            user_rows.append({"identity_key": identity_key, "source_email": "", "status": "failed", "message": "Invalid or missing source email"})
            continue
        external_id = "sf_customer_" + mc.sha12(identity_key)
        existing = users_by_external_id.get(external_id.casefold())
        status = "existing" if existing else "completed"
        # Synthetic addresses are deliberately Case-specific. Do not map them to a
        # different pre-existing record just because the email lookup matches.
        if not existing and not identity.get("synthetic_email"):
            existing = users_by_email.get(mc.norm(source_email))
        if existing and existing.get("id"):
            uid = int(existing["id"])
            user_state[identity_key] = uid
            email_map[mc.norm(source_email)] = uid
            identity_resolution[identity_key] = {"user": existing, "identity": identity, "status": status}
            user_rows.append({
                "identity_key": identity_key,
                "salesforce_contact_id": identity.get("salesforce_contact_id", ""),
                "source_email": source_email,
                "zendesk_email": mc.s(existing.get("email")) or source_email,
                "phone": identity.get("phone", ""),
                "organization_id": org_map.get(mc.s(identity.get("account_id")), ""),
                "zendesk_user_id": uid,
                "status": status,
            })
        else:
            # Multiple Salesforce identities can share one email. Queue only the
            # first; all corresponding identity keys are mapped to its resulting ID.
            email_key = mc.norm(source_email)
            entry = missing_unique_emails.setdefault(email_key, {
                "name": identity.get("name") or "Imported User",
                "email": source_email,
                "external_id": external_id,
                "role": "end-user",
                "skip_verify_email": True,
                "phone": mc.s(identity.get("phone")),
                "_account_id": mc.s(identity.get("account_id")),
                "_identity_key": identity_key,
                "_source_email": source_email,
                "_synthetic_email": bool(identity.get("synthetic_email")),
            })
            identity_resolution[identity_key] = {"user": None, "identity": identity, "status": "pending", "email_key": email_key}
            identity_keys_by_email[email_key].append(identity_key)
        if count % SAVE_EVERY == 0:
            mc.save_json(user_state_path, user_state)
            mc.save_json(user_email_state_path, email_map)
            print(f"User resolution: {count:,}/{len(identities):,}; existing mapped {sum(1 for x in user_rows if x.get('zendesk_user_id')):,}; missing unique emails {len(missing_unique_emails):,}.", flush=True)

    print(f"End Users: {len(identities):,} identities; {sum(1 for r in user_rows if r.get('zendesk_user_id')):,} mapped to existing users; {len(missing_unique_emails):,} unique emails to create.", flush=True)

    # Attach Zendesk organization IDs to queued user records only when available.
    pending_user_payloads: List[Dict[str, Any]] = []
    pending_user_meta: List[Dict[str, Any]] = []
    for email_key, item in missing_unique_emails.items():
        payload = {k: v for k, v in item.items() if not k.startswith("_") and v not in (None, "")}
        # Synthetic email metadata is internal to the mapping and is not sent to Zendesk.
        payload.pop("synthetic_email", None)
        org_id = org_map.get(mc.s(item.get("_account_id")))
        if org_id:
            payload["organization_id"] = int(org_id)
        pending_user_payloads.append(payload)
        pending_user_meta.append({"email_key": email_key, **item})

    _bulk_create(zd, "/api/v2/users/create_many", "users", pending_user_payloads, "End User bulk create")

    # Batch verify every queued user by external ID. If any bulk create failed, the
    # single-record fallback below handles only unresolved users, making reruns safe.
    refreshed_users: Dict[str, Dict[str, Any]] = {}
    for batch_no, batch in enumerate(_chunks(pending_user_payloads), start=1):
        external_ids = [mc.s(u.get("external_id")) for u in batch if mc.s(u.get("external_id"))]
        if not external_ids:
            continue
        data = _response_json(zd, "GET", "/api/v2/users/show_many", params={"external_ids": ",".join(external_ids)})
        for user in data.get("users", []):
            ext = mc.s(user.get("external_id")).casefold()
            if ext:
                refreshed_users[ext] = user
        if batch_no % 25 == 0:
            print(f"  User verification: checked {min(batch_no * BATCH_SIZE, len(pending_user_payloads)):,}/{len(pending_user_payloads):,} users...", flush=True)

    # Build resulting email -> user mapping, then update every source identity key
    # sharing the same primary email.
    for pending_index, (meta, payload) in enumerate(zip(pending_user_meta, pending_user_payloads), start=1):
        if pending_index == 1 or pending_index % 100 == 0 or pending_index == len(pending_user_payloads):
            print(f"  Resolving created users: {pending_index:,}/{len(pending_user_payloads):,}...", flush=True)
        ext_id = mc.s(payload.get("external_id"))
        user = refreshed_users.get(ext_id.casefold())
        if not user:
            try:
                org_id = payload.get("organization_id")
                # This identity was absent from the full Zendesk directory read. Try
                # creation directly to avoid repeating two lookups per fallback user.
                source_phone = mc.s(payload.get("phone")) or None
                try:
                    user = zd.create_end_user(
                        mc.s(payload.get("name")), mc.s(payload.get("email")), ext_id,
                        int(org_id) if org_id else None,
                        phone=source_phone,
                    )
                except Exception as create_error:
                    error_text = str(create_error)
                    is_phone_validation_error = (
                        source_phone
                        and "HTTP 422" in error_text
                        and "phone" in error_text.lower()
                        and any(term in error_text.lower() for term in (
                            "properly formatted", "e.164", "invalid phone", "phone number"
                        ))
                    )
                    if is_phone_validation_error:
                        user = zd.create_end_user(
                            mc.s(payload.get("name")), mc.s(payload.get("email")), ext_id,
                            int(org_id) if org_id else None,
                            phone=None,
                        )
                    else:
                        # A concurrent creation or an email match may have raced with
                        # the directory scan. Resolve it before recording a failure.
                        user = zd.find_user_by_external_id(ext_id)
                        if not user and not meta.get("_synthetic_email"):
                            user = zd.find_user_by_email(mc.s(payload.get("email")))
                        if not user:
                            raise create_error
            except Exception as exc:
                email_key = meta["email_key"]
                for identity_key in identity_keys_by_email.get(email_key, []):
                    resolved = identity_resolution[identity_key]
                    ident = resolved["identity"]
                    user_rows.append({"identity_key": identity_key, "salesforce_contact_id": ident.get("salesforce_contact_id", ""), "source_email": ident.get("source_email", ""), "status": "failed", "message": str(exc)})
                continue
        if not user or not user.get("id"):
            continue
        uid = int(user["id"])
        source_email = mc.s(payload.get("email"))
        email_map[mc.norm(source_email)] = uid
        users_by_email.setdefault(mc.norm(source_email), user)
        users_by_external_id[mc.s(user.get("external_id")).casefold()] = user
        # Map every identity sharing this source email to the same Zendesk user.
        for identity_key in identity_keys_by_email.get(meta["email_key"], []):
            resolved = identity_resolution[identity_key]
            identity = resolved["identity"]
            user_state[identity_key] = uid
            user_rows.append({
                "identity_key": identity_key,
                "salesforce_contact_id": identity.get("salesforce_contact_id", ""),
                "source_email": identity.get("source_email", ""),
                "zendesk_email": mc.s(user.get("email")) or source_email,
                "phone": identity.get("phone", ""),
                "organization_id": org_map.get(mc.s(identity.get("account_id")), ""),
                "zendesk_user_id": uid,
                "status": "completed",
            })
        if pending_index % SAVE_EVERY == 0 or pending_index == len(pending_user_payloads):
            mc.save_json(user_state_path, user_state)
            mc.save_json(user_email_state_path, email_map)

    mc.save_json(user_state_path, user_state)
    mc.save_json(user_email_state_path, email_map)
    mc.modular_excel_write(out_dir / "End_User_Migration_Log.xlsx", user_rows, sheet_name="End Users")
    mc.write_csv(out_dir / "End_User_Migration_Log.csv", user_rows)

    user_success = sum(1 for r in user_rows if r.get("status") in {"completed", "existing"})
    user_failed = sum(1 for r in user_rows if r.get("status") == "failed")
    print(f"End Users complete: {user_success:,} success/existing; {user_failed:,} failed.", flush=True)
    return {
        "organizations": len(accounts),
        "organizations_success": org_success,
        "organizations_failed": org_failed,
        "end_users": len(identities),
        "end_users_success": user_success,
        "end_users_failed": user_failed,
    }
