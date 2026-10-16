#!/usr/bin/env python3
"""
Salesforce UAT -> Zendesk Sandbox migration (S3 + historical email migration)

This version uses a separate S3 preflight/download mapping:
  prepare-s3: download all Case-linked S3 files locally and create S3_Attachment_Mapping.csv.
  migrate: read that mapping, upload local files to Zendesk, and attach them to imported comments.
  all: export Salesforce, run the S3 preflight, then migrate; failed S3 attachments are logged and do not block ticket migration.

Modes:
  export   : Salesforce API -> local CSV export only. No Zendesk writes.
  validate : Build Zendesk payloads from the local CSV export. No Zendesk writes.
  migrate  : Create/update End Users + Organizations and import 100 tickets with comments/attachments.
  all      : Export from Salesforce, validate locally, then migrate to Zendesk.

Important rules for this test:
  * MAX_TICKETS defaults to 100 for safe testing. Use --max-tickets 0 for all Cases in the selected date range.
  * Only Akarsh Shrivastava is used as the Zendesk author for agent-side comments.
  * Original Salesforce customer/end-user emails are used as the Zendesk requester/end-user emails.
  * Agent emails are not changed. Zendesk comment authorship is set with author_id.
  * EmailMessage.Incoming=True  -> customer/end-user author.
  * EmailMessage.Incoming=False -> Akarsh author.
  * EmailMessage.IsPrivateDraft=True records are skipped.
  * EmailMessage.HtmlBody is preserved as Zendesk comment html_body when available; TextBody is the fallback.
  * Case.Description is used as the ticket description when present (required by Ticket Import).
  * Mapped internal Case text fields become private comments authored by Akarsh.
  * Salesforce BP_S3__c files are pre-downloaded with the client-provided AWS credentials.
  * S3_Attachment_Mapping.csv records Case ID, source comment/email ID when available, local file path,
    download status, and all preflight errors before any Zendesk ticket import begins.
  * During migration, successful local files are uploaded to Zendesk and attached to the matching
    historical EmailMessage comment when a Salesforce comment ID can be resolved; otherwise they are
    attached to a case-level internal note.
  * S3 preflight/download failures never stop ticket migration. Failed attachments are logged
    in S3_Attachment_Failure_Log.xlsx and their attempted S3 URL(s) are added to a private ticket note.
  * Field/value mapping is read from Salesforce_to_Zendesk_Case_Field_Mapping.xlsx.

The script is designed for the current 100-ticket sandbox test. It deliberately stops
at 100 tickets so it cannot accidentally run a full migration.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import mimetypes
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

import requests
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError, BotoCoreError
from dateutil import parser as dtparser
from openpyxl import load_workbook, Workbook
from openpyxl.styles import Font, PatternFill, Alignment


import sys

limit = sys.maxsize
while True:
    try:
        csv.field_size_limit(limit)
        break
    except OverflowError:
        limit //= 10

# -----------------------------------------------------------------------------
# Fixed test controls
# -----------------------------------------------------------------------------
MAX_TICKETS = 0
API_VERSION = "v59.0"
SF_BASE_URL_DEFAULT = "https://bayport--uatsandbox.sandbox.my.salesforce.com"
AGENT_COMMENT_EMAIL = "akarsh.shrivastava@groupbayport.com"
BATCH_SIZE = 100  # maximum ticket objects per Zendesk bulk-import request

# Zendesk fields required by the current Salesforce -> Zendesk mapping rules.
ASSOCIATION_TYPE_FIELD_ID = 30064365242012
GROUP_CUSTOM_FIELD_ID = 30114110837404
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
EXPORT_ID_BATCH_SIZE = 200  # IDs per related-object SOQL query
ZENDESK_ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024
S3_LOCAL_ROOT_NAME = "s3_downloads"
DELETE_LOCAL_S3_FILE_AFTER_UPLOAD = False
S3_MAPPING_FILENAME = "S3_Attachment_Mapping.csv"
S3_PREFLIGHT_SUMMARY_FILENAME = "S3_Attachment_Preflight_Summary.json"
S3_MIGRATION_REPORT_FILENAME = "S3_Attachment_Migration_Report.csv"
S3_FAILURE_EXCEL_FILENAME = "S3_Attachment_Failure_Log.xlsx"
SYNTHETIC_USER_DOMAIN = "sfmigrateduser.com"

EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])([A-Za-z0-9._%+\-]+\s*@\s*[A-Za-z0-9.-]+\s*\.\s*[A-Za-z]{2,})(?![A-Za-z0-9._%+\-])"
)

SYSTEM_ONLY_TARGETS = {
    "Requester (system)",
    "Organization (system)",
    "Zendesk Ticket external_id (Import API)",
    "Initial ticket public comment",
    "Historical ticket comments",
    "Ticket internal note/comment",
    "Ticket created_at (Import API)",
    "Comment/ticket source author reference",
    "Assignee/Group (system)",
}

INTERNAL_COMMENT_FIELDS = [
    "BP_Internal_Comments__c",
    "BP_Remarks__c",
    "BP_Resolution_Notes__c",
    "BP_Case_Notes__c",
    "BP_Transcripts__c",
    "GM_Voicemail_Transcript__c",
]


# -----------------------------------------------------------------------------
# Basic helpers
# -----------------------------------------------------------------------------


def modular_excel_write(path: Path, rows: List[Dict[str, Any]], headers: Optional[List[str]] = None, sheet_name: str = "Sheet1") -> Path:
    """Write migration rows to a durable XLSX file.

    This helper is intentionally local to migration_core so every stage uses the
    same Excel writer. It never assumes that a worksheet already exists.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = (sheet_name or "Sheet1")[:31]

    rows = rows or []
    if headers is not None:
        headers = [str(h) for h in headers]
    else:
        headers = []
        for row in rows:
            if isinstance(row, dict):
                for key in row.keys():
                    key = str(key)
                    if key not in headers:
                        headers.append(key)

    if not headers:
        headers = ["Message"]
        rows = [{"Message": "No records"}]

    ws.append(headers)
    for row in rows:
        if not isinstance(row, dict):
            row = {"Message": str(row)}
        ws.append([row.get(h, "") for h in headers])

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for column_cells in ws.columns:
        max_len = 0
        for cell in column_cells:
            value = "" if cell.value is None else str(cell.value)
            max_len = max(max_len, min(len(value), 60))
        ws.column_dimensions[column_cells[0].column_letter].width = max(12, min(max_len + 2, 62))

    wb.save(path)
    return path


def modular_excel_read(path: Path, sheet_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read an XLSX sheet into dictionaries using the first row as headers."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Excel mapping file not found: {path}")
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet_name] if sheet_name else wb.active
        values = ws.iter_rows(values_only=True)
        try:
            raw_headers = next(values)
        except StopIteration:
            return []
        headers = [str(h).strip() if h is not None else "" for h in raw_headers]
        result = []
        for raw in values:
            if not any(v is not None and str(v).strip() for v in raw):
                continue
            result.append({headers[i]: raw[i] if i < len(raw) else None for i in range(len(headers)) if headers[i]})
        return result
    finally:
        wb.close()


