"""Ticket payload constructor ensuring 1:1 external_id invariant and full audit trail."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from migration.engine.comment_builder import build_ticket_comments, normalize_comment_body, parse_dt
from migration.engine.field_mapper import (
    ASSOCIATION_TYPE_FIELD_ID,
    PRODUCT_FIELD_ID,
    ENQUIRY_QUERY_ISSUE_FIELD_ID,
    ENQUIRY_TEXT_FIELD_ID,
    QUERY_TEXT_FIELD_ID,
    QUERY_RELATED_ISSUE_TEXT_FIELD_ID,
    DatabaseFieldMapper,
    build_system_tags,
    normalize_label,
    resolve_brand_and_product,
    resolve_enquiry_query_issue,
    s,
    sf_system_status,
)
from migration.engine.identity_resolver import customer_source_email, first_email, norm, sha12, user_name, valid_email


def build_ticket_payload(
    case: Dict[str, Any],
    email_messages: List[Dict[str, Any]],
    s3_records: List[Dict[str, Any]],
    mapper: DatabaseFieldMapper,
    contacts_by_id: Dict[str, Dict[str, Any]],
    customer_users: Dict[str, int],
    customer_user_by_email: Dict[str, int],
    org_map: Dict[str, int],
    owner_assignee_map: Dict[str, int],
    group_map: Dict[str, int],
    brand_map: Dict[str, int],
    record_types_by_id: Dict[str, Dict[str, Any]],
    akarsh_user_id: int,
    agent_user_by_sf_user_id: Optional[Dict[str, int]],
    agent_user_by_email: Optional[Dict[str, int]],
    protected_agent_emails: Set[str],
    prep_report: List[Dict[str, Any]],
    custom_field_report: List[Dict[str, Any]],
    phone_tasks: Optional[List[Dict[str, Any]]] = None,
    synthetic_domain: str = "sfmigrateduser.com",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build a complete Zendesk Ticket Import payload for a single Salesforce Case."""
    case_id = s(case.get("id") or case.get("Id"))
    case_number = s(case.get("case_number") or case.get("CaseNumber"))
    if not case_id:
        raise ValueError("Salesforce Case.Id is blank; cannot create migration ticket.")

    contact_id = s(case.get("contact_id") or case.get("ContactId"))
    contact = contacts_by_id.get(contact_id) if contact_id else None
    account_id = s(case.get("account_id") or case.get("AccountId"))
    owner_id = s(case.get("owner_id") or case.get("OwnerId"))

    source_email = customer_source_email(case, contact, email_messages, protected_agent_emails)
    phone = (
        s((contact or {}).get("phone") or (contact or {}).get("Phone"))
        or s((contact or {}).get("mobile_phone") or (contact or {}).get("MobilePhone"))
        or s(case.get("supplied_phone") or case.get("SuppliedPhone"))
    )

    if valid_email(source_email):
        customer_email = source_email
        requester_id = customer_user_by_email.get(norm(source_email))
        if requester_id is None and contact_id:
            requester_id = customer_users.get(f"contact:{contact_id}")
    else:
        customer_email = f"sfcase_{sha12(case_id)}@{synthetic_domain}"
        requester_id = customer_users.get(f"case:{case_id}")

    if requester_id is None:
        requester_id = customer_users.get(f"case:{case_id}")
    if requester_id is None and contact_id:
        requester_id = customer_users.get(f"contact:{contact_id}")
    if requester_id is None:
        # Fallback to root migration agent to guarantee valid payload
        requester_id = akarsh_user_id

    status_value = s(case.get("status") or case.get("Status") or case.get("status_b2b__c") or case.get("Status_B2B__c"))
    ticket_status = sf_system_status(status_value)

    # Zendesk Ticket Import requires a non-blank description
    initial_desc = s(case.get("description") or case.get("Description"))
    desc_source = "Case.Description" if initial_desc else ""

    if not initial_desc:
        for msg in email_messages:
            if msg.get("is_private_draft") or msg.get("IsPrivateDraft"):
                continue
            text = s(msg.get("text_body") or msg.get("TextBody"))
            if text:
                initial_desc = text
                desc_source = "EmailMessage"
                break

    if not initial_desc:
        initial_desc = s(case.get("subject") or case.get("Subject")) or "Salesforce Case Migration"
        desc_source = "Fallback"

    initial_desc_key = normalize_comment_body(initial_desc)

    subject = s(case.get("subject") or case.get("Subject")) or f"Salesforce Case {case_number}"

    payload: Dict[str, Any] = {
        "external_id": case_id,
        "description": initial_desc,
        "subject": subject,
        "requester_id": int(requester_id),
        "status": ticket_status,
        "tags": build_system_tags(case),
    }

    created_at = parse_dt(case.get("created_date") or case.get("CreatedDate"))
    if created_at and created_at <= datetime.now(timezone.utc) and created_at.year >= 1970:
        payload["created_at"] = created_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    if account_id and account_id in org_map:
        payload["organization_id"] = int(org_map[account_id])

    if owner_id and owner_id in owner_assignee_map:
        payload["assignee_id"] = int(owner_assignee_map[owner_id])
    else:
        prep_report.append({
            "case_id": case_id,
            "status": "assignee_not_mapped",
            "source_value": owner_id,
            "message": "Salesforce owner not mapped to Zendesk agent; assignee left blank.",
        })

    # Group mapping
    group_val = s(case.get("bp_group__c") or case.get("BP_Group__c"))
    if group_val:
        mapped_grp = mapper.mapped_zendesk_value("BP_Group__c", group_val) or group_val
        grp_id = group_map.get(normalize_label(mapped_grp))
        if grp_id:
            payload["group_id"] = int(grp_id)
        else:
            prep_report.append({
                "case_id": case_id,
                "status": "group_not_mapped",
                "source_value": group_val,
            })

    # Brand mapping
    prod_val = s(case.get("bp_product__c") or case.get("BP_Product__c"))
    bp_info = resolve_brand_and_product(prod_val) if prod_val else None

    if bp_info:
        payload["brand_id"] = int(bp_info["brand_id"])
    elif prod_val:
        mapped_brand = mapper.mapped_zendesk_value("BP_Product__c", prod_val) or prod_val
        brand_id = brand_map.get(normalize_label(mapped_brand)) or brand_map.get(normalize_label(prod_val))
        if brand_id:
            payload["brand_id"] = int(brand_id)
        else:
            prep_report.append({
                "case_id": case_id,
                "status": "brand_not_mapped",
                "source_value": prod_val,
            })

    # Record Type -> Association Type
    record_type_id = s(case.get("record_type_id") or case.get("RecordTypeId"))
    record_type = record_types_by_id.get(record_type_id, {})
    rt_name = s(record_type.get("name") or record_type.get("Name"))
    association_val = ""
    if rt_name:
        for allowed in ("Parent", "Child", "NA"):
            if normalize_label(rt_name) == normalize_label(allowed):
                association_val = allowed
                break

    priority = normalize_label(case.get("priority") or case.get("Priority"))
    if priority in {"low", "medium", "high", "critical"}:
        payload["priority"] = priority

    sf_type = normalize_label(case.get("type") or case.get("Type"))
    if sf_type in {"problem", "question", "incident", "task"}:
        payload["type"] = sf_type

    # Build comments
    comments = build_ticket_comments(
        case,
        email_messages,
        customer_user_by_email,
        requester_id,
        akarsh_user_id,
        initial_description_key=initial_desc_key,
        agent_user_by_sf_user_id=agent_user_by_sf_user_id,
        agent_user_by_email=agent_user_by_email,
        phone_tasks=phone_tasks,
    )
    if comments:
        payload["comments"] = comments

    # Custom fields
    custom_fields = mapper.build_custom_fields(case, custom_field_report)
    if association_val:
        custom_fields = [f for f in custom_fields if int(f["id"]) != ASSOCIATION_TYPE_FIELD_ID]
        custom_fields.append({"id": ASSOCIATION_TYPE_FIELD_ID, "value": association_val})

    if bp_info:
        custom_fields = [f for f in custom_fields if int(f["id"]) != PRODUCT_FIELD_ID]
        custom_fields.append({"id": PRODUCT_FIELD_ID, "value": bp_info["product_value"]})

    # Enquiry Query Issue (Mandatory dropdown on Zendesk Support Form: 30888655400220)
    enquiry_val = s(case.get("bp_enquiry__c") or case.get("BP_Enquiry__c"))
    query_val = s(case.get("bp_query__c") or case.get("BP_Query__c") or case.get("bp_query_b2b__c") or case.get("BP_Query_B2B__c"))
    issue_val = s(case.get("bp_query_related_issue__c") or case.get("BP_Query_Related_Issue__c") or case.get("query_related_issue_b2b__c") or case.get("Query_Related_Issue_B2B__c"))

    enquiry_tag = resolve_enquiry_query_issue(enquiry_val, query_val, issue_val)
    if enquiry_tag:
        custom_fields = [f for f in custom_fields if int(f["id"]) != ENQUIRY_QUERY_ISSUE_FIELD_ID]
        custom_fields.append({"id": ENQUIRY_QUERY_ISSUE_FIELD_ID, "value": enquiry_tag})

    # Direct text fields for Enquiry hierarchy
    if enquiry_val:
        custom_fields = [f for f in custom_fields if int(f["id"]) != ENQUIRY_TEXT_FIELD_ID]
        custom_fields.append({"id": ENQUIRY_TEXT_FIELD_ID, "value": enquiry_val})
    if query_val:
        custom_fields = [f for f in custom_fields if int(f["id"]) != QUERY_TEXT_FIELD_ID]
        custom_fields.append({"id": QUERY_TEXT_FIELD_ID, "value": query_val})
    if issue_val:
        custom_fields = [f for f in custom_fields if int(f["id"]) != QUERY_RELATED_ISSUE_TEXT_FIELD_ID]
        custom_fields.append({"id": QUERY_RELATED_ISSUE_TEXT_FIELD_ID, "value": issue_val})

    # Source (Mandatory dropdown on Zendesk Support Form: 30867599532700)
    source_val = s(case.get("bp_source__c") or case.get("BP_Source__c") or case.get("origin") or case.get("Origin")).lower()
    source_tag_map = {
        "chat": "chat",
        "email": "email",
        "phone": "phone",
        "whatsapp": "whatsapp",
        "feedback widget": "feedback_widget",
        "feedback_widget": "feedback_widget",
        "webhook": "webhook",
        "portal": "portal__ticket_api_",
        "web": "portal__ticket_api_",
        "ticket-api": "portal__ticket_api_",
        "ticket_api": "portal__ticket_api_",
        "e-commerce": "portal__ticket_api_",
        "outbound email": "email",
    }
    source_tag = source_tag_map.get(source_val, "email")
    custom_fields = [f for f in custom_fields if int(f["id"]) != 30867599532700]
    custom_fields.append({"id": 30867599532700, "value": source_tag})

    if custom_fields:
        payload["custom_fields"] = custom_fields

    # Final invariant check
    if payload["external_id"] != case_id:
        raise RuntimeError(f"Payload external_id mismatch: expected '{case_id}', got '{payload['external_id']}'")

    audit = {
        "salesforce_case_id": case_id,
        "case_number": case_number,
        "customer_email": customer_email,
        "requester_id": requester_id,
        "status": ticket_status,
        "description_source": desc_source,
        "comment_count": len(comments),
        "custom_fields_count": len(custom_fields),
    }

    return payload, audit
