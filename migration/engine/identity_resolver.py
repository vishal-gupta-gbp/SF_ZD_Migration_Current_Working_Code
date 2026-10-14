"""Identity discovery and deduplication for Organizations and End Users."""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional, Set

EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])([A-Za-z0-9._%+\-]+\s*@\s*[A-Za-z0-9.-]+\s*\.\s*[A-Za-z]{2,})(?![A-Za-z0-9._%+\-])"
)


def sha12(value: str) -> str:
    """Return 12-character SHA256 hex digest."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def s(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def norm(val: Any) -> str:
    return s(val).strip().lower()


def valid_email(value: Any) -> str:
    v = re.sub(r"\s+", "", s(value).strip().lower())
    if not v or "@" not in v:
        return ""
    match = EMAIL_RE.fullmatch(v)
    return re.sub(r"\s+", "", match.group(1).lower()) if match else ""


def extract_emails(text: str) -> List[str]:
    return [re.sub(r"\s+", "", m.group(1).lower()) for m in EMAIL_RE.finditer(text or "")]


def first_email(value: Any) -> str:
    emails = extract_emails(s(value))
    return emails[0] if emails else valid_email(value)


def customer_source_email(
    case: Dict[str, Any],
    contact: Optional[Dict[str, Any]],
    case_messages: List[Dict[str, Any]],
    protected_agent_emails: Set[str],
) -> str:
    candidates = []
    if contact:
        candidates.append(contact.get("email") or contact.get("Email"))
    candidates.extend([
        case.get("contact_email") or case.get("ContactEmail"),
        case.get("supplied_email") or case.get("SuppliedEmail"),
    ])
    for msg in case_messages:
        incoming = msg.get("incoming") if "incoming" in msg else msg.get("Incoming")
        if incoming:
            candidates.append(msg.get("from_address") or msg.get("FromAddress"))

    for candidate in candidates:
        email = first_email(candidate)
        if email and email not in protected_agent_emails:
            return email
    return ""


def user_name(case: Dict[str, Any], contact: Optional[Dict[str, Any]]) -> str:
    if contact and s(contact.get("name") or contact.get("Name")):
        return s(contact.get("name") or contact.get("Name"))
    if s(case.get("supplied_name") or case.get("SuppliedName")):
        return s(case.get("supplied_name") or case.get("SuppliedName"))
    email = first_email(case.get("supplied_email") or case.get("SuppliedEmail"))
    case_num = s(case.get("case_number") or case.get("CaseNumber"))
    return email or f"Salesforce Customer {case_num}"


class IdentityResolver:
    """Discovers unique customer and organization identities from Salesforce records."""

    def __init__(self, synthetic_domain: str = "sfmigrateduser.com"):
        self.synthetic_domain = synthetic_domain

    def discover_identities(
        self,
        cases: List[Dict[str, Any]],
        contacts_by_id: Dict[str, Dict[str, Any]],
        emails_by_case: Dict[str, List[Dict[str, Any]]],
        protected_agent_emails: Set[str],
    ) -> Dict[str, Dict[str, Any]]:
        """Discover unique identities from Cases, Contacts, and incoming messages."""
        identities: Dict[str, Dict[str, Any]] = {}

        for case in cases:
            cid = s(case.get("id") or case.get("Id"))
            contact_id = s(case.get("contact_id") or case.get("ContactId"))
            contact = contacts_by_id.get(contact_id) if contact_id else None
            case_messages = emails_by_case.get(cid, [])
            source_email = customer_source_email(case, contact, case_messages, protected_agent_emails)

            phone = (
                s((contact or {}).get("phone") or (contact or {}).get("Phone"))
                or s((contact or {}).get("mobile_phone") or (contact or {}).get("MobilePhone"))
                or s(case.get("supplied_phone") or case.get("SuppliedPhone"))
            )
            account_id = s(case.get("account_id") or case.get("AccountId"))

            if valid_email(source_email):
                if contact_id:
                    identity_key = f"contact:{contact_id}"
                else:
                    identity_key = f"email:{norm(source_email)}"
                identities.setdefault(
                    identity_key,
                    {
                        "source_email": source_email,
                        "name": user_name(case, contact),
                        "account_id": account_id,
                        "phone": phone,
                        "salesforce_contact_id": contact_id,
                        "case_id": cid,
                        "is_synthetic": False,
                    },
                )
            else:
                # Deterministic synthetic requester per Case
                identity_key = f"case:{cid}"
                synthetic_email = f"sfcase_{sha12(cid)}@{self.synthetic_domain}"
                identities.setdefault(
                    identity_key,
                    {
                        "source_email": synthetic_email,
                        "name": user_name(case, contact),
                        "account_id": account_id,
                        "phone": phone,
                        "salesforce_contact_id": contact_id,
                        "case_id": cid,
                        "is_synthetic": True,
                    },
                )

            # Discover identities from incoming customer email messages
            for msg in case_messages:
                incoming = msg.get("incoming") if "incoming" in msg else msg.get("Incoming")
                if not incoming:
                    continue
                from_addr = first_email(msg.get("from_address") or msg.get("FromAddress"))
                if not valid_email(from_addr) or norm(from_addr) in protected_agent_emails:
                    continue

                msg_key = f"email:{norm(from_addr)}"
                identities.setdefault(
                    msg_key,
                    {
                        "source_email": from_addr,
                        "name": s(msg.get("from_name") or msg.get("FromName")) or "Salesforce Customer",
                        "account_id": account_id,
                        "phone": phone,
                        "salesforce_contact_id": contact_id,
                        "case_id": cid,
                        "is_synthetic": False,
                    },
                )

        return identities