def s(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def bool_value(value: Any) -> bool:
    return s(value).lower() in {"true", "1", "yes", "y"}


def parse_dt(value: Any) -> Optional[datetime]:
    if value is None or s(value) == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = dtparser.parse(s(value))
        except (ValueError, TypeError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(value: Any) -> Optional[str]:
    dt = parse_dt(value)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def norm(value: Any) -> str:
    return s(value).strip().lower()


def normalize_label(value: Any) -> str:
    return re.sub(r"\s+", " ", s(value)).strip().lower()


def safe_tag(value: Any) -> str:
    x = re.sub(r"[^A-Za-z0-9_-]+", "_", s(value).lower()).strip("_")
    return x[:100]


def sha12(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def flatten(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_json_compact(path: Path, data: Any) -> None:
    """Compatibility alias used by migration stages for compact JSON audit files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def limit_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply the global migration cap. 0 means no cap."""
    if MAX_TICKETS <= 0:
        return rows
    return rows[:MAX_TICKETS]


# -----------------------------------------------------------------------------
# CSV helpers
# -----------------------------------------------------------------------------
def write_csv(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["No records returned"])
        return

    headers: List[str] = []
    seen = set()
    for record in records:
        for key in record.keys():
            if key not in seen:
                headers.append(key)
                seen.add(key)

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            writer.writerow({h: flatten(record.get(h)) for h in headers})


def read_csv(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing CSV file: {path}")
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = []
        if reader.fieldnames == ["No records returned"]:
            return []
        for row in reader:
            if any(s(v) for v in row.values()):
                rows.append(dict(row))
        return rows


# -----------------------------------------------------------------------------
# Salesforce REST client
# -----------------------------------------------------------------------------
class SalesforceClient:
    def __init__(self, base_url: str, client_id: str, client_secret: str):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = ""
        self.instance_url = self.base_url
        self.session = requests.Session()

    def authenticate(self) -> None:
        response = self.session.post(
            f"{self.base_url}/services/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=60,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Salesforce authentication failed: HTTP {response.status_code}\n{response.text[:3000]}"
            )
        data = response.json()
        self.access_token = data["access_token"]
        self.instance_url = data.get("instance_url", self.base_url).rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        url = path if path.startswith("http") else f"{self.instance_url}{path}"

        for attempt in range(1, 7):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    timeout=90,
                )
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 6:
                    raise
                time.sleep(min(60, 2 ** (attempt - 1)))
                continue

            if response.status_code == 401 and attempt == 1:
                self.authenticate()
                continue

            api_usage = response.headers.get("Sforce-Limit-Info", "")
            if api_usage and "api-usage=" in api_usage:
                # Example: api-usage=18/15000
                try:
                    usage = api_usage.split("api-usage=", 1)[1].split(",", 1)[0]
                    used, maximum = usage.split("/", 1)
                    if int(maximum) and int(used) / int(maximum) >= 0.90:
                        print(f"WARNING: Salesforce API daily usage is high: {used}/{maximum}")
                except (ValueError, IndexError):
                    pass

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < 6:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(60, 2 ** (attempt - 1))
                except ValueError:
                    delay = min(60, 2 ** (attempt - 1))
                time.sleep(delay)
                continue

            return response

        raise RuntimeError("Salesforce request failed after retries")

    def describe_fields(self, object_name: str) -> set[str]:
        response = self.request(
            "GET",
            f"/services/data/{API_VERSION}/sobjects/{object_name}/describe",
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Salesforce describe failed for {object_name}: HTTP {response.status_code}\n{response.text[:3000]}"
            )
        return {s(f.get("name")) for f in response.json().get("fields", []) if s(f.get("name"))}

    def query(self, soql: str, progress_label: Optional[str] = None) -> List[Dict[str, Any]]:
        response = self.request(
            "GET",
            f"/services/data/{API_VERSION}/query",
            params={"q": soql},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Salesforce SOQL failed: HTTP {response.status_code}\n{response.text[:4000]}"
            )

        data = response.json()
        records = list(data.get("records", []))
        if progress_label:
            total = data.get("totalSize", "?")
            print(f"[Export] {progress_label}: {len(records):,}/{total} records received", flush=True)

        while not data.get("done", True):
            next_url = data.get("nextRecordsUrl")
            if not next_url:
                break
            response = self.request("GET", next_url)
            if response.status_code != 200:
                raise RuntimeError(
                    f"Salesforce pagination failed: HTTP {response.status_code}\n{response.text[:4000]}"
                )
            data = response.json()
            records.extend(data.get("records", []))
            if progress_label:
                total = data.get("totalSize", "?")
                print(f"[Export] {progress_label}: {len(records):,}/{total} records received", flush=True)

        return records


def quote_soql(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def soql_ids(ids: Iterable[str]) -> str:
    return ",".join(quote_soql(x) for x in ids)


def chunks(values: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(values), size):
        yield values[i:i + size]


def progress_chunks(values: List[str], size: int, label: str) -> Iterable[List[str]]:
    batches = list(chunks(values, size))
    for index, batch in enumerate(batches, start=1):
        print(f"[Export] {label}: batch {index:,}/{len(batches):,}", flush=True)
        yield batch


# -----------------------------------------------------------------------------
# S3 download / attachment helpers
# -----------------------------------------------------------------------------
def normalize_s3_url(url: str) -> str:
    """Normalize an S3 reference without environment-specific host rewriting."""
    raw = s(url)
    if not raw:
        return ""
    return raw


def is_supported_s3_url(url: str) -> bool:
    """Return True only for URL forms that can be resolved to an S3 bucket/key."""
    raw = s(url)
    if not raw:
        return False

    try:
        parsed = urlparse(normalize_s3_url(raw))
    except Exception:
        return False

    if parsed.scheme.lower() == "s3" and parsed.netloc:
        return True

    host = parsed.netloc.split(":", 1)[0].lower()
    host_parts = host.split(".")
    if host_parts and host_parts[0] == "s3" and len(host_parts) >= 3:
        return True
    if "s3" in host_parts:
        return True
    return False


def s3_url_candidates(record: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return usable S3 URL candidates in priority order.

    BP_S3_URL__c is preferred. BP_Fallback_URL__c is used only when it is
    itself an S3 URL, because Salesforce ContentDistribution URLs are not
    directly parseable as bucket/key URLs.
    """
    candidates: List[Tuple[str, str]] = []
    seen: set[str] = set()

    for source_field in ("BP_S3_URL__c", "BP_Fallback_URL__c"):
        raw = s(record.get(source_field))
        if not raw:
            continue
        normalized = normalize_s3_url(raw)
        if not normalized or normalized in seen:
            continue
        if is_supported_s3_url(normalized):
            candidates.append((source_field, normalized))
            seen.add(normalized)

    return candidates


def parse_s3_url(url: str) -> Tuple[str, str]:
    """Extract bucket and object key using the proven standalone logic."""
    parsed = urlparse(url)

    if parsed.scheme == "s3":
        return parsed.netloc, unquote(parsed.path.lstrip("/"))

    netloc_parts = parsed.netloc.split(".")

    if netloc_parts[0] == "s3":
        path_parts = unquote(parsed.path.lstrip("/")).split("/", 1)
        return path_parts[0], path_parts[1] if len(path_parts) > 1 else ""

    # Format: bucket.s3.region.amazonaws.com/key
    return netloc_parts[0], unquote(parsed.path.lstrip("/"))


def safe_local_filename(filename: str, prefix: str = "") -> str:
    base = Path(filename or "s3_file").name
    base = re.sub(r"[^A-Za-z0-9._()\- ]+", "_", base).strip(" .") or "s3_file"
    if prefix:
        base = f"{prefix}_{base}"
    # Keep the Windows/local path comfortably below common path-length limits.
    return base[:180]


class S3Downloader:
    """S3 downloader using the exact logic proven to work independently."""

    def __init__(self, access_key_id: str, secret_access_key: str, region: str = "us-east-1"):
        if not access_key_id or not secret_access_key:
            raise RuntimeError(
                "AWS credentials are required. Set AWS_ACCESS_KEY_ID and "
                "AWS_SECRET_ACCESS_KEY in environment variables."
            )

        self.region = region or "us-east-1"
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.client = boto3.client(
            "s3",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=self.region,
        )

    def download(self, url: str, local_path: Path, transfer_config: Optional[TransferConfig] = None) -> Dict[str, Any]:
        """Download a private S3 object using the proven direct boto3 call."""
        bucket, key = parse_s3_url(url)
        if not bucket or not key:
            raise RuntimeError(f"Invalid S3 URL: {url}")

        local_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self.client.download_file(
                Bucket=bucket,
                Key=key,
                Filename=str(local_path),
                Config=transfer_config,
            )
        except ClientError as exc:
            raise RuntimeError(
                f"S3 download failed for {url}: {exc}"
            ) from exc
        except (BotoCoreError, OSError) as exc:
            raise RuntimeError(
                f"S3 download failed for {url}: {exc}"
            ) from exc

        if not local_path.exists():
            raise RuntimeError(
                f"S3 download completed but local file was not created: {local_path}"
            )

        return {
            "bucket": bucket,
            "key": key,
            "size": local_path.stat().st_size,
            "content_type": mimetypes.guess_type(local_path.name)[0] or "application/octet-stream",
            "etag": "",
        }


def get_aws_config(cfg: Dict[str, Any]) -> Tuple[str, str, str]:
    aws_cfg = cfg.get("aws", {}) if isinstance(cfg, dict) else {}
    access_key = s(os.getenv("AWS_ACCESS_KEY_ID")) or s(aws_cfg.get("access_key_id"))
    secret_key = s(os.getenv("AWS_SECRET_ACCESS_KEY")) or s(aws_cfg.get("secret_access_key"))
    region = s(os.getenv("AWS_REGION")) or s(aws_cfg.get("region")) or "us-east-1"
    return access_key, secret_key, region


SF_ID_RE = re.compile(r"(?<![A-Za-z0-9])(02s|500)[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?(?![A-Za-z0-9])")


def extract_salesforce_id(value: Any, prefixes: Tuple[str, ...] = ("02s", "500")) -> str:
    raw = s(value)
    if not raw:
        return ""
    for match in SF_ID_RE.finditer(raw):
        if match.group(1) in prefixes:
            return match.group(0)
    compact = re.sub(r"\s+", "", raw)
    if compact.startswith(prefixes) and len(compact) in {15, 18}:
        return compact
    return ""


def extract_comment_id(record: Dict[str, Any]) -> Tuple[str, str]:
    # Preferred source: BP_RecordId__c when it contains an EmailMessage (02s...) ID.
    record_id = extract_salesforce_id(record.get("BP_RecordId__c"), prefixes=("02s",))
    if record_id:
        return record_id, "BP_RecordId__c"

    # Fallback: many S3 object names contain the Salesforce EmailMessage ID.
    for candidate in (record.get("Name"), record.get("BP_S3_URL__c"), record.get("BP_Fallback_URL__c")):
        found = extract_salesforce_id(candidate, prefixes=("02s",))
        if found:
            return found, "filename/url"
    return "", ""


def load_s3_mapping_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return read_csv(path)


def write_s3_preflight_summary(path: Path, summary: Dict[str, Any]) -> None:
    save_json(path, summary)


def best_s3_failure_url(row: Dict[str, Any]) -> str:
    """Return the most useful S3 URL for a failure log/internal note."""
    for field in ("S3_URL", "Attempted_S3_URLs", "Original_S3_URL", "Fallback_S3_URL"):
        value = s(row.get(field))
        if value:
            return value.split(" | ")[0].strip()
    return ""


def s3_failure_note_text(failed_rows: List[Dict[str, Any]]) -> str:
    """Build a private/internal note for failed attachment export/upload rows."""
    lines = [
        "S3 Attachment Migration Notice",
        "",
        "The following Salesforce attachment(s) could not be exported/downloaded or uploaded, so they were not attached to the ticket.",
        "The attachment URL attempted during migration is listed below.",
        "",
    ]
    for index, row in enumerate(failed_rows, start=1):
        filename = s(row.get("File_Name")) or "Unknown file"
        url = best_s3_failure_url(row)
        error = s(row.get("Error")) or s(row.get("S3_Attempt_Errors")) or "Unknown attachment error"
        lines.append(f"{index}. File: {filename}")
        lines.append(f"   Attachment URL: {url or 'Not available'}")
        lines.append(f"   Error: {error}")
        lines.append("")
    return "\n".join(lines).strip()


def write_s3_failure_excel(
    output_dir: Path,
    mapping_rows: List[Dict[str, Any]],
    extra_failure_rows: Optional[List[Dict[str, Any]]] = None,
) -> Path:
    """Write a durable Excel log of S3 export/download and Zendesk upload failures."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / S3_FAILURE_EXCEL_FILENAME

    combined: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str, str, str]] = set()
    for source_row in list(mapping_rows or []) + list(extra_failure_rows or []):
        preflight_status = s(source_row.get("Preflight_Status"))
        upload_status = s(source_row.get("Zendesk_Upload_Status"))
        if preflight_status and preflight_status != "success":
            failure_stage = "S3 Export / Download"
        elif upload_status == "failed":
            failure_stage = "Zendesk Attachment Upload"
        else:
            continue

        key = (
            s(source_row.get("S3_Record_ID")),
            s(source_row.get("Salesforce_Case_ID")),
            s(source_row.get("File_Name")),
            failure_stage,
        )
        if key in seen:
            continue
        seen.add(key)

        combined.append({
            "Failure Stage": failure_stage,
            "Salesforce Case ID": s(source_row.get("Salesforce_Case_ID")),
            "Salesforce Case Number": s(source_row.get("Salesforce_Case_Number")),
            "Salesforce Comment ID": s(source_row.get("Salesforce_Comment_ID")),
            "S3 Record ID": s(source_row.get("S3_Record_ID")),
            "File Name": s(source_row.get("File_Name")),
            "Attachment URL": best_s3_failure_url(source_row),
            "Original S3 URL": s(source_row.get("Original_S3_URL")),
            "Fallback S3 URL": s(source_row.get("Fallback_S3_URL")),
            "Attempted S3 URLs": s(source_row.get("Attempted_S3_URLs")),
            "Error": s(source_row.get("Error")) or s(source_row.get("S3_Attempt_Errors")),
            "Download Status": s(source_row.get("Download_Status")),
            "Preflight Status": preflight_status,
            "Zendesk Upload Status": upload_status,
            "Logged At UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        })

    wb = Workbook()
    ws = wb.active
    ws.title = "Failed Attachments"
    headers = [
        "Failure Stage", "Salesforce Case ID", "Salesforce Case Number",
        "Salesforce Comment ID", "S3 Record ID", "File Name",
        "Attachment URL", "Original S3 URL", "Fallback S3 URL",
        "Attempted S3 URLs", "Error", "Download Status",
        "Preflight Status", "Zendesk Upload Status", "Logged At UTC",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9EAF7")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for row in combined:
        ws.append([row.get(h, "") for h in headers])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    widths = [24,20,22,22,20,42,70,70,70,90,80,18,18,24,24]
    for i, width in enumerate(widths, start=1):
        from openpyxl.utils import get_column_letter
        ws.column_dimensions[get_column_letter(i)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    summary = wb.create_sheet("Summary")
    summary["A1"] = "S3 Attachment Failure Log"
    summary["A1"].font = Font(bold=True, size=14)
    summary["A3"] = "Total failures logged"
    summary["B3"] = len(combined)
    summary["A4"] = "S3 export/download failures"
    summary["B4"] = sum(1 for r in combined if r["Failure Stage"] == "S3 Export / Download")
    summary["A5"] = "Zendesk attachment upload failures"
    summary["B5"] = sum(1 for r in combined if r["Failure Stage"] == "Zendesk Attachment Upload")
    summary["A7"] = "Behavior"
    summary["B7"] = "Ticket migration continues when attachment export/download fails. Failed attachment URLs are added to private/internal notes."
    summary.column_dimensions["A"].width = 40
    summary.column_dimensions["B"].width = 110
    summary["B7"].alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(path)
    return path


def prepare_s3_attachment_mapping(
    source_dir: Path,
    output_dir: Path,
    aws_access_key_id: str,
    aws_secret_access_key: str,
    aws_region: str = "us-east-1",
) -> Dict[str, Any]:
    """Download all Case-linked S3 objects locally and create a durable mapping CSV.

    This stage performs no Zendesk ticket imports or attachment uploads.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    mapping_path = output_dir / S3_MAPPING_FILENAME
    summary_path = output_dir / S3_PREFLIGHT_SUMMARY_FILENAME

    s3_records_path = source_dir / "S3Records.csv"
    cases_path = source_dir / "Cases.csv"
    if not s3_records_path.exists():
        raise RuntimeError(f"Missing {s3_records_path}. Run --mode export first.")

    cases = limit_rows(read_csv(cases_path)) if cases_path.exists() else []
    selected_case_ids = {s(c.get("Id")) for c in cases if s(c.get("Id"))}
    all_records = read_csv(s3_records_path)
    records = [r for r in all_records if not selected_case_ids or s(r.get("BP_Case__c")) in selected_case_ids]

    print()
    print("S3 preflight/download stage")
    print(f"S3 records found: {len(records)}")

    if not records:
        rows: List[Dict[str, Any]] = []
        write_csv(mapping_path, rows)
        summary = {
            "mapping_file": str(mapping_path),
            "total_s3_records": 0,
            "download_success": 0,
            "download_failed": 0,
            "preflight_failed": 0,
            "preflight_passed": 0,
            "status": "success",
        }
        write_s3_preflight_summary(summary_path, summary)
        print("No S3 attachments found for the selected Cases.")
        return summary

    downloader = S3Downloader(aws_access_key_id, aws_secret_access_key, aws_region)
    root = output_dir / S3_LOCAL_ROOT_NAME
    root.mkdir(parents=True, exist_ok=True)

    rows: List[Dict[str, Any]] = []
    downloaded_by_url: Dict[str, Dict[str, Any]] = {}

    for index, record in enumerate(records, start=1):
        s3_record_id = s(record.get("Id"))
        case_id = s(record.get("BP_Case__c"))
        case_number = ""
        filename = s(record.get("Name"))
        original_url = s(record.get("BP_S3_URL__c"))
        fallback_url = s(record.get("BP_Fallback_URL__c"))
        comment_id, comment_id_source = extract_comment_id(record)
        bucket = ""
        key = ""
        local_file = ""
        content_type = ""
        size_bytes = ""
        sha256 = ""
        download_status = "failed"
        preflight_status = "failed"
        error = ""
        note = ""
        url = ""
        url_source = ""
        attempted_urls: List[str] = []
        attempt_errors: List[str] = []

        candidates = s3_url_candidates(record)

        if not candidates:
            if original_url:
                error = "BP_S3_URL__c is not a supported S3 URL"
            elif fallback_url:
                error = "BP_S3_URL__c is blank and BP_Fallback_URL__c is not a supported S3 URL"
            else:
                error = "BP_S3_URL__c and BP_Fallback_URL__c are blank"
        else:
            display_name = safe_local_filename(filename or f"s3_{s3_record_id}")
            case_folder = safe_local_filename(case_id or "case")
            local_path = root / case_folder / safe_local_filename(
                display_name,
                prefix=sha12(s3_record_id or candidates[0][1])[-8:],
            )
            local_path.parent.mkdir(parents=True, exist_ok=True)

            print(f"  [{index}/{len(records)}] Case {case_id} | {display_name}")

            for candidate_source, candidate_url in candidates:
                attempted_urls.append(candidate_url)
                try:
                    candidate_bucket, candidate_key = parse_s3_url(candidate_url)
                    print(f"      Trying {candidate_source}: s3://{candidate_bucket}/{candidate_key}")

                    if candidate_url in downloaded_by_url and downloaded_by_url[candidate_url].get("download_status") == "success":
                        prior = downloaded_by_url[candidate_url]
                        candidate_local_path = Path(prior["local_file"])
                        metadata = prior
                        note = "Duplicate S3 URL; reused previous local download."
                    else:
                        metadata = downloader.download(candidate_url, candidate_local_path := local_path)
                        downloaded_by_url[candidate_url] = {
                            "local_file": str(candidate_local_path),
                            "download_status": "success",
                            "size": metadata.get("size", 0),
                            "content_type": metadata.get("content_type", ""),
                            "sha256": "",
                        }

                    if not candidate_local_path.exists():
                        raise RuntimeError(f"Downloaded file does not exist: {candidate_local_path}")

                    candidate_size = int(candidate_local_path.stat().st_size)
                    candidate_content_type = (
                        s(metadata.get("content_type"))
                        or mimetypes.guess_type(display_name)[0]
                        or "application/octet-stream"
                    )
                    candidate_sha256 = hashlib.sha256()
                    with candidate_local_path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                            candidate_sha256.update(chunk)
                    candidate_sha256 = candidate_sha256.hexdigest()

                    if candidate_size > ZENDESK_ATTACHMENT_MAX_BYTES:
                        raise RuntimeError(
                            f"File exceeds Zendesk 50 MB limit: {candidate_size} bytes"
                        )

                    url = candidate_url
                    url_source = candidate_source
                    bucket = candidate_bucket
                    key = candidate_key
                    local_file = str(candidate_local_path)
                    size_bytes = candidate_size
                    content_type = candidate_content_type
                    sha256 = candidate_sha256
                    download_status = "success"
                    preflight_status = "success"

                    downloaded_by_url[candidate_url].update({
                        "size": candidate_size,
                        "content_type": candidate_content_type,
                        "sha256": candidate_sha256,
                    })
                    break

                except Exception as exc:
                    attempt_errors.append(f"{candidate_source}: {exc}")
                    # Remove a partial file before trying the next candidate.
                    try:
                        if local_path.exists() and download_status != "success":
                            local_path.unlink()
                    except OSError:
                        pass

            if preflight_status != "success":
                error = " | ".join(attempt_errors) or "All S3 download candidates failed"

        rows.append({
            "Salesforce_Case_ID": case_id,
            "Salesforce_Case_Number": case_number,
            "Salesforce_Comment_ID": comment_id,
            "Comment_ID_Source": comment_id_source,
            "S3_Record_ID": s3_record_id,
            "File_Name": filename or (Path(unquote(key)).name if key else ""),
            "Original_S3_URL": original_url,
            "Fallback_S3_URL": fallback_url if is_supported_s3_url(fallback_url) else "",
            "S3_URL": url,
            "S3_URL_Source": url_source,
            "Attempted_S3_URLs": " | ".join(attempted_urls),
            "S3_Attempt_Errors": " | ".join(attempt_errors),
            "Bucket": bucket,
            "Object_Key": key,
            "Local_File_Path": local_file,
            "Content_Type": content_type,
            "File_Size_Bytes": size_bytes,
            "SHA256": sha256,
            "Download_Status": download_status,
            "Preflight_Status": preflight_status,
            "Zendesk_Upload_Status": "not_started",
            "Zendesk_Attachment_Status": "not_started",
            "Error": error,
            "Note": note,
        })

    write_csv(mapping_path, rows)
    failure_excel_path = write_s3_failure_excel(output_dir, rows)
    failed = [r for r in rows if s(r.get("Preflight_Status")) != "success"]
    success = [r for r in rows if s(r.get("Preflight_Status")) == "success"]
    summary = {
        "mapping_file": str(mapping_path),
        "total_s3_records": len(rows),
        "download_success": sum(1 for r in rows if s(r.get("Download_Status")) == "success"),
        "download_failed": sum(1 for r in rows if s(r.get("Download_Status")) != "success"),
        "preflight_passed": len(success),
        "preflight_failed": len(failed),
        "status": "failed" if failed else "success",
        "failed_files": [
            {
                "case_id": r.get("Salesforce_Case_ID", ""),
                "comment_id": r.get("Salesforce_Comment_ID", ""),
                "file_name": r.get("File_Name", ""),
                "error": r.get("Error", ""),
            }
            for r in failed
        ],
    }
    write_s3_preflight_summary(summary_path, summary)

    print()
    print("S3 PREFLIGHT SUMMARY")
    print(f"  Total attachments : {summary['total_s3_records']}")
    print(f"  Download successful: {summary['download_success']}")
    print(f"  Download failed   : {summary['download_failed']}")
    print(f"  Preflight failed  : {summary['preflight_failed']}")
    print(f"  Mapping file      : {mapping_path}")
    print(f"  Summary file      : {summary_path}")
    print(f"  Failure Excel     : {failure_excel_path}")
    return summary


# -----------------------------------------------------------------------------
# Zendesk REST client
# -----------------------------------------------------------------------------
class ZendeskClient:
    def __init__(self, subdomain: str, client_id: str, client_secret: str, scope: str):
        self.base_url = f"https://{subdomain}.zendesk.com"
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.access_token = ""
        self.session = requests.Session()
        self._groups_cache: Optional[List[Dict[str, Any]]] = None
        self._brands_cache: Optional[List[Dict[str, Any]]] = None

    def authenticate(self) -> None:
        response = self.session.post(
            f"{self.base_url}/oauth/tokens",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": self.scope,
            },
            timeout=60,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Zendesk OAuth authentication failed: HTTP {response.status_code}\n{response.text[:3000]}"
            )
        self.access_token = response.json()["access_token"]

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> requests.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        for attempt in range(1, 7):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    timeout=90,
                )
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 6:
                    raise
                time.sleep(min(60, 2 ** (attempt - 1)))
                continue

            if response.status_code == 401 and attempt == 1:
                self.authenticate()
                continue

            try:
                remaining = int(response.headers.get("ratelimit-remaining", "999999"))
                reset = int(response.headers.get("ratelimit-reset", "0"))
                if remaining <= 5 and reset > 0 and response.status_code not in RETRYABLE_STATUS_CODES:
                    time.sleep(reset + 1)
            except (TypeError, ValueError):
                pass

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < 6:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(60, 2 ** (attempt - 1))
                except ValueError:
                    delay = min(60, 2 ** (attempt - 1))
                time.sleep(delay)
                continue

            return response
        raise RuntimeError("Zendesk request failed after retries")

    def find_user_by_external_id(self, external_id: str) -> Optional[Dict[str, Any]]:
        response = self.request(
            "GET",
            "/api/v2/users.json",
            params={"external_id": external_id, "per_page": 100},
        )
        if response.status_code != 200:
            raise RuntimeError(f"Zendesk user lookup failed: HTTP {response.status_code}\n{response.text[:3000]}")
        users = response.json().get("users", [])
        return users[0] if users else None

    def find_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        response = self.request(
            "GET",
            "/api/v2/users/search.json",
            params={"query": email, "per_page": 100},
        )
        if response.status_code != 200:
            raise RuntimeError(f"Zendesk user email search failed: HTTP {response.status_code}\n{response.text[:3000]}")
        users = response.json().get("users", [])
        exact = [u for u in users if norm(u.get("email")) == norm(email)]
        if exact:
            return exact[0]
        return users[0] if users else None

    def _load_groups(self) -> List[Dict[str, Any]]:
        if self._groups_cache is not None:
            return self._groups_cache
        response = self.request("GET", "/api/v2/groups.json", params={"per_page": 100})
        if response.status_code != 200:
            raise RuntimeError(f"Zendesk group lookup failed: HTTP {response.status_code}\n{response.text[:3000]}")
        data = response.json()
        groups = data.get("groups", [])
        while data.get("next_page"):
            response = self.request("GET", data["next_page"])
            if response.status_code != 200:
                raise RuntimeError(f"Zendesk group pagination failed: HTTP {response.status_code}\n{response.text[:3000]}")
            data = response.json()
            groups.extend(data.get("groups", []))
        self._groups_cache = groups
        return groups

    def find_group_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        target = normalize_label(name)
        if not target:
            return None
        exact = [g for g in self._load_groups() if normalize_label(g.get("name")) == target]
        return exact[0] if exact else None

    def _load_brands(self) -> List[Dict[str, Any]]:
        if self._brands_cache is not None:
            return self._brands_cache
        response = self.request("GET", "/api/v2/brands.json", params={"per_page": 100})
        if response.status_code != 200:
            raise RuntimeError(f"Zendesk brand lookup failed: HTTP {response.status_code}\n{response.text[:3000]}")
        data = response.json()
        brands = data.get("brands", [])
        while data.get("next_page"):
            response = self.request("GET", data["next_page"])
            if response.status_code != 200:
                raise RuntimeError(f"Zendesk brand pagination failed: HTTP {response.status_code}\n{response.text[:3000]}")
            data = response.json()
            brands.extend(data.get("brands", []))
        self._brands_cache = brands
        return brands

    def find_brand_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        target = normalize_label(name)
        if not target:
            return None
        exact = [b for b in self._load_brands() if normalize_label(b.get("name")) == target]
        return exact[0] if exact else None

    def create_end_user(self, name: str, email: str, external_id: str, organization_id: Optional[int] = None, phone: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "user": {
                "name": name or email,
                "email": email,
                "external_id": external_id,
                "role": "end-user",
                "skip_verify_email": True,
            }
        }
        if organization_id:
            body["user"]["organization_id"] = organization_id
        if s(phone):
            body["user"]["phone"] = s(phone)
        response = self.request("POST", "/api/v2/users.json", json_body=body)
        if response.status_code not in {200, 201}:
            raise RuntimeError(f"Zendesk end user creation failed: HTTP {response.status_code}\n{response.text[:3000]}")
        return response.json().get("user", {})

    def find_or_create_end_user(
        self,
        name: str,
        customer_email: str,
        external_id: str,
        organization_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        existing = self.find_user_by_external_id(external_id)
        if existing:
            return existing
        existing = self.find_user_by_email(customer_email)
        if existing:
            return existing
        return self.create_end_user(name, customer_email, external_id, organization_id)

    def find_organization_by_external_id(self, external_id: str) -> Optional[Dict[str, Any]]:
        response = self.request(
            "GET",
            "/api/v2/organizations.json",
            params={"external_id": external_id, "per_page": 100},
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Zendesk organization lookup failed: HTTP {response.status_code}\n{response.text[:3000]}"
            )
        orgs = response.json().get("organizations", [])
        return orgs[0] if orgs else None

    def create_or_update_organization(self, name: str, external_id: str) -> Dict[str, Any]:
        payload = {
            "organization": {
                "name": name or f"Salesforce {external_id}",
                "external_id": external_id,
            }
        }
        response = self.request("POST", "/api/v2/organizations/create_or_update.json", json_body=payload)
        if response.status_code not in {200, 201}:
            raise RuntimeError(
                f"Zendesk organization create/update failed: HTTP {response.status_code}\n{response.text[:3000]}"
            )
        return response.json().get("organization", {})

    def upload_file(self, local_path: Path, attachment_filename: str, content_type: str) -> str:
        """Upload one local file to Zendesk and return its one-time upload token."""
        if not local_path.exists():
            raise FileNotFoundError(f"Local attachment not found: {local_path}")
        if local_path.stat().st_size > ZENDESK_ATTACHMENT_MAX_BYTES:
            raise RuntimeError(
                f"Zendesk attachment exceeds 50 MB limit: {attachment_filename} "
                f"({local_path.stat().st_size} bytes)"
            )

        url = f"{self.base_url}/api/v2/uploads.json"
        params = {"filename": attachment_filename}
        mime = content_type or mimetypes.guess_type(attachment_filename)[0] or "application/octet-stream"

        for attempt in range(1, 7):
            try:
                with local_path.open("rb") as handle:
                    response = self.session.post(
                        url,
                        params=params,
                        data=handle,
                        headers={
                            "Authorization": f"Bearer {self.access_token}",
                            "Content-Type": mime,
                            "Accept": "application/json",
                        },
                        timeout=180,
                    )
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 6:
                    raise
                time.sleep(min(60, 2 ** (attempt - 1)))
                continue

            if response.status_code == 401 and attempt == 1:
                self.authenticate()
                continue

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < 6:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(60, 2 ** (attempt - 1))
                except ValueError:
                    delay = min(60, 2 ** (attempt - 1))
                time.sleep(delay)
                continue

            if response.status_code != 201:
                raise RuntimeError(
                    f"Zendesk attachment upload failed for {attachment_filename}: "
                    f"HTTP {response.status_code}\n{response.text[:5000]}"
                )

            token = s(response.json().get("upload", {}).get("token"))
            if not token:
                raise RuntimeError(
                    f"Zendesk attachment upload returned no token for {attachment_filename}."
                )
            return token

        raise RuntimeError(f"Zendesk attachment upload failed after retries: {attachment_filename}")

    def upload_file_detailed(self, local_path: Path, attachment_filename: str, content_type: str) -> Dict[str, Any]:
        """Upload one file and return token plus attachment metadata for a later ticket import."""
        if not local_path.exists():
            raise FileNotFoundError(f"Local attachment not found: {local_path}")
        if local_path.stat().st_size > ZENDESK_ATTACHMENT_MAX_BYTES:
            raise RuntimeError(
                f"Zendesk attachment exceeds 50 MB limit: {attachment_filename} ({local_path.stat().st_size} bytes)"
            )

        url = f"{self.base_url}/api/v2/uploads.json"
        params = {"filename": attachment_filename}
        mime = content_type or mimetypes.guess_type(attachment_filename)[0] or "application/octet-stream"

        for attempt in range(1, 7):
            try:
                with local_path.open("rb") as handle:
                    response = self.session.post(
                        url,
                        params=params,
                        data=handle,
                        headers={
                            "Authorization": f"Bearer {self.access_token}",
                            "Content-Type": mime,
                            "Accept": "application/json",
                        },
                        timeout=180,
                    )
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 6:
                    raise
                time.sleep(min(60, 2 ** (attempt - 1)))
                continue

            if response.status_code == 401 and attempt == 1:
                self.authenticate()
                continue

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < 6:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else min(60, 2 ** (attempt - 1))
                except ValueError:
                    delay = min(60, 2 ** (attempt - 1))
                time.sleep(delay)
                continue

            if response.status_code != 201:
                raise RuntimeError(
                    f"Zendesk attachment upload failed for {attachment_filename}: "
                    f"HTTP {response.status_code}\n{response.text[:5000]}"
                )

            upload = response.json().get("upload", {})
            token = s(upload.get("token"))
            attachment = upload.get("attachment") or {}
            if not token:
                raise RuntimeError(f"Zendesk attachment upload returned no token for {attachment_filename}.")
            return {
                "token": token,
                "attachment_id": attachment.get("id", ""),
                "content_url": attachment.get("content_url", ""),
                "file_name": attachment.get("file_name", attachment_filename),
                "content_type": attachment.get("content_type", mime),
                "size": attachment.get("size", local_path.stat().st_size),
            }

        raise RuntimeError(f"Zendesk attachment upload failed after retries: {attachment_filename}")

    def add_internal_comment(
        self,
        ticket_id: int,
        body: str,
    ) -> Dict[str, Any]:
        payload = {
            "ticket": {
                "comment": {
                    "body": body or "Salesforce Attachment Migration Notice",
                    "public": False,
                }
            }
        }
        response = self.request(
            "PUT",
            f"/api/v2/tickets/{int(ticket_id)}.json",
            json_body=payload,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Zendesk internal comment failed: HTTP {response.status_code}\n{response.text[:5000]}"
            )
        return response.json().get("ticket", {})

    def add_internal_comment_with_uploads(
        self,
        ticket_id: int,
        body: str,
        upload_tokens: List[str],
    ) -> Dict[str, Any]:
        """Add a private/internal note with one or more uploaded attachment tokens."""
        payload = {
            "ticket": {
                "comment": {
                    "body": body or "Salesforce S3 Attachments",
                    "public": False,
                    "uploads": upload_tokens,
                }
            }
        }
        response = self.request(
            "PUT",
            f"/api/v2/tickets/{int(ticket_id)}.json",
            json_body=payload,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Zendesk ticket attachment comment failed: HTTP {response.status_code}\n{response.text[:5000]}"
            )
        return response.json().get("ticket", {})

    def import_ticket(self, ticket_payload: Dict[str, Any], archive_immediately: bool = False) -> Dict[str, Any]:
        clean_payload = strip_private_metadata(ticket_payload)
        response = self.request(
            "POST",
            "/api/v2/imports/tickets.json",
            params={"archive_immediately": str(archive_immediately).lower()},
            json_body={"ticket": clean_payload},
        )
        if response.status_code != 201:
            raise RuntimeError(
                f"Zendesk ticket import failed: HTTP {response.status_code}\n{response.text[:5000]}"
            )
        return response.json().get("ticket", {})

    def find_ticket_by_external_id(self, external_id: str) -> Optional[Dict[str, Any]]:
        query = f"type:ticket external_id:{external_id}"
        response = self.request("GET", "/api/v2/search.json", params={"query": query, "per_page": 100})
        if response.status_code != 200:
            # Search is a guard only. Do not fail the migration if this optional lookup is unavailable.
            return None
        results = response.json().get("results", [])
        tickets = [r for r in results if r.get("result_type") == "ticket" or "ticket" in s(r.get("url")).lower()]
        return tickets[0] if tickets else None


# -----------------------------------------------------------------------------
# Mapping workbook reader
# -----------------------------------------------------------------------------
class MappingWorkbook:
    def __init__(self, path: Path):
        if not path.exists():
            raise FileNotFoundError(f"Mapping workbook not found: {path}")
        self.path = path
        self.field_rows: List[Dict[str, Any]] = []
        self.value_rows: List[Dict[str, Any]] = []
        self.value_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        wb = load_workbook(self.path, read_only=True, data_only=True)
        try:
            if "SF to ZD Field Map" not in wb.sheetnames:
                raise RuntimeError("Mapping workbook missing sheet: SF to ZD Field Map")
            ws = wb["SF to ZD Field Map"]
            headers = [s(v) for v in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
            for row in ws.iter_rows(min_row=2, values_only=True):
                item = {headers[i]: row[i] if i < len(row) else None for i in range(len(headers))}
                if s(item.get("Salesforce API Name")):
                    self.field_rows.append(item)

            if "Value Mapping" in wb.sheetnames:
                ws = wb["Value Mapping"]
                headers = [s(v) for v in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
                for row in ws.iter_rows(min_row=2, values_only=True):
                    item = {headers[i]: row[i] if i < len(row) else None for i in range(len(headers))}
                    if s(item.get("Salesforce API Name")):
                        self.value_rows.append(item)
                        key = (s(item.get("Salesforce API Name")), normalize_label(item.get("Salesforce Value")))
                        if key[1] and key not in self.value_map:
                            self.value_map[key] = item
        finally:
            wb.close()

    @staticmethod
    def field_id(row: Dict[str, Any]) -> Optional[int]:
        raw = s(row.get("Zendesk Target Field ID"))
        if not raw:
            return None
        try:
            return int(float(raw))
        except (ValueError, TypeError):
            return None

    def grouped_target_rows(self) -> Dict[int, List[Dict[str, Any]]]:
        groups: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for row in self.field_rows:
            fid = self.field_id(row)
            if fid:
                groups[fid].append(row)
        return groups

    def lookup_value(self, sf_api: str, sf_value: str) -> Optional[Dict[str, Any]]:
        return self.value_map.get((sf_api, normalize_label(sf_value)))

    def summary(self) -> Dict[str, Any]:
        mapped = [r for r in self.field_rows if s(r.get("Mapping Status")) != "Ignore"]
        return {
            "field_rows": len(self.field_rows),
            "value_rows": len(self.value_rows),
            "non_ignored_field_rows": len(mapped),
        }


# -----------------------------------------------------------------------------
# Privacy / customer aliasing
# -----------------------------------------------------------------------------
class CustomerAliasManager:
    """Backward-compatible identity helper. No anonymization is performed.

    The existing migration code calls alias_for(); this method now returns the actual
    customer email unchanged so all requester/customer comments use the original email.
    """

    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.aliases: Dict[str, str] = {}

    def _key(self, source_email: str) -> str:
        return sha12(norm(source_email))

    def alias_for(self, source_email: str) -> str:
        email = valid_email(source_email) if source_email else ""
        return email or norm(source_email)

    def fingerprint(self, source_email: str) -> str:
        return self._key(source_email) if source_email else ""


# -----------------------------------------------------------------------------
# Email handling
# -----------------------------------------------------------------------------
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


def scrub_customer_emails(
    text: str,
    aliases: CustomerAliasManager,
    protected_agent_emails: set[str],
) -> str:
    """Compatibility wrapper: customer emails are no longer anonymized."""
    return text or ""


# -----------------------------------------------------------------------------
# Salesforce data export
# -----------------------------------------------------------------------------
def build_case_select_fields(sf: SalesforceClient, mapping_path: Optional[Path]) -> List[str]:
    """Build a bounded SELECT list instead of FIELDS(ALL).

    REST supports FIELDS(ALL) only with a row LIMIT <= 200. For large date ranges
    we therefore select only the mapped/core Case fields and let REST pagination
    return subsequent pages.
    """
    core_fields = {
        "Id", "AccountId", "ContactId", "CaseNumber", "CreatedById", "CreatedDate",
        "Description", "OwnerId", "Priority", "Status", "Subject", "SuppliedCompany",
        "SuppliedEmail", "SuppliedName", "SuppliedPhone", "ContactEmail", "Origin", "ParentId",
        "Type", "RecordTypeId",
        "To_Address__c", "BP_Group__c", "BP_Product__c", "Status_B2B__c", "BP_Source__c",
    }
    core_fields.update(INTERNAL_COMMENT_FIELDS)

    if mapping_path and mapping_path.exists():
        try:
            wb = MappingWorkbook(mapping_path)
            for row in wb.field_rows:
                api_name = s(row.get("Salesforce API Name"))
                if api_name and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_name):
                    core_fields.add(api_name)
        except Exception as exc:
            print(f"WARNING: Could not read mapping workbook for Case field discovery: {exc}")

    available = sf.describe_fields("Case")
    fields = sorted(f for f in core_fields if f in available)
    missing = sorted(f for f in core_fields if f not in available)
    if missing:
        print(f"WARNING: {len(missing)} mapped Case fields were not found in Salesforce and will be skipped.")
    if "Id" not in fields or "CreatedDate" not in fields or "Subject" not in fields:
        raise RuntimeError("Required Case fields were not found in Salesforce Case describe metadata.")
    return fields



PHONE_SOURCE_VALUES = {"phone", "telephone", "tel", "call"}
URL_RE = re.compile(r"https?://[^\s<>\"']+", flags=re.I)


def is_phone_case(case: Dict[str, Any]) -> bool:
    """Identify phone-origin Cases using BP_Source__c first, then standard Origin."""
    values = [case.get("BP_Source__c"), case.get("Origin")]
    for value in values:
        if normalize_label(value) in PHONE_SOURCE_VALUES:
            return True
    return False


def extract_urls_from_value(value: Any) -> List[str]:
    raw = s(value)
    if not raw:
        return []
    # Remove common trailing punctuation from URLs copied from Task text.
    urls: List[str] = []
    for match in re.findall(r"https?://[^\s<>\"']+", raw, flags=re.I):
        url = match.rstrip(",.;:)]}")
        if url and url not in urls:
            urls.append(url)
    return urls


def build_task_select_fields(sf: SalesforceClient) -> List[str]:
    """Build a conservative Task SELECT list, including likely recording URL custom fields."""
    available = sf.describe_fields("Task")
    preferred = {
        "Id", "WhatId", "WhoId", "Subject", "Description", "CreatedDate", "CreatedById",
        "Status", "Priority", "Type", "TaskSubtype", "CallType", "CallDisposition",
        "CallDurationInSeconds", "ActivityDate", "IsDeleted",
    }
    for field_name in available:
        lowered = field_name.lower()
        if any(token in lowered for token in ("record", "recording", "call", "voicemail")) and any(token in lowered for token in ("url", "link", "record", "recording", "call", "voicemail")):
            preferred.add(field_name)
    fields = sorted(f for f in preferred if f in available)
    if "Id" not in fields or "WhatId" not in fields:
        raise RuntimeError("Required Task fields Id/WhatId were not found in Salesforce Task describe metadata.")
    return fields


def extract_task_recording_urls(task: Dict[str, Any]) -> List[str]:
    """Extract recording URLs from Task.Description and fields whose names suggest recording/call URLs."""
    preferred_fields = []
    for key in task.keys():
        lowered = key.lower()
        if key == "Description" or any(token in lowered for token in ("recording", "record", "call", "voicemail")) and any(token in lowered for token in ("url", "link", "record", "recording", "call", "voicemail")):
            preferred_fields.append(key)
    # Keep Description first; then candidate custom fields.
    preferred_fields = sorted(set(preferred_fields), key=lambda x: (0 if x == "Description" else 1, x))
    urls: List[str] = []
    for key in preferred_fields:
        for url in extract_urls_from_value(task.get(key)):
            if url not in urls:
                urls.append(url)
    return urls


def export_phone_tasks(
    sf: SalesforceClient,
    phone_cases: List[Dict[str, Any]],
    output_dir: Path,
) -> List[Dict[str, Any]]:
    """Export Tasks only for phone-origin Cases and extract call recording URLs."""
    tasks: List[Dict[str, Any]] = []
    phone_case_ids = [s(c.get("Id")) for c in phone_cases if s(c.get("Id"))]
    task_fields = build_task_select_fields(sf)
    for ids in progress_chunks(phone_case_ids, EXPORT_ID_BATCH_SIZE, "Phone Tasks"):
        tasks.extend(sf.query(
            f"SELECT {', '.join(task_fields)} FROM Task "
            f"WHERE WhatId IN ({soql_ids(ids)}) AND IsDeleted = false "
            "ORDER BY WhatId, CreatedDate ASC"
        ))

    tasks_by_case: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    recording_rows: List[Dict[str, Any]] = []
    for task in tasks:
        case_id = s(task.get("WhatId"))
        if not case_id:
            continue
        urls = extract_task_recording_urls(task)
        task_copy = dict(task)
        task_copy["Recording_URLs"] = "; ".join(urls)
        task_copy["Is_Recording_Task"] = "yes" if urls else "no"
        tasks_by_case[case_id].append(task_copy)
        if urls:
            for url in urls:
                recording_rows.append({
                    "Salesforce_Case_ID": case_id,
                    "CaseNumber": "",
                    "Task_ID": s(task.get("Id")),
                    "Task_Subject": s(task.get("Subject")),
                    "Task_CreatedDate": s(task.get("CreatedDate")),
                    "Recording_URL": url,
                    "Source_Field": "Task.Description/custom recording field",
                })

    case_number_by_id = {s(c.get("Id")): s(c.get("CaseNumber")) for c in phone_cases if s(c.get("Id"))}
    for row in recording_rows:
        row["CaseNumber"] = case_number_by_id.get(row["Salesforce_Case_ID"], "")

    write_csv(output_dir / "Phone_Cases.csv", phone_cases)
    write_csv(output_dir / "Phone_Tasks.csv", tasks)
    modular_excel_write(
        output_dir / "Phone_Call_Recordings.xlsx",
        recording_rows,
        headers=["Salesforce_Case_ID", "CaseNumber", "Task_ID", "Task_Subject", "Task_CreatedDate", "Recording_URL", "Source_Field"],
        sheet_name="Phone Recordings",
    )
    write_csv(output_dir / "Phone_Call_Recordings.csv", recording_rows)
    return tasks


def export_salesforce_data(
    sf: SalesforceClient,
    output_dir: Path,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    mapping_path: Optional[Path] = None,
) -> Dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    print("Authenticating to Salesforce...")
    sf.authenticate()
    print("Salesforce authentication successful.")

    # Build the CreatedDate filter before applying the 10-ticket limit.
    # The end date is inclusive, so 2026-09-29 includes the entire day.
    filters: List[str] = []

    parsed_start = None
    parsed_end = None

    if start_date:
        try:
            parsed_start = datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            raise RuntimeError(
                f"Invalid --start-date '{start_date}'. Use YYYY-MM-DD."
            )
        filters.append(f"CreatedDate >= {start_date}T00:00:00Z")

    if end_date:
        try:
            parsed_end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            raise RuntimeError(
                f"Invalid --end-date '{end_date}'. Use YYYY-MM-DD."
            )

        if parsed_start and parsed_start > parsed_end:
            raise RuntimeError(
                f"Start date {start_date} cannot be after end date {end_date}."
            )

        # Inclusive end date: use the next day as the exclusive upper bound.
        next_day = parsed_end + timedelta(days=1)
        filters.append(f"CreatedDate < {next_day.isoformat()}T00:00:00Z")

    active_source_filter = os.getenv("SF_SOURCE_FILTER", "").lower().strip()
    if active_source_filter in {"portal_api", "portal", "ticket_api", "portal_ticket_api"}:
        print("Applying Source Filter: ONLY 'Portal (Ticket API)' cases [BP_Source__c IN ('Ticket-API', 'Portal (Ticket API)', 'Ticket API')]")
        filters.append("BP_Source__c IN ('Ticket-API', 'Portal (Ticket API)', 'Ticket API')")
    elif active_source_filter in {"non_portal_api", "non_portal", "exclude_portal_api"}:
        print("Applying Source Filter: ONLY NON-'Portal (Ticket API)' cases [(BP_Source__c NOT IN ('Ticket-API', 'Portal (Ticket API)', 'Ticket API') OR BP_Source__c = null)]")
        filters.append("(BP_Source__c NOT IN ('Ticket-API', 'Portal (Ticket API)', 'Ticket API') OR BP_Source__c = null)")

    where_clause = f" WHERE {' AND '.join(filters)}" if filters else ""

    fields = build_case_select_fields(sf, mapping_path)
    if filters:
        limit_label = str(MAX_TICKETS) if MAX_TICKETS > 0 else "all"
        print(
            f"Fetching {limit_label} Cases where CreatedDate is between "
            f"{start_date or 'beginning'} and {end_date or 'today/infinity'}..."
        )
    else:
        limit_label = str(MAX_TICKETS) if MAX_TICKETS > 0 else "all"
        print(f"Fetching {limit_label} Cases without a date filter...")

    case_soql = (
        f"SELECT {', '.join(fields)} FROM Case"
        f"{where_clause} ORDER BY CreatedDate ASC"
    )
    if MAX_TICKETS > 0:
        case_soql += f" LIMIT {MAX_TICKETS}"

    cases = sf.query(case_soql, progress_label="Cases")
    cases = limit_rows(cases)
    if not cases:
        raise RuntimeError("No Salesforce Cases returned.")

    case_ids = [s(c.get("Id")) for c in cases if s(c.get("Id"))]
    account_ids = sorted({s(c.get("AccountId")) for c in cases if s(c.get("AccountId"))})
    contact_ids = sorted({s(c.get("ContactId")) for c in cases if s(c.get("ContactId"))})

    print("Fetching related EmailMessages...")
    email_messages: List[Dict[str, Any]] = []
    for ids in progress_chunks(case_ids, EXPORT_ID_BATCH_SIZE, "EmailMessages"):
        email_messages.extend(
            sf.query(
                "SELECT Id, ParentId, CreatedById, CreatedDate, LastModifiedDate, "
                "MessageDate, Incoming, FromAddress, ToAddress, CcAddress, BccAddress, "
                "FromName, Subject, TextBody, HtmlBody, Status, IsPrivateDraft, HasAttachment, Headers "
                f"FROM EmailMessage WHERE ParentId IN ({soql_ids(ids)}) "
                "ORDER BY ParentId, MessageDate ASC, CreatedDate ASC"
            )
        )

    print(f"[Export] EmailMessages complete: {len(email_messages):,}", flush=True)
    print("Fetching related Salesforce S3 mappings...")
    s3_records: List[Dict[str, Any]] = []
    for ids in progress_chunks(case_ids, EXPORT_ID_BATCH_SIZE, "S3 mappings"):
        s3_records.extend(
            sf.query(
                "SELECT Id, Name, CreatedDate, BP_Case__c, BP_RecordId__c, "
                "BP_S3_URL__c, BP_Fallback_URL__c, BP_Upload_Complete__c "
                f"FROM BP_S3__c WHERE BP_Case__c IN ({soql_ids(ids)}) "
                "AND IsDeleted = false ORDER BY BP_Case__c, CreatedDate ASC"
            )
        )

    print(f"[Export] S3 mapping rows complete: {len(s3_records):,}", flush=True)
    print("Fetching related Contacts...")
    contacts: List[Dict[str, Any]] = []
    for ids in progress_chunks(contact_ids, EXPORT_ID_BATCH_SIZE, "Contacts"):
        contacts.extend(
            sf.query(
                "SELECT Id, AccountId, Name, FirstName, LastName, Email, Phone, MobilePhone, "
                "MailingStreet, MailingCity, MailingState, MailingPostalCode, MailingCountry, "
                "OwnerId, CreatedDate, LastModifiedDate "
                f"FROM Contact WHERE Id IN ({soql_ids(ids)})"
            )
        )

    print(f"[Export] Contacts complete: {len(contacts):,}", flush=True)
    print("Fetching related Accounts...")
    accounts: List[Dict[str, Any]] = []
    for ids in progress_chunks(account_ids, EXPORT_ID_BATCH_SIZE, "Accounts"):
        accounts.extend(
            sf.query(
                "SELECT Id, Name, Type, ParentId, BillingStreet, BillingCity, BillingState, "
                "BillingPostalCode, BillingCountry, ShippingStreet, ShippingCity, ShippingState, "
                "ShippingPostalCode, ShippingCountry, Phone, Website, Industry, NumberOfEmployees, "
                "Description, OwnerId, CreatedDate, LastModifiedDate "
                f"FROM Account WHERE Id IN ({soql_ids(ids)})"
            )
        )

    print(f"[Export] Accounts complete: {len(accounts):,}", flush=True)
    print("Fetching Salesforce users relevant to the sample...")
    user_ids = sorted(
        {s(c.get("OwnerId")) for c in cases if s(c.get("OwnerId"))}
        | {s(m.get("CreatedById")) for m in email_messages if s(m.get("CreatedById"))}
    )
    users: List[Dict[str, Any]] = []
    for ids in progress_chunks(user_ids, EXPORT_ID_BATCH_SIZE, "Salesforce Users"):
        users.extend(
            sf.query(
                "SELECT Id, Name, Email, Username, IsActive, UserType, Profile.Name, Department, Title "
                f"FROM User WHERE Id IN ({soql_ids(ids)})"
            )
        )

    print(f"[Export] Salesforce Users complete: {len(users):,}", flush=True)
    phone_cases = [c for c in cases if is_phone_case(c)]
    phone_tasks: List[Dict[str, Any]] = []
    if phone_cases:
        print(f"Fetching Tasks for {len(phone_cases)} phone-origin Cases...")
        phone_tasks = export_phone_tasks(sf, phone_cases, output_dir)
    else:
        write_csv(output_dir / "Phone_Cases.csv", [])
        write_csv(output_dir / "Phone_Tasks.csv", [])
        modular_excel_write(
            output_dir / "Phone_Call_Recordings.xlsx",
            [],
            headers=["Salesforce_Case_ID", "CaseNumber", "Task_ID", "Task_Subject", "Task_CreatedDate", "Recording_URL", "Source_Field"],
            sheet_name="Phone Recordings",
        )
        write_csv(output_dir / "Phone_Call_Recordings.csv", [])

    print(f"[Export] Phone Tasks complete: {len(phone_tasks):,}", flush=True)
    print("Fetching related Salesforce Record Types...")
    record_type_ids = sorted({s(c.get("RecordTypeId")) for c in cases if s(c.get("RecordTypeId"))})
    record_types: List[Dict[str, Any]] = []
    for ids in progress_chunks(record_type_ids, EXPORT_ID_BATCH_SIZE, "Record Types"):
        record_types.extend(
            sf.query(
                "SELECT Id, Name, DeveloperName, SobjectType, IsActive "
                f"FROM RecordType WHERE Id IN ({soql_ids(ids)})"
            )
        )

    print(f"[Export] Record Types complete: {len(record_types):,}", flush=True)
    write_csv(output_dir / "Cases.csv", cases)
    write_csv(output_dir / "EmailMessages.csv", email_messages)
    write_csv(output_dir / "S3Records.csv", s3_records)
    write_csv(output_dir / "Contacts.csv", contacts)
    write_csv(output_dir / "Accounts.csv", accounts)
    write_csv(output_dir / "Salesforce_Users.csv", users)
    write_csv(output_dir / "Salesforce_RecordTypes.csv", record_types)

    summary = {
        "cases": len(cases),
        "phone_cases": len(phone_cases),
        "phone_tasks": len(phone_tasks),
        "phone_recording_urls": sum(1 for t in phone_tasks if s(t.get("Recording_URLs"))),
        "email_messages": len(email_messages),
        "s3_records": len(s3_records),
        "contacts": len(contacts),
        "accounts": len(accounts),
        "salesforce_users": len(users),
        "salesforce_record_types": len(record_types),
    }
    write_csv(
        output_dir / "Export_Summary.csv",
        [{"Item": k, "Count": v} for k, v in summary.items()],
    )
    return summary


# -----------------------------------------------------------------------------
# Migration payload preparation
# -----------------------------------------------------------------------------
def build_lookup_maps(cases: List[Dict[str, Any]], contacts: List[Dict[str, Any]], accounts: List[Dict[str, Any]], users: List[Dict[str, Any]]):
    contacts_by_id = {s(x.get("Id")): x for x in contacts if s(x.get("Id"))}
    accounts_by_id = {s(x.get("Id")): x for x in accounts if s(x.get("Id"))}
    users_by_id = {s(x.get("Id")): x for x in users if s(x.get("Id"))}
    emails_by_case: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    return contacts_by_id, accounts_by_id, users_by_id, emails_by_case


def customer_source_email(
    case: Dict[str, Any],
    contact: Optional[Dict[str, Any]],
    case_messages: List[Dict[str, Any]],
    agent_emails: set[str],
) -> str:
    candidates = []
    if contact:
        candidates.extend([contact.get("Email")])
    candidates.extend([case.get("ContactEmail"), case.get("SuppliedEmail")])
    for msg in case_messages:
        if bool_value(msg.get("Incoming")):
            candidates.extend([msg.get("FromAddress")])

    for candidate in candidates:
        email = first_email(candidate)
        if email and email not in agent_emails:
            return email
    return ""


def user_name(case: Dict[str, Any], contact: Optional[Dict[str, Any]]) -> str:
    if contact and s(contact.get("Name")):
        return s(contact.get("Name"))
    if s(case.get("SuppliedName")):
        return s(case.get("SuppliedName"))
    email = first_email(case.get("SuppliedEmail"))
    return email or f"Salesforce Customer {s(case.get('CaseNumber'))}"


def sf_system_status(value: str) -> Optional[str]:
    v = normalize_label(value)
    mapping = {
        "new": "new",
        "open": "open",
        "assigned": "open",
        "working": "open",
        "reassigned": "open",
        "escalated": "open",
        "jira escalated": "open",
        "waiting on customer": "pending",
        "waiting on department": "hold",
        "pending": "pending",
        "hold": "hold",
        "resolved": "solved",
        "solved": "solved",
        "closed": "closed",
        "discarded": "closed",
    }
    return mapping.get(v)


def map_custom_value(
    mapping: MappingWorkbook,
    sf_api: str,
    value: Any,
) -> Tuple[Optional[str], Optional[str]]:
    raw = s(value)
    if not raw:
        return None, "blank"
    row = mapping.lookup_value(sf_api, raw)
    if not row:
        return None, "no_value_mapping"
    status = normalize_label(row.get("Match Status"))
    tag = s(row.get("Zendesk Tag"))
    if "exact label match" in status or "exact leaf match" in status or "exact option label" in status:
        if tag:
            return tag, "mapped_tag"
        zvalue = s(row.get("Zendesk Value"))
        if zvalue:
            return zvalue, "mapped_value"
        return None, "mapped_without_target_value"
    return None, "non_exact_or_missing_target"


def parse_checkbox(value: Any) -> Optional[bool]:
    v = s(value)
    if not v:
        return None
    if v.lower() in {"true", "1", "yes", "y"}:
        return True
    if v.lower() in {"false", "0", "no", "n"}:
        return False
    return None


def parse_numeric(value: Any) -> Optional[str]:
    raw = s(value)
    if not raw:
        return None
    try:
        f = float(raw)
        if f.is_integer():
            return str(int(f))
        return str(f)
    except ValueError:
        return raw


def mapped_zendesk_value(mapping: MappingWorkbook, sf_api: str, value: Any) -> str:
    row = mapping.lookup_value(sf_api, s(value))
    if not row:
        return ""
    return s(row.get("Zendesk Value"))


def build_custom_fields(
    case: Dict[str, Any],
    mapping: MappingWorkbook,
    aliases: CustomerAliasManager,
    protected_agent_emails: set[str],
    report: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    grouped = mapping.grouped_target_rows()
    used_ids = set()

    for field_id, rows in grouped.items():
        if field_id in used_ids:
            continue

        # Use the first usable source row for a given Zendesk target field.
        chosen_row: Optional[Dict[str, Any]] = None
        chosen_value: Any = None
        mapped_value: Optional[str] = None
        mapped_reason = ""

        for row in rows:
            sf_api = s(row.get("Salesforce API Name"))
            status = normalize_label(row.get("Mapping Status"))
            target_type = normalize_label(row.get("Zendesk Target Type"))
            target_name = s(row.get("Zendesk Target Field"))

            if status == "ignore" or not sf_api or sf_api not in case:
                continue
            if target_name in SYSTEM_ONLY_TARGETS:
                continue
            if target_name in {"Owner (custom) + Assignee/Group (system)", "Assignee/Group (system)"}:
                continue
            if "lookup" in target_type:
                continue
            if sf_api in {"BP_Tags__c", "Tags_B2B__c"}:
                # Store Salesforce multi-select tags as Zendesk system ticket tags.
                continue
            if sf_api == "BP_Product__c":
                # Salesforce Product maps to Zendesk Brand, not the Zendesk Product custom field.
                continue
            if sf_api in {"BP_Enquiry__c", "BP_Query__c", "BP_Query_B2B__c", "BP_Query_Related_Issue__c", "Query_Related_Issue_B2B__c"}:
                # These three values are stored as ticket tags per the migration requirement.
                continue

            value = case.get(sf_api)
            if s(value) == "":
                continue

            chosen_row = row
            chosen_value = value
            break

        if not chosen_row:
            continue

        sf_api = s(chosen_row.get("Salesforce API Name"))
        target_name = s(chosen_row.get("Zendesk Target Field"))
        target_type = normalize_label(chosen_row.get("Zendesk Target Type"))

        # Email custom fields use the original Salesforce email address; no anonymization is performed.
        if "email" in normalize_label(chosen_row.get("Salesforce Field Label")) or "email" in normalize_label(sf_api) or target_name == "Email Address":
            source_email = first_email(chosen_value)
            if not source_email:
                report.append({
                    "case_id": s(case.get("Id")),
                    "field": sf_api,
                    "target_field_id": field_id,
                    "status": "skipped",
                    "reason": "invalid_email_value",
                    "source_value": s(chosen_value),
                })
                continue
            value = aliases.alias_for(source_email)
        elif "drop-down" in target_type or "drop down" in target_type or "multiselect" in target_type:
            mapped_value, mapped_reason = map_custom_value(mapping, sf_api, chosen_value)
            if mapped_value is None:
                # For the composite Enquiry Query Issue field, try a direct leaf lookup first.
                if sf_api in {"BP_Query_Related_Issue__c", "Query_Related_Issue_B2B__c"}:
                    v = normalize_label(chosen_value)
                    for vmrow in mapping.value_rows:
                        if s(vmrow.get("Salesforce API Name")) == sf_api and normalize_label(vmrow.get("Salesforce Value")) == v:
                            mapped_value = s(vmrow.get("Zendesk Tag")) or s(vmrow.get("Zendesk Value"))
                            mapped_reason = "leaf_mapping"
                            break
                if mapped_value is None or s(mapped_value) == "":
                    report.append({
                        "case_id": s(case.get("Id")),
                        "field": sf_api,
                        "target_field_id": field_id,
                        "status": "skipped",
                        "reason": mapped_reason or "no_value_mapping",
                        "source_value": s(chosen_value),
                    })
                    continue
            value = mapped_value
            if value is None or s(value) == "":
                report.append({
                    "case_id": s(case.get("Id")),
                    "field": sf_api,
                    "target_field_id": field_id,
                    "status": "skipped",
                    "reason": "empty_mapped_value",
                    "source_value": s(chosen_value),
                })
                continue
        elif "check-box" in target_type or "checkbox" in target_type:
            value = parse_checkbox(chosen_value)
            if value is None:
                continue
        elif "integer" in target_type:
            raw_numeric = s(chosen_value)
            if not re.fullmatch(r"[-+]?\d+", raw_numeric):
                report.append({
                    "case_id": s(case.get("Id")),
                    "field": sf_api,
                    "target_field_id": field_id,
                    "status": "skipped",
                    "reason": "invalid_integer_value",
                    "source_value": raw_numeric,
                })
                continue
            value = int(raw_numeric)
        elif "numeric" in target_type:
            value = parse_numeric(chosen_value)
            if value is None:
                continue
        else:
            value = scrub_customer_emails(s(chosen_value), aliases, protected_agent_emails)

        result.append({"id": int(field_id), "value": value})
        used_ids.add(field_id)

    # Do not import the same custom field twice.
    dedup: Dict[int, Dict[str, Any]] = {}
    for item in result:
        dedup[int(item["id"])] = item
    return list(dedup.values())


def build_system_tags(case: Dict[str, Any]) -> List[str]:
    tags = ["salesforce_server_migrated", "sf_case"]

    origin = safe_tag(case.get("Origin"))
    source = safe_tag(case.get("BP_Source__c"))
    tag_value = origin or source
    if tag_value:
        tags.append(f"sf_origin_{tag_value}")
    if source and source != origin:
        tags.append(f"sf_source_{source}")

    # Salesforce multi-select/system tags.
    for field_name in ("BP_Tags__c", "Tags_B2B__c"):
        raw_tags = s(case.get(field_name))
        if raw_tags:
            for part in re.split(r"[;,]", raw_tags):
                t = safe_tag(part)
                if t:
                    tags.append(t)

    # Enquiry / Query / Query Related Issue are required as Zendesk ticket tags.
    for field_name in (
        "BP_Enquiry__c",
        "BP_Query__c",
        "BP_Query_B2B__c",
        "BP_Query_Related_Issue__c",
        "Query_Related_Issue_B2B__c",
    ):
        value = safe_tag(case.get(field_name))
        if value:
            tags.append(value)

    return list(dict.fromkeys(tags))


def normalize_comment_body(value: Any) -> str:
    return re.sub(r"\s+", " ", s(value)).strip().lower()


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


def _has_real_html(value: str) -> bool:
    return bool(HTML_TAG_RE.search(value or ""))


def _convert_signature_markup_in_html(value: str) -> str:
    """Convert Salesforce/email plain-text image-link notation into real HTML."""
    raw = s(value)
    if not raw:
        return ""

    def image_link_repl(match: re.Match[str]) -> str:
        image_url = html.escape(match.group(1), quote=True)
        link_url = html.escape(match.group(2), quote=True)
        return f'<a href="{link_url}" target="_blank" rel="noopener noreferrer"><img src="{image_url}" alt="" /></a>'

    raw = SIGNATURE_IMAGE_LINK_RE.sub(image_link_repl, raw)

    def image_repl(match: re.Match[str]) -> str:
        image_url = html.escape(match.group(1), quote=True)
        return f'<img src="{image_url}" alt="" />'

    raw = SIGNATURE_IMAGE_RE.sub(image_repl, raw)

    def angle_url_repl(match: re.Match[str]) -> str:
        url = html.escape(match.group(1), quote=True)
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{html.escape(match.group(1))}</a>'

    raw = ANGLE_URL_RE.sub(angle_url_repl, raw)
    return raw


def plain_email_text_to_html(value: str) -> str:
    """Convert plain email text to HTML while reconstructing common signature image/link markup."""
    raw = s(value)
    if not raw:
        return ""

    placeholders: List[Tuple[str, str]] = []
    counter = 0

    def image_link_placeholder(match: re.Match[str]) -> str:
        nonlocal counter
        token = f"___ZD_SIG_{counter}___"
        counter += 1
        image_url = html.escape(match.group(1), quote=True)
        link_url = html.escape(match.group(2), quote=True)
        placeholders.append((token, f'<a href="{link_url}" target="_blank" rel="noopener noreferrer"><img src="{image_url}" alt="" /></a>'))
        return token

    raw = SIGNATURE_IMAGE_LINK_RE.sub(image_link_placeholder, raw)

    def image_placeholder(match: re.Match[str]) -> str:
        nonlocal counter
        token = f"___ZD_SIG_{counter}___"
        counter += 1
        image_url = html.escape(match.group(1), quote=True)
        placeholders.append((token, f'<img src="{image_url}" alt="" />'))
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
    """Return (html_body, source_format) for Zendesk Ticket Import."""
    if raw_html and _has_real_html(raw_html):
        prepared = _convert_signature_markup_in_html(raw_html)
        return prepared, "HtmlBody"

    source = raw_text or raw_html
    if source:
        return plain_email_text_to_html(source), "TextBody/reconstructed"

    return "", ""


def build_comments(
    case: Dict[str, Any],
    case_messages: List[Dict[str, Any]],
    s3_records: List[Dict[str, Any]],
    customer_user_by_source_email: Dict[str, int],
    primary_customer_user_id: Optional[int],
    akarsh_user_id: int,
    aliases: CustomerAliasManager,
    protected_agent_emails: set[str],
    report: List[Dict[str, Any]],
    initial_description_key: str = "",
    agent_user_by_sf_user_id: Optional[Dict[str, int]] = None,
    agent_user_by_email: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    comments: List[Tuple[datetime, int, Dict[str, Any]]] = []
    sequence = 0
    seen_public_bodies: set[str] = set()

    for msg in case_messages:
        if bool_value(msg.get("IsPrivateDraft")):
            report.append({
                "case_id": s(case.get("Id")),
                "email_message_id": s(msg.get("Id")),
                "status": "skipped",
                "reason": "private_draft",
            })
            continue

        raw_html = s(msg.get("HtmlBody"))
        raw_text = s(msg.get("TextBody"))
        html_body_prepared, body_format = prepare_zendesk_html_body(raw_html, raw_text)

        display_body = raw_text or html_to_text(raw_html)
        display_body = scrub_customer_emails(display_body, aliases, protected_agent_emails)
        if not display_body:
            continue

        body_key = normalize_comment_body(display_body)
        if initial_description_key and body_key == initial_description_key:
            report.append({
                "case_id": s(case.get("Id")),
                "email_message_id": s(msg.get("Id")),
                "status": "skipped",
                "reason": "used_as_ticket_description",
            })
            continue

        if body_key in seen_public_bodies:
            report.append({
                "case_id": s(case.get("Id")),
                "email_message_id": s(msg.get("Id")),
                "status": "skipped",
                "reason": "duplicate_comment_body",
            })
            continue

        incoming = bool_value(msg.get("Incoming"))
        if incoming:
            source_email = first_email(msg.get("FromAddress"))
            author_id = customer_user_by_source_email.get(norm(source_email), primary_customer_user_id)
            if not author_id:
                author_id = akarsh_user_id
        else:
            sf_user_id = s(msg.get("CreatedById"))
            author_id = (agent_user_by_sf_user_id or {}).get(sf_user_id)
            if not author_id:
                source_email = ""
                # Resolve the Salesforce EmailMessage creator email when possible.
                if agent_user_by_email and sf_user_id:
                    author_id = (agent_user_by_email or {}).get(norm(sf_user_id))
            if not author_id:
                creator_email = first_email(msg.get("FromAddress"))
                author_id = (agent_user_by_email or {}).get(norm(creator_email)) if creator_email else None
            if not author_id:
                author_id = akarsh_user_id

        created = parse_dt(msg.get("MessageDate")) or parse_dt(msg.get("CreatedDate"))
        if not created:
            created = datetime.now(timezone.utc)

        if html_body_prepared:
            html_body = scrub_customer_emails(html_body_prepared, aliases, protected_agent_emails)
            comment_body = {"html_body": html_body}
        else:
            comment_body = {
                "value": scrub_customer_emails(raw_text, aliases, protected_agent_emails),
            }

        comment = {
            "author_id": int(author_id),
            "public": True,
            **comment_body,
            # Internal migration-only metadata. Removed before API submission.
            "_salesforce_email_message_id": s(msg.get("Id")),
        }
        if created <= datetime.now(timezone.utc):
            comment["created_at"] = created.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            report.append({
                "case_id": s(case.get("Id")),
                "email_message_id": s(msg.get("Id")),
                "status": "timestamp_omitted",
                "reason": "future_message_date",
                "body_format": body_format if html_body_prepared else "value",
            })

        comments.append((created, sequence, comment))
        seen_public_bodies.add(body_key)
        sequence += 1

    # S3 attachments are no longer represented by placeholder comments here.
    # They are attached from S3_Attachment_Mapping.csv during migration.

    for field_name in INTERNAL_COMMENT_FIELDS:
        value = s(case.get(field_name))
        if not value:
            continue
        body = scrub_customer_emails(value, aliases, protected_agent_emails)
        created = parse_dt(case.get("CreatedDate")) or datetime.now(timezone.utc)
        comments.append((created, sequence, {
            "author_id": int(akarsh_user_id),
            "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "public": False,
            "value": body,
        }))
        sequence += 1

    comments.sort(key=lambda x: (x[0], x[1]))
    return [c for _, _, c in comments]


def build_ticket_payload(
    case: Dict[str, Any],
    case_messages: List[Dict[str, Any]],
    s3_records: List[Dict[str, Any]],
    mapping: MappingWorkbook,
    contacts_by_id: Dict[str, Dict[str, Any]],
    accounts_by_id: Dict[str, Dict[str, Any]],
    customer_users: Dict[str, int],
    customer_user_by_source_email: Dict[str, int],
    org_users: Dict[str, int],
    owner_assignee_map: Dict[str, int],
    group_map: Dict[str, int],
    brand_map: Dict[str, int],
    record_types_by_id: Dict[str, Dict[str, Any]],
    akarsh_user_id: int,
    agent_user_by_sf_user_id: Optional[Dict[str, int]],
    agent_user_by_email: Optional[Dict[str, int]],
    aliases: CustomerAliasManager,
    protected_agent_emails: set[str],
    prep_report: List[Dict[str, Any]],
    custom_field_report: List[Dict[str, Any]],
    phone_tasks: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    case_id = s(case.get("Id"))
    contact = contacts_by_id.get(s(case.get("ContactId"))) if s(case.get("ContactId")) else None
    account_id = s(case.get("AccountId"))
    owner_id = s(case.get("OwnerId"))

    source_email = customer_source_email(case, contact, case_messages, protected_agent_emails)
    phone = s((contact or {}).get("Phone")) or s((contact or {}).get("MobilePhone")) or s(case.get("SuppliedPhone"))
    name = user_name(case, contact)

    # Standard cases use the original Salesforce customer email. Phone/no-email
    # cases use the migrated case identity created by the entity stage.
    if valid_email(source_email):
        customer_email = aliases.alias_for(source_email)
        fingerprint = aliases.fingerprint(source_email)
        requester_id = customer_user_by_source_email.get(norm(source_email))
    else:
        customer_email = f"sfcase_{sha12(case_id)}@{SYNTHETIC_USER_DOMAIN}"
        fingerprint = sha12(f"case:{case_id}")
        requester_id = customer_users.get(f"case:{case_id}")

    # Backward-compatible fallbacks for the fingerprint-based map.
    if requester_id is None:
        requester_id = customer_users.get(fingerprint)
    if requester_id is None and valid_email(source_email):
        requester_id = customer_users.get(sha12(norm(customer_email)))
    if requester_id is None:
        raise RuntimeError(
            f"Requester user not mapped for Salesforce Case {case_id}. "
            f"Source customer email: {source_email or '<none>'}; phone: {phone or '<none>'}"
        )

    status_value = s(case.get("Status")) or s(case.get("Status_B2B__c"))
    ticket_status = sf_system_status(status_value) or "open"

    # Zendesk Ticket Import requires ticket.description to be non-blank.
    # Prefer Salesforce Case.Description. If it is empty, use the first usable
    # EmailMessage body. Finally, use a deterministic migration fallback so the
    # import never fails validation because Description is blank.
    initial_description = scrub_customer_emails(
        s(case.get("Description")),
        aliases,
        protected_agent_emails,
    )
    description_source = "Case.Description" if initial_description else ""

    if not initial_description:
        for msg in case_messages:
            if bool_value(msg.get("IsPrivateDraft")):
                continue
            msg_body = s(msg.get("TextBody")) or html_to_text(msg.get("HtmlBody"))
            msg_body = scrub_customer_emails(msg_body, aliases, protected_agent_emails)
            if msg_body:
                initial_description = msg_body
                description_source = "EmailMessage"
                break

    if not initial_description:
        initial_description = scrub_customer_emails(
            s(case.get("Subject")) or "Salesforce Case Migration",
            aliases,
            protected_agent_emails,
        ) or "Salesforce Case Migration"
        description_source = "Fallback"

    initial_description_key = normalize_comment_body(initial_description)

    payload: Dict[str, Any] = {
        "external_id": case_id,
        "description": initial_description,
        "subject": scrub_customer_emails(
            s(case.get("Subject")) or f"Salesforce Case {s(case.get('CaseNumber'))}",
            aliases,
            protected_agent_emails,
        ),
        "requester_id": int(requester_id),
        "status": ticket_status,
        "tags": build_system_tags(case),
    }

    created_at = parse_dt(case.get("CreatedDate"))
    if created_at and created_at <= datetime.now(timezone.utc) and created_at.year >= 1970:
        payload["created_at"] = created_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Salesforce Account -> Zendesk Organization. The organization name is preserved
    # on the Zendesk Organization record and the ticket receives its organization_id.
    if account_id and account_id in org_users:
        payload["organization_id"] = int(org_users[account_id])

    # Salesforce Case Owner -> Zendesk Assignee.
        # Salesforce Case Owner -> Zendesk Assignee.
    # If the owner is not mapped to Zendesk, leave the assignee blank.
    if owner_id and owner_id in owner_assignee_map:
        payload["assignee_id"] = int(owner_assignee_map[owner_id])
    else:
        prep_report.append({
            "case_id": case_id,
            "status": "assignee_not_mapped",
            "source_value": owner_id,
            "target_value": "",
            "message": "Salesforce owner not found in Zendesk; assignee left blank.",
        })

    # Salesforce Group -> Zendesk system Group.
    group_value = s(case.get("BP_Group__c"))
    if group_value:
        mapped_group_name = mapped_zendesk_value(mapping, "BP_Group__c", group_value) or group_value
        group_key = normalize_label(mapped_group_name)
        if group_key in group_map:
            payload["group_id"] = int(group_map[group_key])
        else:
            prep_report.append({
                "case_id": case_id,
                "status": "group_not_mapped",
                "source_value": group_value,
            })

    # Salesforce Product -> Zendesk Brand.
    product_value = s(case.get("BP_Product__c"))
    if product_value:
        mapped_brand_name = mapped_zendesk_value(mapping, "BP_Product__c", product_value) or product_value
        brand_key = normalize_label(mapped_brand_name)
        if brand_key in brand_map:
            payload["brand_id"] = int(brand_map[brand_key])
        else:
            prep_report.append({
                "case_id": case_id,
                "status": "brand_not_mapped",
                "source_value": product_value,
            })

    # Salesforce Record Type -> Zendesk Association Type.
    record_type_id = s(case.get("RecordTypeId"))
    record_type = record_types_by_id.get(record_type_id, {})
    record_type_name = s(record_type.get("Name"))
    association_value = ""
    if record_type_name:
        for allowed in ("Parent", "Child", "NA"):
            if normalize_label(record_type_name) == normalize_label(allowed):
                association_value = allowed
                break
    if association_value:
        custom_field_report.append({
            "case_id": case_id,
            "field": "RecordTypeId",
            "target_field_id": ASSOCIATION_TYPE_FIELD_ID,
            "status": "mapped",
            "source_value": record_type_name,
            "target_value": association_value,
        })
    elif record_type_name:
        prep_report.append({
            "case_id": case_id,
            "status": "association_type_not_mapped",
            "source_value": record_type_name,
        })

    priority = normalize_label(case.get("Priority"))
    if priority in {"low", "medium", "high", "critical"}:
        payload["priority"] = priority

    sf_type = normalize_label(case.get("Type"))
    if sf_type in {"problem", "question", "incident", "task"}:
        payload["type"] = sf_type

    comments = build_comments(
        case,
        case_messages,
        s3_records,
        customer_user_by_source_email,
        requester_id,
        akarsh_user_id,
        aliases,
        protected_agent_emails,
        prep_report,
        initial_description_key=initial_description_key,
        agent_user_by_sf_user_id=agent_user_by_sf_user_id,
        agent_user_by_email=agent_user_by_email,
    )
    if comments:
        payload["comments"] = comments

    # Phone-origin Cases: add Salesforce Task call-recording URLs as a private/internal note.
    if is_phone_case(case) and phone_tasks:
        recording_urls: List[str] = []
        recording_tasks: List[Tuple[str, str]] = []
        for task in phone_tasks:
            urls = extract_task_recording_urls(task)
            if not urls:
                continue
            task_subject = s(task.get("Subject")) or "Phone Call"
            task_id = s(task.get("Id"))
            recording_tasks.append((task_subject, task_id))
            for url in urls:
                if url not in recording_urls:
                    recording_urls.append(url)
        if recording_urls:
            note_lines = [
                "Salesforce Phone Call Recording(s)",
                "",
                "The following call recording URL(s) were found on Salesforce Task records for this phone-origin Case:",
            ]
            for url in recording_urls:
                note_lines.append(f"- {url}")
            if recording_tasks:
                note_lines.append("")
                note_lines.append("Task reference:")
                for subject, task_id in recording_tasks:
                    note_lines.append(f"- {subject} (Task {task_id})")
            payload.setdefault("comments", []).append({
                "author_id": int(akarsh_user_id),
                "public": False,
                "value": "\n".join(note_lines),
                "_source": "Salesforce Task call recording",
            })
            prep_report.append({
                "case_id": case_id,
                "status": "phone_recording_urls_added_to_internal_note",
                "recording_url_count": len(recording_urls),
            })

    custom_fields = build_custom_fields(
        case,
        mapping,
        aliases,
        protected_agent_emails,
        custom_field_report,
    )

    # Record Type -> Association Type is an explicit override of the previous workbook mapping.
    if association_value:
        custom_fields = [x for x in custom_fields if int(x["id"]) != ASSOCIATION_TYPE_FIELD_ID]
        custom_fields.append({"id": ASSOCIATION_TYPE_FIELD_ID, "value": association_value})

    if custom_fields:
        payload["custom_fields"] = custom_fields

    audit = {
        "salesforce_case_id": case_id,
        "case_number": s(case.get("CaseNumber")),
        "migration_source": "phone" if is_phone_case(case) else "standard",
        "customer_email": customer_email,
        "customer_fingerprint": fingerprint,
        "requester_id": requester_id,
        "assignee_id": payload.get("assignee_id", ""),
        "organization_id": payload.get("organization_id", ""),
        "group_id": payload.get("group_id", ""),
        "brand_id": payload.get("brand_id", ""),
        "record_type": record_type_name,
        "association_type": association_value,
        "status": ticket_status,
        "description_source": description_source,
        "description_length": len(initial_description),
        "comment_count": len(comments),
        "s3_url_count": len({s(r.get("BP_S3_URL__c")) for r in s3_records if s(r.get("BP_S3_URL__c"))}),
        "phone_recording_url_count": len({url for task in (phone_tasks or []) for url in extract_task_recording_urls(task)}),
        "custom_field_count": len(custom_fields),
    }
    return payload, audit


# -----------------------------------------------------------------------------
# Validation / payload building
# -----------------------------------------------------------------------------
def prepare_local_migration(
    source_dir: Path,
    mapping_path: Path,
    output_dir: Path,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    state_dir = output_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    cases = limit_rows(read_csv(source_dir / "Cases.csv"))
    emails = read_csv(source_dir / "EmailMessages.csv")
    contacts = read_csv(source_dir / "Contacts.csv")
    accounts = read_csv(source_dir / "Accounts.csv")
    users = read_csv(source_dir / "Salesforce_Users.csv")
    record_types = read_csv(source_dir / "Salesforce_RecordTypes.csv") if (source_dir / "Salesforce_RecordTypes.csv").exists() else []
    s3_records = read_csv(source_dir / "S3Records.csv") if (source_dir / "S3Records.csv").exists() else []


    if not cases:
        raise RuntimeError("No Cases found in source export.")

    mapping = MappingWorkbook(mapping_path)
    aliases = CustomerAliasManager(state_dir / "customer_email_aliases.json")

    protected_agent_emails = {
        norm(u.get("Email")) for u in users if valid_email(u.get("Email"))
    }
    protected_agent_emails.add(norm(AGENT_COMMENT_EMAIL))

    contacts_by_id = {s(x.get("Id")): x for x in contacts if s(x.get("Id"))}
    accounts_by_id = {s(x.get("Id")): x for x in accounts if s(x.get("Id"))}

    emails_by_case: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for msg in emails:
        parent = s(msg.get("ParentId"))
        if parent:
            emails_by_case[parent].append(msg)
    for case_id in emails_by_case:
        emails_by_case[case_id].sort(
            key=lambda m: (
                parse_dt(m.get("MessageDate")) or parse_dt(m.get("CreatedDate")) or datetime.min.replace(tzinfo=timezone.utc),
                s(m.get("Id")),
            )
        )

    s3_by_case: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in s3_records:
        case_id = s(record.get("BP_Case__c"))
        if case_id:
            s3_by_case[case_id].append(record)
    for case_id in s3_by_case:
        s3_by_case[case_id].sort(
            key=lambda r: (
                parse_dt(r.get("CreatedDate")) or datetime.min.replace(tzinfo=timezone.utc),
                s(r.get("Id")),
            )
        )

    # Customer aliases and source identities needed to create Zendesk end users.
    customer_identity: Dict[str, Dict[str, Any]] = {}
    for case in cases:
        cid = s(case.get("ContactId"))
        contact = contacts_by_id.get(cid) if cid else None
        case_messages = emails_by_case.get(s(case.get("Id")), [])
        main_source_email = customer_source_email(case, contact, case_messages, protected_agent_emails)
        if not main_source_email:
            main_source_email = f"sfcase_{sha12(s(case.get('Id')))}@{SYNTHETIC_USER_DOMAIN}"
        fingerprint = aliases.fingerprint(main_source_email)
        customer_identity[fingerprint] = {
            "source_email_present": bool(valid_email(main_source_email)),
            "customer_email": aliases.alias_for(main_source_email),
            "name": user_name(case, contact),
            "source_email_for_lookup": main_source_email,
        }

        # Every distinct incoming EmailMessage sender also needs a Zendesk end user if we want
        # that incoming comment to have a real customer author_id.
        for msg in case_messages:
            if not bool_value(msg.get("Incoming")):
                continue
            source_email = first_email(msg.get("FromAddress"))
            if not source_email or source_email in protected_agent_emails:
                continue
            fp = aliases.fingerprint(source_email)
            customer_identity.setdefault(
                fp,
                {
                    "source_email_present": True,
                    "customer_email": aliases.alias_for(source_email),
                    "name": s(msg.get("FromName")) or "Salesforce Customer",
                    "source_email_for_lookup": source_email,
                },
            )

    # Contact/Account relationships are not present in the current 10-case export if there are no IDs.
    return cases, emails, {
        "contacts": contacts,
        "accounts": accounts,
        "users": users,
        "record_types": record_types,
        "contacts_by_id": contacts_by_id,
        "accounts_by_id": accounts_by_id,
        "emails_by_case": emails_by_case,
        "s3_records": s3_records,
        "s3_by_case": s3_by_case,
        "mapping": mapping,
        "aliases": aliases,
        "protected_agent_emails": protected_agent_emails,
        "customer_identity": customer_identity,
        "state_dir": state_dir,
    }


def strip_private_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_private_metadata(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [strip_private_metadata(v) for v in value]
    return value


def write_payload_outputs(output_dir: Path, payloads: List[Dict[str, Any]], audits: List[Dict[str, Any]], prep_report: List[Dict[str, Any]], custom_field_report: List[Dict[str, Any]]) -> None:
    safe_payloads = strip_private_metadata(payloads)
    save_json(output_dir / "tickets_payload.json", {"tickets": safe_payloads})
    write_csv(output_dir / "ticket_payload_summary.csv", audits)
    write_csv(output_dir / "comment_preparation_report.csv", prep_report)
    write_csv(output_dir / "custom_field_report.csv", custom_field_report)


def apply_s3_mapping_to_payload(
    payload: Dict[str, Any],
    case_id: str,
    mapping_rows: List[Dict[str, Any]],
    zd: ZendeskClient,
    migration_report_rows: List[Dict[str, Any]],
    akarsh_user_id: int,
) -> None:
    """Upload locally preflighted files and attach them to matching comments in the payload."""
    if not mapping_rows:
        return

    comments = payload.setdefault("comments", [])
    by_comment_id: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    case_level_rows: List[Dict[str, Any]] = []
    failed_preflight_rows = [r for r in mapping_rows if s(r.get("Preflight_Status")) != "success"]
    failed_upload_rows: List[Dict[str, Any]] = []
    seen_attachment_keys: set[Tuple[str, str, str]] = set()

    # Failed S3 export/downloads do not block ticket creation. Record their URLs
    # and errors in a private/internal note on the ticket.
    if failed_preflight_rows:
        comments.append({
            "author_id": int(akarsh_user_id),
            "public": False,
            "value": s3_failure_note_text(failed_preflight_rows),
        })

    for row in mapping_rows:
        if s(row.get("Preflight_Status")) != "success":
            continue
        local_path = s(row.get("Local_File_Path"))
        if not local_path or not Path(local_path).exists():
            row["Zendesk_Upload_Status"] = "failed"
            row["Zendesk_Attachment_Status"] = "not_attached"
            row["Error"] = f"Local file missing: {local_path}"
            migration_report_rows.append(dict(row))
            continue

        comment_id = s(row.get("Salesforce_Comment_ID"))
        target_key = comment_id or "__CASE_LEVEL__"
        unique_key = (target_key, local_path, s(row.get("S3_URL")))
        if unique_key in seen_attachment_keys:
            continue
        seen_attachment_keys.add(unique_key)
        if comment_id:
            by_comment_id[comment_id].append(row)
        else:
            case_level_rows.append(row)

    comment_objects: Dict[str, Dict[str, Any]] = {
        s(comment.get("_salesforce_email_message_id")): comment
        for comment in comments
        if isinstance(comment, dict) and s(comment.get("_salesforce_email_message_id"))
    }

    def upload_row(row: Dict[str, Any]) -> Optional[str]:
        local_path = Path(s(row.get("Local_File_Path")))
        filename = s(row.get("File_Name")) or local_path.name
        content_type = s(row.get("Content_Type")) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        print(f"      Uploading S3 file to Zendesk: {filename}")
        try:
            token = zd.upload_file(local_path, filename, content_type)
            row["Zendesk_Upload_Status"] = "success"
            row["Zendesk_Attachment_Status"] = "pending_ticket_import"
            row["Error"] = ""
            return token
        except Exception as exc:
            row["Zendesk_Upload_Status"] = "failed"
            row["Zendesk_Attachment_Status"] = "not_attached"
            row["Error"] = str(exc)
            print(f"      Zendesk upload FAILED: {exc}")
            return None

    # First preference: attach to the corresponding historical EmailMessage comment.
    for comment_id, rows in by_comment_id.items():
        comment = comment_objects.get(comment_id)
        if not comment:
            print(f"      Comment {comment_id} is not present in the imported payload; using case-level internal note.")
            case_level_rows.extend(rows)
            continue

        tokens: List[str] = []
        for row in rows:
            token = upload_row(row)
            migration_report_rows.append(dict(row))
            if token:
                tokens.append(token)
            else:
                failed_upload_rows.append(row)
        if tokens:
            comment.setdefault("uploads", []).extend(tokens)

    # Case-level/unmapped attachments go into one private/internal note.
    if case_level_rows:
        tokens: List[str] = []
        attached_names: List[str] = []
        failed_names: List[str] = []
        for row in case_level_rows:
            token = upload_row(row)
            migration_report_rows.append(dict(row))
            name = s(row.get("File_Name")) or Path(s(row.get("Local_File_Path"))).name
            if token:
                tokens.append(token)
                attached_names.append(name)
            else:
                failed_names.append(name)

        note_lines = ["S3 Attachments from Salesforce"]
        if attached_names:
            note_lines += ["", "Attached:"]
            note_lines += [f"- {name}" for name in attached_names]
        if failed_names:
            note_lines += ["", "Not attached:"]
            for row in case_level_rows:
                if row.get("Zendesk_Upload_Status") == "failed":
                    name = s(row.get("File_Name")) or "Unknown file"
                    note_lines.append(f"- {name}")
                    note_lines.append(f"  URL: {best_s3_failure_url(row) or 'Not available'}")

        note = {
            "author_id": int(akarsh_user_id),
            "public": False,
            "value": "\n".join(note_lines),
        }
        if tokens:
            note["uploads"] = tokens
        comments.append(note)

    # Historical-comment uploads can fail after the S3 export succeeded. Record
    # those failures as a separate private/internal note with the URL.
    if failed_upload_rows:
        comments.append({
            "author_id": int(akarsh_user_id),
            "public": False,
            "value": s3_failure_note_text(failed_upload_rows),
        })


def summarize_s3_mapping(mapping_rows: List[Dict[str, Any]]) -> Dict[str, int]:
    total = len(mapping_rows)
    passed = sum(1 for r in mapping_rows if s(r.get("Preflight_Status")) == "success")
    failed = total - passed
    return {"total": total, "passed": passed, "failed": failed}


def execute_migration(
    source_dir: Path,
    mapping_path: Path,
    case_mapping_path: Path,
    output_dir: Path,
    zd: ZendeskClient,
    archive_immediately: bool,
    allow_s3_failures: bool = False,
) -> None:
    # S3 preflight is informational for migration: failed attachments do not block ticket imports.
    s3_mapping_rows = load_s3_mapping_rows(mapping_path)
    s3_summary = summarize_s3_mapping(s3_mapping_rows)
    print()
    print("S3 PRE-IMPORT CHECK")
    print(f"  Total S3 attachments : {s3_summary['total']}")
    print(f"  Ready for migration  : {s3_summary['passed']}")
    print(f"  Failed preflight     : {s3_summary['failed']}")
    print(f"  Mapping file         : {mapping_path}")
    if s3_summary["failed"]:
        print(
            "WARNING: continuing with ticket migration. "
            f"{s3_summary['failed']} S3 attachment(s) failed export/download; "
            "their URLs will be recorded in private ticket notes."
        )
    else:
        print("S3 preflight passed. Ticket import may begin.")

    cases, _, ctx = prepare_local_migration(source_dir, case_mapping_path, output_dir)
    mapping: MappingWorkbook = ctx["mapping"]
    aliases: CustomerAliasManager = ctx["aliases"]
    protected_agent_emails: set[str] = ctx["protected_agent_emails"]
    contacts_by_id: Dict[str, Dict[str, Any]] = ctx["contacts_by_id"]
    accounts_by_id: Dict[str, Dict[str, Any]] = ctx["accounts_by_id"]
    users: List[Dict[str, Any]] = ctx["users"]
    record_types: List[Dict[str, Any]] = ctx.get("record_types", [])
    emails_by_case: Dict[str, List[Dict[str, Any]]] = ctx["emails_by_case"]
    customer_identity: Dict[str, Dict[str, Any]] = ctx["customer_identity"]
    state_dir: Path = ctx["state_dir"]

    zd.authenticate()
    print("Zendesk OAuth authentication successful.")

    akarsh = zd.find_user_by_email(AGENT_COMMENT_EMAIL)
    if not akarsh:
        raise RuntimeError(
            f"Zendesk agent not found for {AGENT_COMMENT_EMAIL}. The test requires this existing agent."
        )
    akarsh_user_id = int(akarsh["id"])
    print(f"Using Akarsh as the only agent comment author. Zendesk user ID: {akarsh_user_id}")

    owner_assignee_map: Dict[str, int] = {}
    owner_ids = sorted({s(c.get("OwnerId")) for c in cases if s(c.get("OwnerId"))})
    users_by_id = {s(u.get("Id")): u for u in users if s(u.get("Id"))}
    for owner_id in owner_ids:
        sf_owner = users_by_id.get(owner_id)
        if not sf_owner or not valid_email(sf_owner.get("Email")):
            continue
        zd_owner = zd.find_user_by_email(s(sf_owner.get("Email")))
        if zd_owner and zd_owner.get("id"):
            owner_assignee_map[owner_id] = int(zd_owner["id"])
        else:
            print(f"WARNING: Salesforce Case Owner {sf_owner.get('Name')} ({sf_owner.get('Email')}) was not found in Zendesk; assignee will remain blank.")

    group_map: Dict[str, int] = {}
    group_values = sorted({s(c.get("BP_Group__c")) for c in cases if s(c.get("BP_Group__c"))})
    for group_value in group_values:
        lookup_name = mapped_zendesk_value(mapping, "BP_Group__c", group_value) or group_value
        zd_group = zd.find_group_by_name(lookup_name)
        if zd_group and zd_group.get("id"):
            group_map[normalize_label(group_value)] = int(zd_group["id"])
            group_map[normalize_label(lookup_name)] = int(zd_group["id"])

    brand_map: Dict[str, int] = {}
    product_values = sorted({s(c.get("BP_Product__c")) for c in cases if s(c.get("BP_Product__c"))})
    for product_value in product_values:
        lookup_name = mapped_zendesk_value(mapping, "BP_Product__c", product_value) or product_value
        zd_brand = zd.find_brand_by_name(lookup_name)
        if zd_brand and zd_brand.get("id"):
            brand_map[normalize_label(product_value)] = int(zd_brand["id"])
            brand_map[normalize_label(lookup_name)] = int(zd_brand["id"])

    record_types_by_id = {s(r.get("Id")): r for r in record_types if s(r.get("Id"))}

    org_state_path = state_dir / "organization_map.json"
    user_state_path = state_dir / "customer_user_map.json"
    ticket_state_path = state_dir / "ticket_map.json"
    org_map: Dict[str, int] = load_json(org_state_path) if org_state_path.exists() else {}
    customer_user_map: Dict[str, int] = load_json(user_state_path) if user_state_path.exists() else {}
    ticket_map: Dict[str, Any] = load_json(ticket_state_path) if ticket_state_path.exists() else {}

    results: List[Dict[str, Any]] = []

    # Organization creation/update.
    for account_id, account in accounts_by_id.items():
        external_id = f"sf_account_{account_id}"
        existing_id = org_map.get(account_id)
        try:
            if existing_id:
                org_map[account_id] = int(existing_id)
                continue
            org = zd.find_organization_by_external_id(external_id)
            if not org:
                org = zd.create_or_update_organization(s(account.get("Name")), external_id)
            org_map[account_id] = int(org["id"])
            results.append({"object": "organization", "source_id": account_id, "zendesk_id": org["id"], "status": "completed", "message": ""})
        except Exception as exc:
            results.append({"object": "organization", "source_id": account_id, "zendesk_id": "", "status": "failed", "message": str(exc)})
            raise
    save_json(org_state_path, org_map)

    # End users.
    for fp, identity in customer_identity.items():
        if fp in customer_user_map:
            continue
        customer_email = identity.get("customer_email") or identity.get("source_email_for_lookup")
        name = identity["name"]
        external_id = f"sf_customer_{fp}"
        organization_id = None
        for case in cases:
            contact = contacts_by_id.get(s(case.get("ContactId"))) if s(case.get("ContactId")) else None
            source_email = customer_source_email(case, contact, emails_by_case.get(s(case.get("Id")), []), protected_agent_emails)
            if not source_email:
                source_email = f"case-{s(case.get('Id'))}@source.invalid"
            if aliases.fingerprint(source_email) == fp and s(case.get("AccountId")) in org_map:
                organization_id = int(org_map[s(case.get("AccountId"))])
                break
        try:
            user = zd.find_or_create_end_user(name, customer_email, external_id, organization_id)
            customer_user_map[fp] = int(user["id"])
            results.append({"object": "end_user", "source_id": fp, "zendesk_id": user["id"], "status": "completed", "message": customer_email})
        except Exception as exc:
            results.append({"object": "end_user", "source_id": fp, "zendesk_id": "", "status": "failed", "message": str(exc)})
            raise
    save_json(user_state_path, customer_user_map)

    customer_user_by_source_email: Dict[str, int] = {}
    for fp, identity in customer_identity.items():
        uid = customer_user_map.get(fp)
        if uid:
            source_email = identity.get("source_email_for_lookup", "")
            if source_email and "@" in source_email:
                customer_user_by_source_email[norm(source_email)] = int(uid)

    prep_report: List[Dict[str, Any]] = []
    custom_field_report: List[Dict[str, Any]] = []
    payloads: List[Dict[str, Any]] = []
    audits: List[Dict[str, Any]] = []
    for case in cases:
        payload, audit = build_ticket_payload(
            case,
            emails_by_case.get(s(case.get("Id")), []),
            [],
            mapping,
            contacts_by_id,
            accounts_by_id,
            customer_user_map,
            customer_user_by_source_email,
            org_map,
            owner_assignee_map,
            group_map,
            brand_map,
            record_types_by_id,
            akarsh_user_id,
            aliases,
            protected_agent_emails,
            prep_report,
            custom_field_report,
        )
        payloads.append(payload)
        audits.append(audit)

    write_payload_outputs(output_dir, payloads, audits, prep_report, custom_field_report)

    # Group mapping rows by Case ID.
    s3_by_case: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in s3_mapping_rows:
        cid = s(row.get("Salesforce_Case_ID"))
        if cid:
            s3_by_case[cid].append(row)

    migration_report_rows: List[Dict[str, Any]] = []
    migration_report_path = output_dir / S3_MIGRATION_REPORT_FILENAME

    # Import tickets only now, after successful S3 preflight (or explicit override).
    for payload, audit, case in zip(payloads, audits, cases):
        sf_case_id = audit["salesforce_case_id"]
        case_mapping_rows = s3_by_case.get(sf_case_id, [])
        existing_ticket_id: Optional[int] = None

        if sf_case_id in ticket_map:
            existing_ticket_id = int(ticket_map[sf_case_id].get("zendesk_id") or 0) or None
        else:
            existing = zd.find_ticket_by_external_id(sf_case_id)
            if existing and existing.get("id"):
                existing_ticket_id = int(existing["id"])
                ticket_map[sf_case_id] = {"zendesk_id": existing_ticket_id, "status": "existing"}

        if existing_ticket_id:
            # Existing tickets cannot have files attached to old comments through the API.
            # Add files as a new private note.
            failed_rows = [r for r in case_mapping_rows if s(r.get("Preflight_Status")) != "success"]
            if failed_rows:
                zd.add_internal_comment(existing_ticket_id, s3_failure_note_text(failed_rows))
            successful_rows = [r for r in case_mapping_rows if s(r.get("Preflight_Status")) == "success"]
            if successful_rows:
                temp_payload = {"comments": []}
                apply_s3_mapping_to_payload(temp_payload, sf_case_id, successful_rows, zd, migration_report_rows, akarsh_user_id)
                for comment in temp_payload.get("comments", []):
                    safe_comment = strip_private_metadata(comment)
                    if safe_comment.get("uploads"):
                        zd.add_internal_comment_with_uploads(
                            existing_ticket_id,
                            safe_comment.get("value") or "S3 Attachments from Salesforce",
                            safe_comment["uploads"],
                        )
                        for row in successful_rows:
                            if row.get("Zendesk_Upload_Status") == "success":
                                row["Zendesk_Attachment_Status"] = "attached"
                write_csv(migration_report_path, migration_report_rows)
            write_s3_failure_excel(output_dir, s3_mapping_rows, migration_report_rows)
            results.append({"object": "ticket", "source_id": sf_case_id, "zendesk_id": existing_ticket_id, "status": "existing", "message": "Ticket already exists"})
            continue

        try:
            before_report_len = len(migration_report_rows)
            apply_s3_mapping_to_payload(
                payload,
                sf_case_id,
                case_mapping_rows,
                zd,
                migration_report_rows,
                akarsh_user_id,
            )
            # Mark failures from Zendesk upload in the report but continue with ticket import.
            ticket_payload_for_api = strip_private_metadata(payload)
            ticket = zd.import_ticket(ticket_payload_for_api, archive_immediately=archive_immediately)
            zd_id = int(ticket["id"])
            for row in migration_report_rows[before_report_len:]:
                if row.get("Zendesk_Upload_Status") == "success":
                    row["Zendesk_Attachment_Status"] = "attached"
                    row["Zendesk_Ticket_ID"] = zd_id
                else:
                    row["Zendesk_Ticket_ID"] = zd_id
            ticket_map[sf_case_id] = {
                "zendesk_id": zd_id,
                "status": "completed",
                "comment_count": audit["comment_count"],
                "s3_attachment_count": len(case_mapping_rows),
            }
            results.append({
                "object": "ticket",
                "source_id": sf_case_id,
                "zendesk_id": zd_id,
                "status": "completed",
                "message": f"comments={audit['comment_count']}; custom_fields={audit['custom_field_count']}; s3_records={len(case_mapping_rows)}",
            })
            save_json(ticket_state_path, ticket_map)
            write_csv(migration_report_path, migration_report_rows)
            write_s3_failure_excel(output_dir, s3_mapping_rows, migration_report_rows)
            print(f"Imported Case {audit['case_number']} -> Zendesk Ticket {zd_id}")
        except Exception as exc:
            results.append({
                "object": "ticket",
                "source_id": sf_case_id,
                "zendesk_id": "",
                "status": "failed",
                "message": str(exc),
            })
            write_csv(output_dir / "migration_results.csv", results)
            write_csv(migration_report_path, migration_report_rows)
            write_s3_failure_excel(output_dir, s3_mapping_rows, migration_report_rows)
            raise

    write_csv(output_dir / "migration_results.csv", results)
    customer_email_rows = [
        {
            "customer_fingerprint": fp,
            "customer_email": identity.get("source_email_for_lookup", ""),
            "customer_name": identity.get("name", ""),
        }
        for fp, identity in sorted(customer_identity.items())
    ]
    write_csv(output_dir / "customer_emails.csv", customer_email_rows)

    print()
    print(f"Migration completed for {MAX_TICKETS if MAX_TICKETS > 0 else 'ALL'} Cases in the selected range.")
    print(f"Output folder: {output_dir}")
    print(f"Payload:       {output_dir / 'tickets_payload.json'}")
    print(f"Results:       {output_dir / 'migration_results.csv'}")
    print(f"S3 mapping:    {mapping_path}")
    print(f"S3 report:     {migration_report_path}")
    print(f"S3 failure log:{output_dir / S3_FAILURE_EXCEL_FILENAME}")



# =============================================================================
# Modular migration workflow
# =============================================================================
# The modular scripts intentionally do not use execute_migration(). They split the
# migration into four restartable stages so S3 export/upload can be verified before
# ticket import starts.

MODULAR_WORKSPACE_DEFAULT = "migration_workspace"


def modular_workspace(cfg: Dict[str, Any], script_dir: Path) -> Path:
    value = s(cfg.get("workspace_dir")) or s(cfg.get("output_dir")) or MODULAR_WORKSPACE_DEFAULT
    path = Path(value)
    if not path.is_absolute():
        path = script_dir / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def modular_mapping_path(cfg: Dict[str, Any], script_dir: Path) -> Path:
    value = s(cfg.get("mapping_file")) or "Salesforce_to_Zendesk_Case_Field_Mapping.xlsx"
    path = Path(value)
    if path.is_absolute():
        return path

    candidates = [
        script_dir / path,
        script_dir.parent / path,
        Path.cwd() / path,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    # Return the primary expected path so the eventual error is precise.
    return candidates[0]


def s3_target_base_url(cfg: Dict[str, Any]) -> str:
    s3_cfg = cfg.get("s3", {}) if isinstance(cfg, dict) else {}
    return s(os.getenv("S3_BASE_URL")) or s(s3_cfg.get("base_url")) or s(s3_cfg.get("target_base_url"))


def construct_migration_s3_url(raw_url: str, target_base_url: str) -> str:
    """Resolve S3 references using config; never rewrite a complete URL."""
    raw = s(raw_url)
    base = s(target_base_url)
    if not raw:
        return ""

    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()

    if scheme in {"http", "https"} and parsed.netloc:
        if "/Prod" in parsed.path and "/Prod/" not in parsed.path:
            return raw.replace("/Prod", "/Prod/", 1)
        return raw

    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()

    # A complete HTTP(S) URL from Salesforce is authoritative and is preserved.
    if scheme in {"http", "https"} and parsed.netloc:
        return raw

    # Native s3://bucket/key references are converted only when a configured
    # HTTPS base URL is available. Otherwise the native reference is retained.
    if scheme == "s3" and parsed.netloc:
        if not base:
            return raw
        key = unquote(parsed.path.lstrip("/"))
    else:
        if not base:
            raise RuntimeError("S3 base_url is not configured and Salesforce did not provide a complete URL.")
        key = raw.lstrip("/")

    if not key:
        return raw
    return f"{base.rstrip('/')}/{quote(key, safe='/-_.~()')}"


def modular_max_tickets(cfg: Dict[str, Any], override: Optional[int] = None) -> int:
    value = override if override is not None else cfg.get("max_tickets", 0)
    value = 0 if value is None else int(value)
    if value < 0:
        raise RuntimeError("max_tickets cannot be negative; use 0 for all Cases.")
    return value


def download_s3_from_export_modular(cfg: Dict[str, Any], script_dir: Path) -> Dict[str, Any]:
    """Download exported S3 files with bounded concurrency and resumable successes."""
    workspace = modular_workspace(cfg, script_dir)
    source_dir = workspace / "salesforce_export"
    out_dir = workspace / "attachments"
    files_dir = out_dir / "files"
    out_dir.mkdir(parents=True, exist_ok=True)
    files_dir.mkdir(parents=True, exist_ok=True)

    cases = read_csv(source_dir / "Cases.csv")
    records = read_csv(source_dir / "S3Records.csv")
    case_by_id = {s(c.get("Id")): c for c in cases if s(c.get("Id"))}
    records = [r for r in records if s(r.get("BP_Case__c")) in case_by_id]

    access, secret, region = get_aws_config(cfg)
    downloader = S3Downloader(access, secret, region)
    target_base = s3_target_base_url(cfg)
    if not target_base:
        raise RuntimeError("Set S3_BASE_URL in environment or .env file.")

    csv_path = out_dir / "S3_Attachment_Export.csv"
    excel_path = out_dir / "S3_Attachment_Export.xlsx"
    # Resume only successful items from the previous manifest when their local file exists.
    prior_success: Dict[str, Dict[str, Any]] = {}
    if csv_path.exists():
        try:
            for old in read_csv(csv_path):
                old_url = s(old.get("Migration_S3_URL"))
                old_local = s(old.get("Local_File_Path"))
                if old_url and old_local and s(old.get("Download_Status")).lower() == "success":
                    local = (workspace / old_local).resolve()
                    try:
                        local.relative_to(workspace.resolve())
                        if local.is_file():
                            prior_success[old_url] = old
                    except (ValueError, OSError):
                        pass
        except Exception as exc:
            print(f"[S3] Previous manifest could not be reused; downloading normally: {exc}", flush=True)

    def base_row(record: Dict[str, Any]) -> Dict[str, Any]:
        case_id = s(record.get("BP_Case__c"))
        case = case_by_id.get(case_id, {})
        source_url = s(record.get("BP_S3_URL__c")) or s(record.get("BP_Fallback_URL__c"))
        migration_url = construct_migration_s3_url(source_url, target_base) if source_url else ""
        comment_id, comment_source = extract_comment_id(record)
        row = {
            "Salesforce_Case_ID": case_id,
            "CaseNumber": s(case.get("CaseNumber")),
            "CaseSubject": s(case.get("Subject")),
            "S3_Record_ID": s(record.get("Id")),
            "Salesforce_Comment_ID": comment_id,
            "Comment_ID_Source": comment_source,
            "S3_Name": s(record.get("Name")),
            "Original_S3_URL": source_url,
            "Migration_S3_URL": migration_url,
            "Local_File_Path": "",
            "Bucket": "",
            "Key": "",
            "Download_Status": "failed",
            "File_Size_Bytes": "",
            "Content_Type": "",
            "SHA256": "",
            "Error": "",
            "Exported_At_UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        }
        return row

    row_templates = [base_row(r) for r in records]
    unique_jobs: Dict[str, Dict[str, Any]] = {}
    results_by_url: Dict[str, Dict[str, Any]] = {}

    for record, row in zip(records, row_templates):
        url = s(row.get("Migration_S3_URL"))
        if not url:
            row["Error"] = "No usable S3 URL found"
            continue
        if url in unique_jobs or url in results_by_url:
            continue
        prior = prior_success.get(url)
        if prior:
            local = (workspace / s(prior.get("Local_File_Path"))).resolve()
            try:
                local.relative_to(workspace.resolve())
                if local.is_file():
                    bucket = s(prior.get("Bucket"))
                    key = s(prior.get("Key"))
                    if not bucket or not key:
                        bucket, key = parse_s3_url(url)
                    results_by_url[url] = {
                        "Bucket": bucket, "Key": key,
                        "Local_File_Path": s(prior.get("Local_File_Path")),
                        "Download_Status": "success",
                        "File_Size_Bytes": s(prior.get("File_Size_Bytes")) or local.stat().st_size,
                        "Content_Type": s(prior.get("Content_Type")) or mimetypes.guess_type(local.name)[0] or "application/octet-stream",
                        "SHA256": s(prior.get("SHA256")), "Error": "",
                        "_reused": True,
                    }
                    continue
            except (ValueError, OSError, Exception):
                pass
        unique_jobs[url] = record

    print(
        f"[S3] {len(records):,} records; {len(unique_jobs):,} unique URLs to download; "
        f"{sum(1 for v in results_by_url.values() if v.get('_reused')):,} prior downloads reusable; "
        f"6 parallel workers.", flush=True
    )

    def download_one(url: str, record: Dict[str, Any]) -> Dict[str, Any]:
        bucket, key = parse_s3_url(url)
        case_id = s(record.get("BP_Case__c"))
        case_folder = safe_local_filename(case_id or "unknown_case")
        name = safe_local_filename(s(record.get("Name")) or Path(key).name or f"attachment_{s(record.get('Id'))}")
        filename = safe_local_filename(name, prefix=sha12(s(record.get("Id")) or url)[-8:])
        local_path = files_dir / case_folder / filename
        local_path.parent.mkdir(parents=True, exist_ok=True)
        transfer_config = TransferConfig(max_concurrency=2, use_threads=True)
        metadata = downloader.download(url, local_path, transfer_config=transfer_config)
        digest = hashlib.sha256()
        with local_path.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        content_type = s(metadata.get("content_type")) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return {
            "Bucket": bucket, "Key": key,
            "Local_File_Path": str(local_path.relative_to(workspace)),
            "Download_Status": "success",
            "File_Size_Bytes": metadata.get("size", local_path.stat().st_size),
            "Content_Type": content_type,
            "SHA256": digest.hexdigest(), "Error": "", "_reused": False,
        }

    if unique_jobs:
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(download_one, url, record): url for url, record in unique_jobs.items()}
            done = 0
            total = len(futures)
            for future in as_completed(futures):
                url = futures[future]
                try:
                    results_by_url[url] = future.result()
                except Exception as exc:
                    results_by_url[url] = {"Download_Status": "failed", "Error": str(exc),
                                           "Bucket": "", "Key": "", "Local_File_Path": "",
                                           "File_Size_Bytes": "", "Content_Type": "", "SHA256": "", "_reused": False}
                done += 1
                if done == 1 or done % 25 == 0 or done == total:
                    successes = sum(1 for v in results_by_url.values() if v.get("Download_Status") == "success")
                    print(f"[S3] completed {done:,}/{total:,} unique downloads; {successes:,} unique successes so far.", flush=True)

    rows: List[Dict[str, Any]] = []
    for row in row_templates:
        url = s(row.get("Migration_S3_URL"))
        if not url:
            rows.append(row)
            continue
        result = results_by_url.get(url)
        if result:
            for key in ("Bucket", "Key", "Local_File_Path", "Download_Status", "File_Size_Bytes", "Content_Type", "SHA256", "Error"):
                if key in result:
                    row[key] = result[key]
        else:
            row["Download_Status"] = "failed"
            row["Error"] = "Download result unavailable"
        rows.append(row)

    modular_excel_write(excel_path, rows, sheet_name="S3 Attachments")
    write_csv(csv_path, rows)
    summary = {
        "cases": len(cases),
        "s3_records": len(records),
        "unique_urls": len({s(r.get("Migration_S3_URL")) for r in rows if s(r.get("Migration_S3_URL"))}),
        "reused_downloads": sum(1 for r in results_by_url.values() if r.get("_reused")),
        "download_success": sum(1 for r in rows if s(r.get("Download_Status")) == "success"),
        "download_failed": sum(1 for r in rows if s(r.get("Download_Status")) != "success"),
        "base_url": target_base,
        "excel_file": str(excel_path),
    }
    write_json_compact(out_dir / "S3_Attachment_Export_Summary.json", summary)
    print(f"[S3] Finished: {summary['download_success']:,} successful rows, {summary['download_failed']:,} failed rows.", flush=True)
    return summary


def _modular_zendesk_client(cfg: Dict[str, Any]) -> ZendeskClient:
    zd_cfg = cfg.get("zendesk", {}) if isinstance(cfg, dict) else {}
    subdomain = s(os.getenv("ZD_SUBDOMAIN")) or s(zd_cfg.get("subdomain"))
    client_id = s(os.getenv("ZD_CLIENT_ID")) or s(zd_cfg.get("client_id"))
    client_secret = s(os.getenv("ZD_CLIENT_SECRET")) or s(zd_cfg.get("client_secret"))
    raw_scope = s(os.getenv("ZD_SCOPE")).strip('"\'')
    scope = raw_scope or s(zd_cfg.get("scope")) or "tickets:read tickets:write users:read users:write organizations:read organizations:write groups:read brands:read"
    if not subdomain or not client_id or not client_secret:
        raise RuntimeError("Set Zendesk subdomain, client_id and client_secret in environment variables (ZD_SUBDOMAIN, ZD_CLIENT_ID, ZD_CLIENT_SECRET)")
    zd = ZendeskClient(subdomain, client_id, client_secret, scope)
    zd.authenticate()
    return zd


def migrate_entities_modular(cfg: Dict[str, Any], script_dir: Path, mapping_path: Path) -> Dict[str, Any]:
    """Single entity stage: Organizations first, then End Users using the organization map."""
    workspace = modular_workspace(cfg, script_dir)
    source_dir = workspace / "salesforce_export"
    state_dir = workspace / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    out_dir = workspace / "entity_migration"
    out_dir.mkdir(parents=True, exist_ok=True)

    accounts = read_csv(source_dir / "Accounts.csv")
    contacts = read_csv(source_dir / "Contacts.csv")
    cases = read_csv(source_dir / "Cases.csv")
    emails = read_csv(source_dir / "EmailMessages.csv")
    users = read_csv(source_dir / "Salesforce_Users.csv")

    zd = _modular_zendesk_client(cfg)
    org_state_path = state_dir / "organization_map.json"
    org_map: Dict[str, int] = load_json(org_state_path) if org_state_path.exists() else {}
    org_rows: List[Dict[str, Any]] = []

    for account in accounts:
        aid = s(account.get("Id"))
        if not aid:
            continue
        external_id = f"sf_account_{aid}"
        try:
            existing = zd.find_organization_by_external_id(external_id)
            org = existing or zd.create_or_update_organization(s(account.get("Name")), external_id)
            org_map[aid] = int(org["id"])
            org_rows.append({"salesforce_account_id": aid, "zendesk_organization_id": int(org["id"]), "status": "existing" if existing else "completed"})
        except Exception as exc:
            org_rows.append({"salesforce_account_id": aid, "zendesk_organization_id": "", "status": "failed", "message": str(exc)})
    save_json(org_state_path, org_map)
    modular_excel_write(out_dir / "Organization_Migration_Log.xlsx", org_rows, sheet_name="Organizations")
    write_csv(out_dir / "Organization_Migration_Log.csv", org_rows)

    contacts_by_id = {s(c.get("Id")): c for c in contacts if s(c.get("Id"))}
    emails_by_case: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for msg in emails:
        if s(msg.get("ParentId")):
            emails_by_case[s(msg.get("ParentId"))].append(msg)

    protected = {norm(u.get("Email")) for u in users if valid_email(u.get("Email"))}
    protected.add(norm(AGENT_COMMENT_EMAIL))
    aliases = CustomerAliasManager(state_dir / "customer_email_aliases.json")

    identities: Dict[str, Dict[str, Any]] = {}
    for case in cases:
        cid = s(case.get("ContactId"))
        contact = contacts_by_id.get(cid) if cid else None
        case_messages = emails_by_case.get(s(case.get("Id")), [])
        source_email = customer_source_email(case, contact, case_messages, protected)
        phone = s((contact or {}).get("Phone")) or s((contact or {}).get("MobilePhone")) or s(case.get("SuppliedPhone"))
        case_id = s(case.get("Id"))
        if valid_email(source_email):
            if cid:
                identity_key = f"contact:{cid}"
            else:
                identity_key = f"email:{norm(source_email)}"
            identities.setdefault(identity_key, {
                "source_email": source_email,
                "name": user_name(case, contact),
                "account_id": s(case.get("AccountId")),
                "phone": phone,
                "salesforce_contact_id": cid,
                "case_id": case_id,
            })
        else:
            # Phone/no-email identity. Use the real phone number as the Zendesk
            # phone identity and a deterministic synthetic email only because
            # the Zendesk user record also needs a stable email identity.
            identity_key = f"case:{case_id}"
            identities.setdefault(identity_key, {
                "source_email": f"sfcase_{sha12(case_id)}@{SYNTHETIC_USER_DOMAIN}",
                "name": user_name(case, contact),
                "account_id": s(case.get("AccountId")),
                "phone": phone,
                "salesforce_contact_id": cid,
                "case_id": case_id,
                "synthetic_email": True,
            })

        # Incoming EmailMessage senders can be different from Case.Contact.
        # Preserve those real customer identities so their historical comments
        # can resolve to the correct Zendesk author.
        for msg in case_messages:
            if not bool_value(msg.get("Incoming")):
                continue
            incoming_email = first_email(msg.get("FromAddress"))
            if not valid_email(incoming_email) or norm(incoming_email) in protected:
                continue
            identity_key = f"email:{norm(incoming_email)}"
            identities.setdefault(identity_key, {
                "source_email": incoming_email,
                "name": s(msg.get("FromName")) or "Salesforce Customer",
                "account_id": s(case.get("AccountId")),
                "phone": s((contact or {}).get("Phone")) or s((contact or {}).get("MobilePhone")),
                "salesforce_contact_id": cid,
            })

    user_state_path = state_dir / "customer_user_map.json"
    user_email_state_path = state_dir / "customer_user_by_source_email.json"
    user_state: Dict[str, int] = load_json(user_state_path) if user_state_path.exists() else {}
    email_map: Dict[str, int] = load_json(user_email_state_path) if user_email_state_path.exists() else {}
    user_rows: List[Dict[str, Any]] = []

    for identity_key, identity in sorted(identities.items()):
        source_email = valid_email(identity.get("source_email"))
        phone = s(identity.get("phone"))
        if not source_email and not phone:
            # Deterministic fallback for a Salesforce case with neither email nor phone.
            case_id = s(identity.get("case_id")) or identity_key
            source_email = f"sfcase_{sha12(case_id)}@{SYNTHETIC_USER_DOMAIN}"
        if not source_email:
            source_email = f"sfcase_{sha12(identity_key)}@{SYNTHETIC_USER_DOMAIN}"
        external_id = "sf_customer_" + sha12(identity_key)
        try:
            existing = zd.find_user_by_external_id(external_id)
            org_id = org_map.get(s(identity.get("account_id")))
            user = existing
            if not user and not identity.get("synthetic_email"):
                user = zd.find_user_by_email(source_email)
            phone_warning = ""
            if not user:
                try:
                    user = zd.create_end_user(
                        identity.get("name") or "Imported User",
                        source_email,
                        external_id,
                        int(org_id) if org_id else None,
                        phone=phone or None,
                    )
                except Exception as create_error:
                    # Do not guess or manufacture a country code. If Zendesk
                    # specifically rejects the phone format, retry once without
                    # the phone so the requester can still be migrated.
                    error_text = str(create_error)
                    is_phone_validation_error = (
                        "HTTP 422" in error_text
                        and "phone" in error_text.lower()
                        and any(term in error_text.lower() for term in (
                            "properly formatted", "e.164", "invalid phone", "phone number"
                        ))
                    )
                    if not phone or not is_phone_validation_error:
                        raise
                    user = zd.create_end_user(
                        identity.get("name") or "Imported User",
                        source_email,
                        external_id,
                        int(org_id) if org_id else None,
                        phone=None,
                    )
                    phone_warning = (
                        "User created without phone because Zendesk rejected the "
                        "source phone format; no country code was assumed. "
                        f"Original phone retained in migration log: {phone}"
                    )
            uid = int(user["id"])
            user_state[identity_key] = uid
            email_map[norm(source_email)] = uid
            user_rows.append({
                "identity_key": identity_key,
                "salesforce_contact_id": identity.get("salesforce_contact_id", ""),
                "salesforce_case_id": identity.get("case_id", ""),
                "source_email": source_email,
                "zendesk_email": s(user.get("email")) or source_email,
                "phone": identity.get("phone", ""),
                "organization_id": org_id or "",
                "zendesk_user_id": uid,
                "status": "existing" if existing else "completed",
                "message": phone_warning,
            })
        except Exception as exc:
            user_rows.append({"identity_key": identity_key, "source_email": source_email, "status": "failed", "message": str(exc)})

    save_json(user_state_path, user_state)
    save_json(user_email_state_path, email_map)
    modular_excel_write(out_dir / "End_User_Migration_Log.xlsx", user_rows, sheet_name="End Users")
    write_csv(out_dir / "End_User_Migration_Log.csv", user_rows)

    return {
        "organizations": len(accounts),
        "organizations_success": sum(1 for r in org_rows if r.get("status") in {"completed", "existing"}),
        "organizations_failed": sum(1 for r in org_rows if r.get("status") == "failed"),
        "end_users": len(identities),
        "end_users_success": sum(1 for r in user_rows if r.get("status") in {"completed", "existing"}),
        "end_users_failed": sum(1 for r in user_rows if r.get("status") == "failed"),
    }




def _runtime_attachment_rows(workspace: Path) -> Dict[str, List[Dict[str, Any]]]:
    path = workspace / "attachments" / "S3_Attachment_Export.xlsx"
    if not path.exists():
        return {}
    rows = modular_excel_read(path)
    result: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cid = s(row.get("Salesforce_Case_ID"))
        if cid:
            result[cid].append(row)
    return result


def _runtime_attach_files_to_payload(
    payload: Dict[str, Any], case_rows: List[Dict[str, Any]], zd: ZendeskClient, akarsh_user_id: int,
    workspace: Path, migration_report_rows: List[Dict[str, Any]],
) -> None:
    """Upload each file immediately before ticket creation. No token is persisted for later use."""
    if not case_rows:
        return
    comments = payload.setdefault("comments", [])
    comment_objects = {
        s(c.get("_salesforce_email_message_id")): c for c in comments
        if isinstance(c, dict) and s(c.get("_salesforce_email_message_id"))
    }
    case_level: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    by_comment: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for row in case_rows:
        if s(row.get("Download_Status")) != "success":
            failed.append(row)
            continue
        cid = s(row.get("Salesforce_Comment_ID"))
        if cid:
            by_comment[cid].append(row)
        else:
            case_level.append(row)

    def upload(row: Dict[str, Any]) -> Optional[str]:
        relative = s(row.get("Local_File_Path"))
        local_path = workspace / relative if relative and not Path(relative).is_absolute() else Path(relative)
        filename = s(row.get("S3_Name")) or s(row.get("File_Name")) or local_path.name
        mime = s(row.get("Content_Type")) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            token = zd.upload_file(local_path, filename, mime)
            result = dict(row)
            result.update({"Zendesk_Upload_Status": "success", "Zendesk_Attachment_Status": "attached", "Upload_Error": ""})
            migration_report_rows.append(result)
            return token
        except Exception as exc:
            result = dict(row)
            result.update({"Zendesk_Upload_Status": "failed", "Zendesk_Attachment_Status": "not_attached", "Upload_Error": str(exc)})
            migration_report_rows.append(result)
            failed.append(result)
            return None

    for comment_id, rows in by_comment.items():
        comment = comment_objects.get(comment_id)
        if not comment:
            case_level.extend(rows)
            continue
        for row in rows:
            token = upload(row)
            if token:
                comment.setdefault("uploads", []).append(token)

    if case_level:
        tokens: List[str] = []
        names: List[str] = []
        for row in case_level:
            token = upload(row)
            name = s(row.get("S3_Name")) or Path(s(row.get("Local_File_Path"))).name or "Attachment"
            if token:
                tokens.append(token); names.append(name)
        note_lines = ["Salesforce S3 Attachments", ""]
        if names:
            note_lines.append("Attachments:")
            note_lines.extend(f"- {n}" for n in names)
        note = {"author_id": int(akarsh_user_id), "public": False, "value": "\n".join(note_lines)}
        if tokens:
            note["uploads"] = tokens
        comments.append(note)

    # Failed downloads/uploads are represented by URLs in an internal note.
    if failed:
        comments.append({
            "author_id": int(akarsh_user_id),
            "public": False,
            "value": s3_failure_note_text(failed),
        })



# ============================================================================
# V6 MIGRATION AUDIT / RECONCILIATION OVERRIDES
# ============================================================================
# Production migration rules:
#   * Every source Case gets a preparation/import outcome.
#   * Salesforce Case.Id is the ONLY ticket external key.
#   * The imported Zendesk ticket external_id is read back and compared exactly.
#   * Attachment failures never hide a ticket-level success; they are warnings.
#   * One bad Case never stops the remaining Cases.
#   * Excel contains both summary counts and record-level failure reasons.

AUDIT_COLUMNS = [
    "Salesforce Case ID", "Case Number", "Migration Source", "Status",
    "Zendesk Ticket ID", "Salesforce Case ID Sent", "Zendesk External ID",
    "ID Match", "Attempts", "HTTP Status", "Error Type", "Failure Reason",
    "API/Error Detail", "Attachment Count", "Attachments Uploaded",
    "Attachments Failed", "Attachment Failure Reason", "Started At UTC",
    "Completed At UTC",
]


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _extract_http_status(message: Any) -> str:
    text = s(message)
    m = re.search(r"(?:HTTP|status(?:_code)?)\s*[:=]?\s*(\d{3})", text, re.I)
    return m.group(1) if m else ""


def _classify_error(message: Any) -> str:
    text = s(message).lower()
    status = _extract_http_status(message)
    if "external_id" in text and ("mismatch" in text or "does not match" in text):
        return "ID_MISMATCH"
    if status == "401" or "authentication" in text or "unauthorized" in text:
        return "AUTHENTICATION_ERROR"
    if status == "403" or "forbidden" in text or "access denied" in text:
        return "PERMISSION_ERROR"
    if status == "429" or "rate limit" in text or "too many requests" in text:
        return "RATE_LIMIT"
    if status and status.startswith("5"):
        return "ZENDESK_SERVER_ERROR"
    if "timeout" in text or "timed out" in text:
        return "REQUEST_TIMEOUT"
    if "connection" in text:
        return "CONNECTION_ERROR"
    if "requester" in text or "end user" in text:
        return "REQUESTER_ERROR"
    if "organization" in text:
        return "ORGANIZATION_ERROR"
    if "group" in text:
        return "GROUP_MAPPING_ERROR"
    if "brand" in text:
        return "BRAND_MAPPING_ERROR"
    if "custom field" in text or "field" in text or status == "422":
        return "FIELD_VALIDATION_ERROR"
    if "attachment" in text or "upload" in text:
        return "ATTACHMENT_ERROR"
    if "missing external_id" in text:
        return "MISSING_EXTERNAL_ID"
    return "MIGRATION_ERROR"


def _safe_error_detail(exc: Any) -> str:
    text = s(exc)
    # Keep logs useful but prevent huge API bodies from making the workbook unusable.
    return text[:5000]


def _get_ticket_for_validation(zd: ZendeskClient, ticket_id: int) -> Dict[str, Any]:
    response = zd.request("GET", f"/api/v2/tickets/{int(ticket_id)}.json")
    if response.status_code != 200:
        raise RuntimeError(
            f"Zendesk ticket validation lookup failed: HTTP {response.status_code}\n{response.text[:3000]}"
        )
    return response.json().get("ticket", {})


def _validate_ticket_external_id(
    zd: ZendeskClient, ticket_id: int, salesforce_case_id: str, ticket_response: Optional[Dict[str, Any]] = None
) -> str:
    """Return the exact Zendesk external_id after import; raise on mismatch."""
    ticket = ticket_response or {}
    external_id = s(ticket.get("external_id"))
    if not external_id:
        ticket = _get_ticket_for_validation(zd, ticket_id)
        external_id = s(ticket.get("external_id"))
    if external_id != s(salesforce_case_id):
        raise RuntimeError(
            f"Salesforce Case ID / Zendesk external_id mismatch. "
            f"Expected '{salesforce_case_id}', received '{external_id or '<blank>'}'."
        )
    return external_id


def _write_audit_workbook(path: Path, summary: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Metric", "Count"])
    for key, value in summary.items():
        ws.append([key, value])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for col in ws.columns:
        width = max(12, min(55, max(len(s(c.value)) for c in col) + 2))
        ws.column_dimensions[col[0].column_letter].width = width

    detail = wb.create_sheet("Detailed Results")
    detail.append(AUDIT_COLUMNS)
    for row in rows:
        detail.append([row.get(c, "") for c in AUDIT_COLUMNS])
    detail.freeze_panes = "A2"
    detail.auto_filter.ref = detail.dimensions
    for col in detail.columns:
        width = max(12, min(60, max(len(s(c.value)) for c in col) + 2))
        detail.column_dimensions[col[0].column_letter].width = width
    wb.save(path)


def _read_result_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return modular_excel_read(path)


def _write_overall_reconciliation(workspace: Path) -> None:
    """Build one final workbook from the standard + phone import logs."""
    out = workspace / "import"
    standard = _read_result_rows(out / "Standard_Migration_Results.xlsx")
    phone = _read_result_rows(out / "Phone_Migration_Results.xlsx")
    rows = standard + phone

    status_counts = defaultdict(int)
    error_counts = defaultdict(int)
    for row in rows:
        status_counts[s(row.get("Status")).lower() or "unknown"] += 1
        et = s(row.get("Error Type"))
        if et:
            error_counts[et] += 1

    summary: Dict[str, Any] = {
        "Total ticket records": len(rows),
        "Successfully imported": status_counts.get("completed", 0),
        "Already existed / resumed": status_counts.get("existing", 0),
        "Completed with attachment warnings": status_counts.get("completed_with_warnings", 0),
        "Failed": status_counts.get("failed", 0),
        "Manual review required": status_counts.get("manual_review_required", 0),
        "Exact Case.Id -> external_id matches": sum(1 for r in rows if s(r.get("ID Match")).upper() == "YES"),
        "External ID mismatches": error_counts.get("ID_MISMATCH", 0),
    }
    for error_type, count in sorted(error_counts.items()):
        summary[f"Failure - {error_type}"] = count

    _write_audit_workbook(out / "Migration_Summary.xlsx", summary, rows)
    write_csv(out / "Migration_Summary.csv", rows)
    write_json_compact(out / "Migration_Summary.json", summary)


def _build_audit_row(
    case_id: str,
    case_number: str,
    migration_source: str,
    status: str,
    started: str,
    completed: str = "",
    **kwargs: Any,
) -> Dict[str, Any]:
    row = {
        "Salesforce Case ID": case_id,
        "Case Number": case_number,
        "Migration Source": migration_source,
        "Status": status,
        "Zendesk Ticket ID": kwargs.get("zendesk_ticket_id", ""),
        "Salesforce Case ID Sent": kwargs.get("salesforce_case_id_sent", case_id),
        "Zendesk External ID": kwargs.get("zendesk_external_id", ""),
        "ID Match": kwargs.get("id_match", ""),
        "Attempts": kwargs.get("attempts", 0),
        "HTTP Status": kwargs.get("http_status", ""),
        "Error Type": kwargs.get("error_type", ""),
        "Failure Reason": kwargs.get("failure_reason", ""),
        "API/Error Detail": kwargs.get("error_detail", ""),
        "Attachment Count": kwargs.get("attachment_count", 0),
        "Attachments Uploaded": kwargs.get("attachments_uploaded", 0),
        "Attachments Failed": kwargs.get("attachments_failed", 0),
        "Attachment Failure Reason": kwargs.get("attachment_failure_reason", ""),
        "Started At UTC": started,
        "Completed At UTC": completed,
    }
    return row


def _attachment_stats_for_case(rows: List[Dict[str, Any]], case_id: str) -> Dict[str, Any]:
    selected = [r for r in rows if s(r.get("Salesforce_Case_ID")) == s(case_id)]
    failed = [r for r in selected if s(r.get("Zendesk_Upload_Status")) == "failed" or s(r.get("Download_Status")) != "success"]
    uploaded = [r for r in selected if s(r.get("Zendesk_Upload_Status")) == "success"]
    reasons = []
    for r in failed:
        reason = s(r.get("Upload_Error")) or s(r.get("Error")) or "Attachment unavailable"
        if reason and reason not in reasons:
            reasons.append(reason)
    return {
        "attachment_count": len(selected),
        "attachments_uploaded": len(uploaded),
        "attachments_failed": len(failed),
        "attachment_failure_reason": " | ".join(reasons)[:5000],
    }


# -----------------------------------------------------------------------------
# V6: ticket preparation with per-record audit logging
# -----------------------------------------------------------------------------
def prepare_ticket_payloads_modular(
    cfg: Dict[str, Any], script_dir: Path, mapping_path: Path,
    start_date: Optional[str], end_date: Optional[str], max_tickets: Optional[int] = None,
) -> Dict[str, Any]:
    workspace = modular_workspace(cfg, script_dir)
    source_dir = workspace / "salesforce_export"
    payload_dir = workspace / "payload"
    payload_dir.mkdir(parents=True, exist_ok=True)
    state_dir = workspace / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    global MAX_TICKETS
    MAX_TICKETS = modular_max_tickets(cfg, max_tickets)

    cases, _, ctx = prepare_local_migration(source_dir, mapping_path, payload_dir)
    mapping: MappingWorkbook = ctx["mapping"]
    aliases = CustomerAliasManager(state_dir / "customer_email_aliases.json")
    protected_agent_emails = set(ctx["protected_agent_emails"])
    contacts_by_id = ctx["contacts_by_id"]
    accounts_by_id = ctx["accounts_by_id"]
    users = ctx["users"]
    record_types_by_id = {s(r.get("Id")): r for r in ctx.get("record_types", []) if s(r.get("Id"))}
    emails_by_case = ctx["emails_by_case"]
    s3_by_case = ctx["s3_by_case"]
    org_map = load_json(state_dir / "organization_map.json") if (state_dir / "organization_map.json").exists() else {}
    customer_user_map = load_json(state_dir / "customer_user_map.json") if (state_dir / "customer_user_map.json").exists() else {}
    customer_user_by_source_email = load_json(state_dir / "customer_user_by_source_email.json") if (state_dir / "customer_user_by_source_email.json").exists() else {}
    phone_tasks = read_csv(source_dir / "Phone_Tasks.csv") if (source_dir / "Phone_Tasks.csv").exists() else []
    phone_tasks_by_case: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for task in phone_tasks:
        if s(task.get("WhatId")):
            phone_tasks_by_case[s(task.get("WhatId"))].append(task)

    zd = _modular_zendesk_client(cfg)
    akarsh = zd.find_user_by_email(AGENT_COMMENT_EMAIL)
    if not akarsh:
        raise RuntimeError(f"Zendesk agent not found for {AGENT_COMMENT_EMAIL}.")
    akarsh_user_id = int(akarsh["id"])

    users_by_id = {s(u.get("Id")): u for u in users if s(u.get("Id"))}
    agent_user_by_sf_user_id: Dict[str, int] = {}
    agent_user_by_email: Dict[str, int] = {}
    owner_assignee_map: Dict[str, int] = {}
    for sf_user_id, sf_user in users_by_id.items():
        email = valid_email(sf_user.get("Email"))
        if not email:
            continue
        z_agent = zd.find_user_by_email(email)
        if z_agent and z_agent.get("id"):
            agent_user_by_sf_user_id[sf_user_id] = int(z_agent["id"])
            agent_user_by_email[norm(email)] = int(z_agent["id"])
    for owner_id in sorted({s(c.get("OwnerId")) for c in cases if s(c.get("OwnerId"))}):
        if owner_id in agent_user_by_sf_user_id:
            owner_assignee_map[owner_id] = agent_user_by_sf_user_id[owner_id]

    group_map: Dict[str, int] = {}
    for raw in sorted({s(c.get("BP_Group__c")) for c in cases if s(c.get("BP_Group__c"))}):
        lookup = mapped_zendesk_value(mapping, "BP_Group__c", raw) or raw
        group = zd.find_group_by_name(lookup)
        if group and group.get("id"):
            group_map[normalize_label(raw)] = int(group["id"])
            group_map[normalize_label(lookup)] = int(group["id"])

    brand_map: Dict[str, int] = {}
    for raw in sorted({s(c.get("BP_Product__c")) for c in cases if s(c.get("BP_Product__c"))}):
        lookup = mapped_zendesk_value(mapping, "BP_Product__c", raw) or raw
        brand = zd.find_brand_by_name(lookup)
        if brand and brand.get("id"):
            brand_map[normalize_label(raw)] = int(brand["id"])
            brand_map[normalize_label(lookup)] = int(brand["id"])

    prep_report: List[Dict[str, Any]] = []
    custom_field_report: List[Dict[str, Any]] = []
    audits: List[Dict[str, Any]] = []
    payloads: List[Dict[str, Any]] = []
    prep_results: List[Dict[str, Any]] = []

    for case in cases:
        cid = s(case.get("Id"))
        case_number = s(case.get("CaseNumber"))
        source = "phone" if is_phone_case(case) else "standard"
        started = _utc_now_text()
        try:
            if not cid:
                raise RuntimeError("Salesforce Case.Id is blank; cannot create a migration external_id.")
            payload, audit = build_ticket_payload(
                case, emails_by_case.get(cid, []), s3_by_case.get(cid, []), mapping,
                contacts_by_id, accounts_by_id, customer_user_map,
                customer_user_by_source_email, org_map, owner_assignee_map,
                group_map, brand_map, record_types_by_id, akarsh_user_id,
                agent_user_by_sf_user_id, agent_user_by_email, aliases,
                protected_agent_emails, prep_report, custom_field_report,
                phone_tasks=phone_tasks_by_case.get(cid, []),
            )
            # Hard invariant: Salesforce Case.Id must be the payload external_id.
            if s(payload.get("external_id")) != cid:
                raise RuntimeError(
                    f"Payload external_id mismatch before import. Expected '{cid}', got '{s(payload.get('external_id'))}'."
                )
            payload["_migration_attachment_case_id"] = cid
            payload["_case_number"] = case_number
            payload["_migration_source"] = source
            payloads.append(payload)
            audit.update({"preparation_status": "ready", "preparation_error": ""})
            audits.append(audit)
            prep_results.append(_build_audit_row(
                cid, case_number, source, "READY", started, _utc_now_text(),
                salesforce_case_id_sent=cid, zendesk_external_id=cid, id_match="YES",
            ))
        except Exception as exc:
            detail = _safe_error_detail(exc)
            et = _classify_error(detail)
            prep_results.append(_build_audit_row(
                cid, case_number, source, "FAILED", started, _utc_now_text(),
                salesforce_case_id_sent=cid, zendesk_external_id="", id_match="NO" if cid else "",
                error_type=et, failure_reason=detail.splitlines()[0][:1000], error_detail=detail,
            ))

    phone_ids = {s(c.get("Id")) for c in cases if is_phone_case(c) and s(c.get("Id"))}
    standard = [p for p in payloads if s(p.get("external_id")) not in phone_ids]
    phone = [p for p in payloads if s(p.get("external_id")) in phone_ids]
    standard_audits = [a for a in audits if s(a.get("salesforce_case_id")) not in phone_ids]
    phone_audits = [a for a in audits if s(a.get("salesforce_case_id")) in phone_ids]

    save_json(payload_dir / "tickets_payload.json", {"tickets": payloads})
    save_json(payload_dir / "tickets_payload_standard.json", {"tickets": standard})
    save_json(payload_dir / "tickets_payload_phone.json", {"tickets": phone})
    write_csv(payload_dir / "ticket_payload_summary.csv", audits)
    write_csv(payload_dir / "standard_ticket_payload_summary.csv", standard_audits)
    write_csv(payload_dir / "phone_ticket_payload_summary.csv", phone_audits)
    write_csv(payload_dir / "comment_preparation_report.csv", prep_report)
    write_csv(payload_dir / "custom_field_report.csv", custom_field_report)

    prep_summary = {
        "Total Salesforce Cases": len(cases),
        "Payloads Ready": len(payloads),
        "Preparation Failed": len(prep_results) - len(payloads),
        "Standard Payloads": len(standard),
        "Phone Payloads": len(phone),
    }
    _write_audit_workbook(payload_dir / "Ticket_Preparation_Log.xlsx", prep_summary, prep_results)
    write_csv(payload_dir / "Ticket_Preparation_Log.csv", prep_results)
    write_json_compact(payload_dir / "payload_build_summary.json", {
        **prep_summary,
        "attachment_strategy": "upload immediately before ticket creation",
        "external_id_rule": "Salesforce Case.Id -> Zendesk Ticket.external_id (exact 1:1)",
    })

    return {
        "cases": len(cases), "payloads": len(payloads), "standard_cases": len(standard),
        "phone_cases": len(phone), "phone_tasks": len(phone_tasks),
        "preparation_failed": len(prep_results) - len(payloads),
        "standard_payload_file": str(payload_dir / "tickets_payload_standard.json"),
        "phone_payload_file": str(payload_dir / "tickets_payload_phone.json"),
    }


# -----------------------------------------------------------------------------
# V6: import with attachment/runtime audit + exact external_id validation
# -----------------------------------------------------------------------------
def _submit_bulk_ticket_import_job(
    zd: ZendeskClient, batch_items: List[Dict[str, Any]], archive_immediately: bool = False,
) -> Dict[str, Any]:
    """Submit up to 100 ticket payloads and wait for Zendesk's asynchronous import job."""
    body = {"tickets": [item["api_payload"] for item in batch_items]}
    response = zd.request(
        "POST",
        "/api/v2/imports/tickets/create_many.json",
        params={"archive_immediately": str(archive_immediately).lower()},
        json_body=body,
    )
    if response.status_code not in {200, 201, 202}:
        raise _BulkTicketRequestError(response.status_code, response.text[:5000])

    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f"Zendesk bulk ticket import returned invalid JSON: {response.text[:2000]}") from exc
    job = data.get("job_status", data) if isinstance(data, dict) else {}
    if not isinstance(job, dict) or not (job.get("id") or job.get("url")):
        raise RuntimeError(f"Zendesk bulk ticket import did not return a job status: {str(data)[:2000]}")

    job_id = s(job.get("id"))
    job_url = s(job.get("url"))
    deadline = time.monotonic() + 1800  # wait up to 30 minutes; do not submit the same batch again on timeout

    while True:
        status = norm(job.get("status"))
        if status in {"completed", "failed"}:
            return job
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Zendesk bulk ticket import job {job_id or job_url} did not finish within 30 minutes. "
                "Do not retry this batch automatically; ticket creation may still be running."
            )

        if not job_url:
            if not job_id:
                raise RuntimeError(f"Zendesk bulk ticket import job has no poll URL or job ID: {str(job)[:1000]}")
            job_url = f"{zd.base_url}/api/v2/job_statuses/{job_id}.json"

        time.sleep(2)
        poll = zd.request("GET", job_url)
        if poll.status_code != 200:
            raise RuntimeError(
                f"Zendesk bulk ticket import job poll failed: HTTP {poll.status_code}\n{poll.text[:3000]}"
            )
        try:
            poll_data = poll.json()
        except Exception as exc:
            raise RuntimeError(f"Zendesk job status returned invalid JSON: {poll.text[:2000]}") from exc
        job = poll_data.get("job_status", poll_data) if isinstance(poll_data, dict) else {}
        if not isinstance(job, dict):
            raise RuntimeError(f"Invalid Zendesk job status response: {str(poll_data)[:2000]}")
        job_id = s(job.get("id")) or job_id
        job_url = s(job.get("url")) or job_url


class _BulkTicketRequestError(RuntimeError):
    def __init__(self, status_code: int, response_text: str):
        self.status_code = int(status_code)
        super().__init__(f"Zendesk bulk ticket import failed: HTTP {status_code}\n{response_text}")


def import_prepared_tickets_modular(
    cfg: Dict[str, Any], script_dir: Path, archive_immediately: bool = False,
    payload_filename: str = "tickets_payload_standard.json", result_prefix: str = "Standard_Migration_Results",
) -> Dict[str, Any]:
    """Import prepared tickets in Zendesk batches of up to 100, with per-ticket audit and ID verification."""
    workspace = modular_workspace(cfg, script_dir)
    payload_path = workspace / "payload" / payload_filename
    if not payload_path.exists():
        raise RuntimeError(f"{payload_filename} not found. Run 03_prepare_tickets.py first.")
    tickets = json.loads(payload_path.read_text(encoding="utf-8")).get("tickets", [])
    if not isinstance(tickets, list):
        raise RuntimeError(f"Invalid ticket payload structure in {payload_filename}.")
    if not tickets:
        return {
            "total": 0, "completed": 0, "existing": 0, "failed": 0,
            "completed_with_warnings": 0, "manual_review_required": 0,
            "result_file": str(workspace / "import" / f"{result_prefix}.xlsx"),
        }

    zd = _modular_zendesk_client(cfg)
    akarsh = zd.find_user_by_email(AGENT_COMMENT_EMAIL)
    if not akarsh:
        raise RuntimeError(f"Zendesk agent not found for {AGENT_COMMENT_EMAIL}.")
    akarsh_user_id = int(akarsh["id"])

    state_path = workspace / "state" / "ticket_state.json"
    state = load_json(state_path) if state_path.exists() else {}
    if not isinstance(state, dict):
        state = {}
    attachment_rows_by_case = _runtime_attachment_rows(workspace)
    results: List[Dict[str, Any]] = []
    attachment_log: List[Dict[str, Any]] = []
    pending: List[Dict[str, Any]] = []
    bulk_enabled = True
    batch_number = 0

    def save_ticket_state(case_id: str, ticket_id: Optional[int], status: str, external_id: str = "", message: str = "") -> None:
        record: Dict[str, Any] = {"status": status}
        if ticket_id:
            record["zendesk_id"] = int(ticket_id)
        if external_id:
            record["zendesk_external_id"] = external_id
        if message:
            record["message"] = message
        state[case_id] = record
        save_json(state_path, state)

    def record_single_import(item: Dict[str, Any], fallback_reason: str = "") -> None:
        """Compatibility fallback when bulk import is unavailable or a single bad payload is isolated."""
        case_id = item["case_id"]
        ticket_id = 0
        attempts = 1
        try:
            ticket = zd.import_ticket(item["api_payload"], archive_immediately=archive_immediately)
            ticket_id = int(ticket["id"])
            verified = _validate_ticket_external_id(zd, ticket_id, case_id, ticket)
            final_status = "COMPLETED_WITH_WARNINGS" if item["attachment_stats"]["attachments_failed"] else "COMPLETED"
            save_ticket_state(case_id, ticket_id, "completed" if final_status == "COMPLETED" else "completed_with_warnings", verified)
            results.append(_build_audit_row(
                case_id, item["case_number"], item["source"], final_status,
                item["started"], _utc_now_text(),
                zendesk_ticket_id=ticket_id, salesforce_case_id_sent=case_id,
                zendesk_external_id=verified, id_match="YES", attempts=attempts,
                **item["attachment_stats"],
            ))
        except Exception as exc:
            detail = _safe_error_detail(exc)
            status = "MANUAL_REVIEW_REQUIRED" if ticket_id else "FAILED"
            if ticket_id:
                save_ticket_state(case_id, ticket_id, "manual_review_required", message=detail)
            results.append(_build_audit_row(
                case_id, item["case_number"], item["source"], status,
                item["started"], _utc_now_text(),
                zendesk_ticket_id=ticket_id, salesforce_case_id_sent=case_id,
                id_match="NO", attempts=attempts,
                http_status=_extract_http_status(detail), error_type=_classify_error(detail),
                failure_reason=detail.splitlines()[0][:1000],
                error_detail=(f"{fallback_reason}\n{detail}" if fallback_reason else detail),
                **item["attachment_stats"],
            ))

    def record_known_creation(item: Dict[str, Any], ticket_id: int, source_result: str = "bulk result") -> None:
        case_id = item["case_id"]
        try:
            # Bulk job results normally return ID/index only; explicitly read the ticket back and verify external_id.
            verified = _validate_ticket_external_id(zd, int(ticket_id), case_id, {"id": int(ticket_id)})
            final_status = "COMPLETED_WITH_WARNINGS" if item["attachment_stats"]["attachments_failed"] else "COMPLETED"
            save_ticket_state(
                case_id, int(ticket_id),
                "completed" if final_status == "COMPLETED" else "completed_with_warnings",
                verified,
            )
            results.append(_build_audit_row(
                case_id, item["case_number"], item["source"], final_status,
                item["started"], _utc_now_text(),
                zendesk_ticket_id=int(ticket_id), salesforce_case_id_sent=case_id,
                zendesk_external_id=verified, id_match="YES", attempts=1,
                **item["attachment_stats"],
            ))
        except Exception as exc:
            detail = _safe_error_detail(exc)
            save_ticket_state(case_id, int(ticket_id), "manual_review_required", message=detail)
            results.append(_build_audit_row(
                case_id, item["case_number"], item["source"], "MANUAL_REVIEW_REQUIRED",
                item["started"], _utc_now_text(),
                zendesk_ticket_id=int(ticket_id), salesforce_case_id_sent=case_id,
                id_match="NO", attempts=1, http_status=_extract_http_status(detail),
                error_type=_classify_error(detail),
                failure_reason=detail.splitlines()[0][:1000],
                error_detail=f"{source_result}: {detail}",
                **item["attachment_stats"],
            ))

    def record_unresolved_bulk_item(item: Dict[str, Any], job: Dict[str, Any], item_result: Optional[Dict[str, Any]]) -> None:
        """Recover by external_id when possible; otherwise park for manual review to prevent duplicate creation."""
        case_id = item["case_id"]
        item_result = item_result or {}
        explicit_error = (
            s(item_result.get("error")) or s(item_result.get("message"))
            or s(item_result.get("error_message"))
        )
        if item_result.get("success") is False and explicit_error:
            detail = explicit_error
            results.append(_build_audit_row(
                case_id, item["case_number"], item["source"], "FAILED",
                item["started"], _utc_now_text(),
                salesforce_case_id_sent=case_id, id_match="NO", attempts=1,
                http_status=_extract_http_status(detail), error_type=_classify_error(detail),
                failure_reason=detail.splitlines()[0][:1000], error_detail=detail,
                **item["attachment_stats"],
            ))
            return

        found = zd.find_ticket_by_external_id(case_id)
        if found and found.get("id"):
            record_known_creation(item, int(found["id"]), "external_id recovery after bulk job")
            return

        job_id = s(job.get("id"))
        detail = (
            f"Bulk job {job_id or '(unknown ID)'} ended with status '{s(job.get('status'))}' "
            f"but returned no ticket ID for batch index {item['batch_index']}. "
            f"Job message: {s(job.get('message')) or 'No job message'}"
        )
        save_ticket_state(case_id, None, "manual_review_required", message=detail)
        results.append(_build_audit_row(
            case_id, item["case_number"], item["source"], "MANUAL_REVIEW_REQUIRED",
            item["started"], _utc_now_text(),
            salesforce_case_id_sent=case_id, id_match="NO", attempts=1,
            error_type="BULK_RESULT_MISSING", failure_reason=detail[:1000],
            error_detail=detail, **item["attachment_stats"],
        ))

    def process_batch(batch_items: List[Dict[str, Any]]) -> None:
        nonlocal bulk_enabled, batch_number
        if not batch_items:
            return
        if not bulk_enabled:
            for item in batch_items:
                record_single_import(item, "Bulk endpoint unavailable; used one-ticket import fallback.")
            return

        batch_number += 1
        first_case = batch_items[0]["case_number"] or batch_items[0]["case_id"]
        last_case = batch_items[-1]["case_number"] or batch_items[-1]["case_id"]
        print(
            f"[Bulk Import] Submitting batch {batch_number}: {len(batch_items)} tickets "
            f"({first_case} -> {last_case})",
            flush=True,
        )
        try:
            job = _submit_bulk_ticket_import_job(zd, batch_items, archive_immediately)
        except _BulkTicketRequestError as exc:
            # A 403 means create_many is not available to this Zendesk role. Fall back automatically.
            if exc.status_code == 403:
                bulk_enabled = False
                print(
                    "WARNING: Zendesk bulk ticket import returned HTTP 403. "
                    "Falling back to the existing one-ticket import API for this and remaining batches.",
                    flush=True,
                )
                for item in batch_items:
                    record_single_import(item, "Bulk endpoint returned HTTP 403.")
                return
            # If a batch is rejected before job creation, split validation/body-size failures to isolate a bad record.
            if exc.status_code in {400, 413, 422}:
                if len(batch_items) > 1:
                    midpoint = len(batch_items) // 2
                    print(
                        f"WARNING: Bulk batch rejected with HTTP {exc.status_code}; splitting into "
                        f"{midpoint} and {len(batch_items) - midpoint} ticket sub-batches.",
                        flush=True,
                    )
                    process_batch(batch_items[:midpoint])
                    process_batch(batch_items[midpoint:])
                else:
                    record_single_import(batch_items[0], str(exc))
                return
            # The server response did not confirm that the batch was safely rejected; stop rather than risk duplicate creation.
            for item in batch_items:
                detail = str(exc)
                save_ticket_state(item["case_id"], None, "manual_review_required", message=detail)
                results.append(_build_audit_row(
                    item["case_id"], item["case_number"], item["source"], "MANUAL_REVIEW_REQUIRED",
                    item["started"], _utc_now_text(),
                    salesforce_case_id_sent=item["case_id"], id_match="NO", attempts=1,
                    http_status=exc.status_code, error_type=_classify_error(detail),
                    failure_reason=detail.splitlines()[0][:1000], error_detail=detail,
                    **item["attachment_stats"],
                ))
            raise RuntimeError(
                f"Stopping ticket import after bulk submission failed with HTTP {exc.status_code}. "
                "The batch was marked for manual review to avoid accidental duplicates."
            ) from exc
        except Exception as exc:
            # Network/poll errors can occur after Zendesk has accepted a job. Never resubmit these payloads automatically.
            detail = _safe_error_detail(exc)
            for item in batch_items:
                save_ticket_state(item["case_id"], None, "manual_review_required", message=detail)
                results.append(_build_audit_row(
                    item["case_id"], item["case_number"], item["source"], "MANUAL_REVIEW_REQUIRED",
                    item["started"], _utc_now_text(),
                    salesforce_case_id_sent=item["case_id"], id_match="NO", attempts=1,
                    http_status=_extract_http_status(detail), error_type=_classify_error(detail),
                    failure_reason=detail.splitlines()[0][:1000], error_detail=detail,
                    **item["attachment_stats"],
                ))
            raise RuntimeError(
                "Stopping ticket import because the bulk job outcome could not be confirmed. "
                "The batch was marked for manual review; do not rerun it until checking Zendesk by external_id."
            ) from exc

        job_status = norm(job.get("status"))
        job_results = job.get("results", [])
        by_index: Dict[int, Dict[str, Any]] = {}
        if isinstance(job_results, list):
            for result in job_results:
                if not isinstance(result, dict):
                    continue
                try:
                    idx = int(result.get("index"))
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(batch_items):
                    by_index[idx] = result
            # Compatibility only for responses that return ordered results without index properties.
            if not by_index and len(job_results) == len(batch_items):
                by_index = {i: r for i, r in enumerate(job_results) if isinstance(r, dict)}

        if job_status == "failed":
            print(
                f"WARNING: Zendesk bulk job {s(job.get('id'))} ended as failed: "
                f"{s(job.get('message')) or 'No job message'}. Reconciling every ticket result.",
                flush=True,
            )
        else:
            print(
                f"[Bulk Import] Job {s(job.get('id')) or '(unknown ID)'} completed; "
                f"results returned: {len(by_index)}/{len(batch_items)}",
                flush=True,
            )

        for idx, item in enumerate(batch_items):
            item["batch_index"] = idx
            bulk_result = by_index.get(idx)
            ticket_id = bulk_result.get("id") if isinstance(bulk_result, dict) else None
            if ticket_id:
                record_known_creation(item, int(ticket_id))
            else:
                record_unresolved_bulk_item(item, job, bulk_result)

    def flush_pending() -> None:
        if pending:
            current = list(pending)
            pending.clear()
            process_batch(current)

    print(
        f"STEP import - {'BULK' if bulk_enabled else 'single-ticket'} mode; "
        f"batch size {BATCH_SIZE} (Zendesk maximum is 100). Payload tickets: {len(tickets)}",
        flush=True,
    )

    for ticket_index, payload in enumerate(tickets, start=1):
        case_id = s(payload.get("external_id"))
        case_number = s(payload.get("_case_number"))
        source = s(payload.get("_migration_source")) or ("phone" if "phone" in result_prefix.lower() else "standard")
        started = _utc_now_text()

        if not case_id:
            results.append(_build_audit_row(
                "", case_number, source, "FAILED", started, _utc_now_text(),
                attempts=0, error_type="MISSING_EXTERNAL_ID",
                failure_reason="Payload has no Salesforce Case.Id / external_id.",
                error_detail="Missing external_id in prepared payload.",
            ))
            continue

        prior = state.get(case_id, {}) if isinstance(state, dict) else {}
        if prior.get("status") == "manual_review_required":
            results.append(_build_audit_row(
                case_id, case_number, source, "MANUAL_REVIEW_REQUIRED", started, _utc_now_text(),
                zendesk_ticket_id=prior.get("zendesk_id", ""),
                salesforce_case_id_sent=case_id, zendesk_external_id=prior.get("zendesk_external_id", ""),
                id_match="NO", error_type="ID_MISMATCH",
                failure_reason="Previous run recorded this ticket for manual review; automatic retry is disabled.",
                error_detail=prior.get("message", "Manual review required."),
            ))
            continue

        existing_id = int(prior.get("zendesk_id") or 0) if prior.get("zendesk_id") else None
        existing = None
        existing_status = False
        if not existing_id:
            existing = zd.find_ticket_by_external_id(case_id)
            if not existing and case_number:
                # Also check Zendesk by case_number to detect duplicates migrated earlier
                q_num = zd.request("GET", "/api/v2/search.json", params={"query": f'type:ticket "{case_number}"', "per_page": 5})
                if q_num.status_code == 200:
                    res_items = q_num.json().get("results", [])
                    if res_items:
                        existing = res_items[0]
            if existing and existing.get("id"):
                existing_id = int(existing["id"])
                existing_status = True

        if existing_id:
            try:
                verified = _validate_ticket_external_id(zd, existing_id, case_id, existing if existing_status else None)
                att = _attachment_stats_for_case(attachment_rows_by_case.get(case_id, []), case_id)
                results.append(_build_audit_row(
                    case_id, case_number, source, "EXISTING", started, _utc_now_text(),
                    zendesk_ticket_id=existing_id, salesforce_case_id_sent=case_id,
                    zendesk_external_id=verified, id_match="YES", attempts=0, **att,
                ))
                save_ticket_state(case_id, existing_id, "existing", verified)
            except Exception as exc:
                detail = _safe_error_detail(exc)
                results.append(_build_audit_row(
                    case_id, case_number, source, "MANUAL_REVIEW_REQUIRED", started, _utc_now_text(),
                    zendesk_ticket_id=existing_id, salesforce_case_id_sent=case_id,
                    id_match="NO", error_type=_classify_error(detail),
                    failure_reason=detail.splitlines()[0][:1000], error_detail=detail,
                ))
                save_ticket_state(case_id, existing_id, "manual_review_required", message=detail)
            continue

        try:
            rows = attachment_rows_by_case.get(case_id, [])
            _runtime_attach_files_to_payload(payload, rows, zd, akarsh_user_id, workspace, attachment_log)
            att = _attachment_stats_for_case(attachment_log, case_id)
            clean_payload = strip_private_metadata(payload)
            if s(clean_payload.get("external_id")) != case_id:
                raise RuntimeError(
                    f"Payload external_id mismatch before Zendesk import. Expected '{case_id}', "
                    f"got '{s(clean_payload.get('external_id'))}'."
                )
            pending.append({
                "case_id": case_id,
                "case_number": case_number,
                "source": source,
                "started": started,
                "api_payload": clean_payload,
                "attachment_stats": att,
            })
        except Exception as exc:
            detail = _safe_error_detail(exc)
            results.append(_build_audit_row(
                case_id, case_number, source, "FAILED", started, _utc_now_text(),
                salesforce_case_id_sent=case_id, id_match="NO",
                error_type=_classify_error(detail),
                failure_reason=detail.splitlines()[0][:1000], error_detail=detail,
                **_attachment_stats_for_case(attachment_log, case_id),
            ))
            continue

        if len(pending) >= BATCH_SIZE:
            flush_pending()

        if ticket_index % 100 == 0 or ticket_index == len(tickets):
            print(
                f"[Import Preparation] {ticket_index:,}/{len(tickets):,} payloads checked; "
                f"{len(results):,} outcomes logged; {len(pending)} ticket(s) waiting for batch.",
                flush=True,
            )

    flush_pending()

    out = workspace / "import"
    out.mkdir(parents=True, exist_ok=True)
    result_xlsx = out / f"{result_prefix}.xlsx"
    result_csv = out / f"{result_prefix}.csv"
    result_json = out / f"{result_prefix}_Summary.json"

    summary = {
        "Total": len(results),
        "Completed": sum(1 for r in results if r.get("Status") == "COMPLETED"),
        "Completed with warnings": sum(1 for r in results if r.get("Status") == "COMPLETED_WITH_WARNINGS"),
        "Existing": sum(1 for r in results if r.get("Status") == "EXISTING"),
        "Failed": sum(1 for r in results if r.get("Status") == "FAILED"),
        "Manual review required": sum(1 for r in results if r.get("Status") == "MANUAL_REVIEW_REQUIRED"),
        "ID matches": sum(1 for r in results if s(r.get("ID Match")) == "YES"),
        "ID mismatches": sum(1 for r in results if r.get("Error Type") == "ID_MISMATCH"),
        "Attachments uploaded": sum(int(r.get("Attachments Uploaded") or 0) for r in results),
        "Attachments failed": sum(int(r.get("Attachments Failed") or 0) for r in results),
    }
    _write_audit_workbook(result_xlsx, summary, results)
    write_csv(result_csv, results)
    write_json_compact(result_json, summary)
    if attachment_log:
        write_csv(out / "Attachment_Runtime_Log.csv", attachment_log)
        modular_excel_write(out / "Attachment_Runtime_Log.xlsx", attachment_log, sheet_name="Attachment Runtime")

    _write_overall_reconciliation(workspace)
    return {
        "total": len(results),
        "completed": summary["Completed"],
        "existing": summary["Existing"],
        "failed": summary["Failed"],
        "completed_with_warnings": summary["Completed with warnings"],
        "manual_review_required": summary["Manual review required"],
        "result_file": str(result_xlsx),
    }
