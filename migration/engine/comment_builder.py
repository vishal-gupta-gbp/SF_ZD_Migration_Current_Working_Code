"""Comment threading, HTML sanitization, signature formatting, and author resolution."""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from dateutil import parser as dtparser

from migration.engine.field_mapper import INTERNAL_COMMENT_FIELDS, normalize_label, s

SIGNATURE_IMAGE_LINK_RE = re.compile(
    r"\[(https?://[^\]\s]+)\]\s*<\s*(https?://[^>\s]+)\s*>",
    flags=re.I,
)
SIGNATURE_IMAGE_RE = re.compile(
    r"\[(https?://[^\]\s]+)\]",
    flags=re.I,
)
ANGLE_URL_RE = re.compile(
    r"<\s*(https?://[^>\s]+)\s*>",
    flags=re.I,
)
HTML_TAG_RE = re.compile(
    r"<\s*/?\s*(?:html|body|div|p|br|a|img|table|tbody|thead|tr|td|th|span|strong|b|i|u|ul|ol|li|font|style|blockquote|hr)\b",
    flags=re.I,
)


def has_real_html(value: str) -> bool:
    return bool(HTML_TAG_RE.search(value or ""))


def html_to_text(value: Any) -> str:
    raw = s(value)
    if not raw:
        return ""
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    raw = re.sub(r"</p\s*>", "\n", raw, flags=re.I)
    raw = re.sub(r"</div\s*>", "\n", raw, flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n[ \t]+", "\n", raw)
    return raw.strip()


def convert_signature_markup_in_html(value: str) -> str:
    raw = s(value)
    if not raw:
        return ""

    def image_link_repl(match: re.Match[str]) -> str:
        img_url = html.escape(match.group(1), quote=True)
        link_url = html.escape(match.group(2), quote=True)
        return f'<a href="{link_url}" target="_blank" rel="noopener noreferrer"><img src="{img_url}" alt="" /></a>'

    raw = SIGNATURE_IMAGE_LINK_RE.sub(image_link_repl, raw)

    def image_repl(match: re.Match[str]) -> str:
        img_url = html.escape(match.group(1), quote=True)
        return f'<img src="{img_url}" alt="" />'

    raw = SIGNATURE_IMAGE_RE.sub(image_repl, raw)

    def angle_url_repl(match: re.Match[str]) -> str:
        url = html.escape(match.group(1), quote=True)
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{html.escape(match.group(1))}</a>'

    raw = ANGLE_URL_RE.sub(angle_url_repl, raw)
    return raw


def plain_email_text_to_html(value: str) -> str:
    raw = s(value)
    if not raw:
        return ""

    placeholders: List[Tuple[str, str]] = []
    counter = 0

    def image_link_placeholder(match: re.Match[str]) -> str:
        nonlocal counter
        token = f"___ZD_SIG_{counter}___"
        counter += 1
        img_url = html.escape(match.group(1), quote=True)
        link_url = html.escape(match.group(2), quote=True)
        placeholders.append((token, f'<a href="{link_url}" target="_blank" rel="noopener noreferrer"><img src="{img_url}" alt="" /></a>'))
        return token

    raw = SIGNATURE_IMAGE_LINK_RE.sub(image_link_placeholder, raw)

    def image_placeholder(match: re.Match[str]) -> str:
        nonlocal counter
        token = f"___ZD_SIG_{counter}___"
        counter += 1
        img_url = html.escape(match.group(1), quote=True)
        placeholders.append((token, f'<img src="{img_url}" alt="" />'))
        return token

    raw = SIGNATURE_IMAGE_RE.sub(image_placeholder, raw)

    def angle_url_placeholder(match: re.Match[str]) -> str:
        nonlocal counter
        token = f"___ZD_SIG_{counter}___"
        counter += 1
        url = html.escape(match.group(1), quote=True)
        placeholders.append((token, f'<a href="{url}" target="_blank" rel="noopener noreferrer">{html.escape(match.group(1))}</a>'))
        return token

    raw = ANGLE_URL_RE.sub(angle_url_placeholder, raw)

    escaped = html.escape(raw, quote=False).replace("\r\n", "\n").replace("\r", "\n")
    escaped = escaped.replace("\n", "<br>\n")
    for token, fragment in placeholders:
        escaped = escaped.replace(token, fragment)
    return escaped


def prepare_zendesk_html_body(raw_html: str, raw_text: str) -> Tuple[str, str]:
    if raw_html and has_real_html(raw_html):
        return convert_signature_markup_in_html(raw_html), "HtmlBody"
    source = raw_text or raw_html
    if source:
        return plain_email_text_to_html(source), "TextBody/reconstructed"
    return "", ""


def parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = dtparser.parse(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def normalize_comment_body(value: Any) -> str:
    return re.sub(r"\s+", " ", s(value)).strip().lower()


def build_ticket_comments(
    case: Dict[str, Any],
    email_messages: List[Dict[str, Any]],
    customer_user_by_email: Dict[str, int],
    primary_customer_user_id: Optional[int],
    akarsh_user_id: int,
    initial_description_key: str = "",
    agent_user_by_sf_user_id: Optional[Dict[str, int]] = None,
    agent_user_by_email: Optional[Dict[str, int]] = None,
    phone_tasks: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Build chronological comments for Zendesk ticket import."""
    comments: List[Tuple[datetime, int, Dict[str, Any]]] = []
    sequence = 0
    seen_public_bodies: Set[str] = set()

    for msg in email_messages:
        # Skip private drafts
        if msg.get("is_private_draft") or msg.get("IsPrivateDraft"):
            continue

        raw_html = s(msg.get("html_body") or msg.get("HtmlBody"))
        raw_text = s(msg.get("text_body") or msg.get("TextBody"))
        html_body_prepared, _ = prepare_zendesk_html_body(raw_html, raw_text)
        display_body = raw_text or html_to_text(raw_html)

        if not display_body:
            continue

        body_key = normalize_comment_body(display_body)
        if initial_description_key and body_key == initial_description_key:
            continue
        if body_key in seen_public_bodies:
            continue

        incoming = bool(msg.get("incoming") if "incoming" in msg else msg.get("Incoming"))
        if incoming:
            from_addr = s(msg.get("from_address") or msg.get("FromAddress")).strip().lower()
            author_id = customer_user_by_email.get(from_addr, primary_customer_user_id) or akarsh_user_id
        else:
            sf_user_id = s(msg.get("created_by_id") or msg.get("CreatedById"))
            author_id = (agent_user_by_sf_user_id or {}).get(sf_user_id)
            if not author_id and agent_user_by_email:
                from_addr = s(msg.get("from_address") or msg.get("FromAddress")).strip().lower()
                author_id = agent_user_by_email.get(from_addr)
            if not author_id:
                author_id = akarsh_user_id

        created = (
            parse_dt(msg.get("message_date") or msg.get("MessageDate"))
            or parse_dt(msg.get("created_date") or msg.get("CreatedDate"))
            or datetime.now(timezone.utc)
        )

        comment_payload: Dict[str, Any] = {
            "author_id": int(author_id),
            "public": True,
            "_salesforce_email_message_id": s(msg.get("id") or msg.get("Id")),
        }
        if html_body_prepared:
            comment_payload["html_body"] = html_body_prepared
        else:
            comment_payload["value"] = raw_text

        if created <= datetime.now(timezone.utc):
            comment_payload["created_at"] = created.strftime("%Y-%m-%dT%H:%M:%SZ")

        comments.append((created, sequence, comment_payload))
        seen_public_bodies.add(body_key)
        sequence += 1

    # Internal comment fields
    case_created = parse_dt(case.get("created_date") or case.get("CreatedDate")) or datetime.now(timezone.utc)
    for field_name in INTERNAL_COMMENT_FIELDS:
        val = s(case.get(field_name))
        if val:
            comments.append((
                case_created,
                sequence,
                {
                    "author_id": int(akarsh_user_id),
                    "created_at": case_created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "public": False,
                    "value": val,
                },
            ))
            sequence += 1

    # Phone call recording URLs from Tasks
    if phone_tasks:
        recording_urls: List[str] = []
        recording_tasks: List[Tuple[str, str]] = []
        url_re = re.compile(r"https?://[^\s<>\"']+", flags=re.I)

        for task in phone_tasks:
            desc = s(task.get("description") or task.get("Description"))
            task_urls = [m.rstrip(",.;:)]}") for m in url_re.findall(desc)]
            # Also check task recording_urls_json if present
            task_rec_json = task.get("recording_urls_json")
            if isinstance(task_rec_json, list):
                task_urls.extend(task_rec_json)

            for u in task_urls:
                if u and u not in recording_urls:
                    recording_urls.append(u)
            if task_urls:
                subject = s(task.get("subject") or task.get("Subject")) or "Phone Call"
                tid = s(task.get("id") or task.get("Id"))
                recording_tasks.append((subject, tid))

        if recording_urls:
            lines = [
                "Salesforce Phone Call Recording(s)",
                "",
                "The following call recording URL(s) were found on Salesforce Task records for this phone-origin Case:",
            ]
            for u in recording_urls:
                lines.append(f"- {u}")
            if recording_tasks:
                lines.append("")
                lines.append("Task reference:")
                for sub, tid in recording_tasks:
                    lines.append(f"- {sub} (Task {tid})")

            comments.append((
                case_created,
                sequence,
                {
                    "author_id": int(akarsh_user_id),
                    "public": False,
                    "value": "\n".join(lines),
                    "_source": "Salesforce Task call recording",
                },
            ))
            sequence += 1

    comments.sort(key=lambda x: (x[0], x[1]))
    return [c for _, _, c in comments]
