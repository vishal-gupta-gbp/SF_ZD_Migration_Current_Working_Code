#!/usr/bin/env python3
"""Apply conservative export speed/progress improvements to the current SF->Zendesk code.

- Shows the existing Salesforce export messages instead of suppressing them.
- Increases ID-based related-object query batches from 100 to 200.
- Prints progress while the REST query paginates and while related-ID batches run.
- Downloads unique S3 URLs with bounded concurrency, while reusing prior successful
  downloads whose file still exists.
- Preserves CSV/XLSX output names and row-level success/failure reporting.
- Adds timestamped backups and validates expected source patterns before writing.

Run only when no migration/export Python process is running.
"""
from __future__ import annotations

import argparse
import ast
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path


S3_FUNCTION = r'''def download_s3_from_export_modular(cfg: Dict[str, Any], script_dir: Path) -> Dict[str, Any]:
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
'''


def fail(msg: str) -> None:
    raise RuntimeError(msg)


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        fail(f"Expected exactly one match for {label}; found {count}. No source files have been written.")
    return text.replace(old, new, 1)


def patch_core(original: str) -> str:
    if "EXPORT_ID_BATCH_SIZE = 200" in original and "6 parallel workers" in original:
        return original

    source = original
    # Imports.
    if "from concurrent.futures import ThreadPoolExecutor, as_completed" not in source:
        source = replace_once(source, "import re\n", "import re\nfrom concurrent.futures import ThreadPoolExecutor, as_completed\n", "concurrent-futures import")
    if "from boto3.s3.transfer import TransferConfig" not in source:
        source = replace_once(source, "import boto3\n", "import boto3\nfrom boto3.s3.transfer import TransferConfig\n", "TransferConfig import")

    # Conservative query batch size.
    if "EXPORT_ID_BATCH_SIZE = 200" not in source:
        source = replace_once(
            source,
            "RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}\n",
            "RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}\nEXPORT_ID_BATCH_SIZE = 200  # IDs per related-object SOQL query\n",
            "export batch-size constant",
        )

    # Add a labelled progress generator without changing existing chunk behaviour elsewhere.
    if "def progress_chunks(" not in source:
        source = replace_once(
            source,
            "def chunks(values: List[str], size: int) -> Iterable[List[str]]:\n    for i in range(0, len(values), size):\n        yield values[i:i + size]\n",
            "def chunks(values: List[str], size: int) -> Iterable[List[str]]:\n    for i in range(0, len(values), size):\n        yield values[i:i + size]\n\n\ndef progress_chunks(values: List[str], size: int, label: str) -> Iterable[List[str]]:\n    batches = list(chunks(values, size))\n    for index, batch in enumerate(batches, start=1):\n        print(f\"[Export] {label}: batch {index:,}/{len(batches):,}\", flush=True)\n        yield batch\n",
            "progress_chunks helper",
        )

    # Show page-level progress for the main Cases query only.
    query_start = source.find("    def query(self, soql: str) -> List[Dict[str, Any]]:")
    require = lambda cond, msg: None if cond else fail(msg)
    require(query_start >= 0, "Could not find SalesforceClient.query(). No source files have been written.")
    query_end = source.find("\n\ndef quote_soql", query_start)
    require(query_end > query_start, "Could not find the end of SalesforceClient.query(). No source files have been written.")
    query_block = source[query_start:query_end]
    query_block = replace_once(query_block, "def query(self, soql: str) -> List[Dict[str, Any]]:", "def query(self, soql: str, progress_label: Optional[str] = None) -> List[Dict[str, Any]]:", "query signature")
    query_block = replace_once(query_block, "        records = list(data.get(\"records\", []))\n", "        records = list(data.get(\"records\", []))\n        if progress_label:\n            total = data.get(\"totalSize\", \"?\")\n            print(f\"[Export] {progress_label}: {len(records):,}/{total} records received\", flush=True)\n", "initial query progress")
    query_block = replace_once(query_block, "            records.extend(data.get(\"records\", []))\n", "            records.extend(data.get(\"records\", []))\n            if progress_label:\n                total = data.get(\"totalSize\", \"?\")\n                print(f\"[Export] {progress_label}: {len(records):,}/{total} records received\", flush=True)\n", "query pagination progress")
    source = source[:query_start] + query_block + source[query_end:]

    # Target the exporter block so unrelated batch operations remain untouched.
    phone_start = source.find("def export_phone_tasks(")
    export_start = source.find("def export_salesforce_data(", phone_start)
    payload_start = source.find("# Migration payload preparation", export_start)
    require(min(phone_start, export_start, payload_start) >= 0, "Could not identify export function boundaries. No source files have been written.")
    phone_block = source[phone_start:export_start]
    export_block = source[export_start:payload_start]

    phone_block = replace_once(phone_block, "for ids in chunks(phone_case_ids, 100):", "for ids in progress_chunks(phone_case_ids, EXPORT_ID_BATCH_SIZE, \"Phone Tasks\"):", "phone Task batching")

    substitutions = [
        ("for ids in chunks(case_ids, 100):", "for ids in progress_chunks(case_ids, EXPORT_ID_BATCH_SIZE, \"EmailMessages\"):", "EmailMessage batching", True),
        ("for ids in chunks(case_ids, 100):", "for ids in progress_chunks(case_ids, EXPORT_ID_BATCH_SIZE, \"S3 mappings\"):", "S3 mapping batching", True),
        ("for ids in chunks(contact_ids, 100):", "for ids in progress_chunks(contact_ids, EXPORT_ID_BATCH_SIZE, \"Contacts\"):", "Contact batching", False),
        ("for ids in chunks(account_ids, 100):", "for ids in progress_chunks(account_ids, EXPORT_ID_BATCH_SIZE, \"Accounts\"):", "Account batching", False),
        ("for ids in chunks(user_ids, 100):", "for ids in progress_chunks(user_ids, EXPORT_ID_BATCH_SIZE, \"Salesforce Users\"):", "User batching", False),
        ("for ids in chunks(record_type_ids, 100):", "for ids in progress_chunks(record_type_ids, EXPORT_ID_BATCH_SIZE, \"Record Types\"):", "Record Type batching", False),
    ]
    for old, new, label, replace_first in substitutions:
        if replace_first:
            count = export_block.count(old)
            if count < 1:
                fail(f"Could not find expected loop for {label}. No source files have been written.")
            export_block = export_block.replace(old, new, 1)
        else:
            export_block = replace_once(export_block, old, new, label)

    export_block = replace_once(export_block, "cases = sf.query(case_soql)", "cases = sf.query(case_soql, progress_label=\"Cases\")", "Cases query progress")
    # After each object query, report record totals. These summaries complement batch progress.
    summaries = [
        ("\n    print(\"Fetching related Salesforce S3 mappings...\")", "\n    print(f\"[Export] EmailMessages complete: {len(email_messages):,}\", flush=True)\n    print(\"Fetching related Salesforce S3 mappings...\")", "EmailMessage result summary"),
        ("\n    print(\"Fetching related Contacts...\")", "\n    print(f\"[Export] S3 mapping rows complete: {len(s3_records):,}\", flush=True)\n    print(\"Fetching related Contacts...\")", "S3 row summary"),
        ("\n    print(\"Fetching related Accounts...\")", "\n    print(f\"[Export] Contacts complete: {len(contacts):,}\", flush=True)\n    print(\"Fetching related Accounts...\")", "Contact result summary"),
        ("\n    print(\"Fetching Salesforce users relevant to the sample...\")", "\n    print(f\"[Export] Accounts complete: {len(accounts):,}\", flush=True)\n    print(\"Fetching Salesforce users relevant to the sample...\")", "Account result summary"),
        ("\n    phone_cases = [c for c in cases if is_phone_case(c)]", "\n    print(f\"[Export] Salesforce Users complete: {len(users):,}\", flush=True)\n    phone_cases = [c for c in cases if is_phone_case(c)]", "User result summary"),
        ("\n    print(\"Fetching related Salesforce Record Types...\")", "\n    print(f\"[Export] Phone Tasks complete: {len(phone_tasks):,}\", flush=True)\n    print(\"Fetching related Salesforce Record Types...\")", "Phone Task result summary"),
        ("\n    write_csv(output_dir / \"Cases.csv\", cases)", "\n    print(f\"[Export] Record Types complete: {len(record_types):,}\", flush=True)\n    write_csv(output_dir / \"Cases.csv\", cases)", "Record Type result summary"),
    ]
    for old, new, label in summaries:
        export_block = replace_once(export_block, old, new, label)

    # O(n^2) lookup on phone recordings -> O(n) dictionary lookup.
    old_lookup = (
        '    for row in recording_rows:\n'
        '        case = next((c for c in phone_cases if s(c.get("Id")) == row["Salesforce_Case_ID"]), {})\n'
        '        row["CaseNumber"] = s(case.get("CaseNumber"))'
    )
    new_lookup = (
        '    case_number_by_id = {s(c.get("Id")): s(c.get("CaseNumber")) for c in phone_cases if s(c.get("Id"))}\n'
        '    for row in recording_rows:\n'
        '        row["CaseNumber"] = case_number_by_id.get(row["Salesforce_Case_ID"], "")'
    )
    if old_lookup in phone_block:
        phone_block = phone_block.replace(old_lookup, new_lookup, 1)
    elif "case_number_by_id = {s(c.get(\"Id\"))" not in phone_block:
        fail("Could not find the phone recording CaseNumber loop. No source files have been written.")

    source = source[:phone_start] + phone_block + export_block + source[payload_start:]

    # Make transfer config optional in S3Downloader.download so current calls remain compatible.
    source = replace_once(
        source,
        "def download(self, url: str, local_path: Path) -> Dict[str, Any]:",
        "def download(self, url: str, local_path: Path, transfer_config: Optional[TransferConfig] = None) -> Dict[str, Any]:",
        "S3Downloader.download signature",
    )
    source = replace_once(
        source,
        "                Filename=str(local_path),\n            )",
        "                Filename=str(local_path),\n                Config=transfer_config,\n            )",
        "S3 transfer configuration",
    )

    # Replace the full S3 downloader function between stable function markers.
    start = source.find("def download_s3_from_export_modular(")
    end = source.find("\n\ndef _modular_zendesk_client(", start)
    require(start >= 0 and end > start, "Could not locate S3 download function boundaries. No source files have been written.")
    source = source[:start] + S3_FUNCTION + source[end:]

    # Check syntax before writing into the user's folder.
    ast.parse(source, filename="migration_core.py")
    return source


def patch_export_script(original: str) -> str:
    if "quiet = sys.stdout" in original and "Salesforce export duration:" in original:
        ast.parse(original, filename="01_export.py")
        return original
    if "quiet = io.StringIO()" not in original:
        fail("Could not find the expected stdout-suppression line in 01_export.py. No changes have been written.")
    source = original.replace("quiet = io.StringIO()", "quiet = sys.stdout", 1)
    # Print elapsed time separately for Salesforce query/export and S3 downloads.
    source = replace_once(source, "    quiet = sys.stdout", "    export_started = time.perf_counter()\n    quiet = sys.stdout", "export timer")
    old_summary = """    print(f"Completed: {summary['cases']} Cases exported ({summary['phone_cases']} phone).")"""
    new_summary = """    print(f"Salesforce export duration: {time.perf_counter() - export_started:.1f} seconds.")
    print(f"Completed: {summary['cases']} Cases exported ({summary['phone_cases']} phone).")"""
    source = replace_once(source, old_summary, new_summary, "export duration output")
    source = replace_once(source, "    s3 = download_s3_from_export_modular(cfg, root)", "    s3_started = time.perf_counter()\n    s3 = download_s3_from_export_modular(cfg, root)\n    print(f\"S3 download duration: {time.perf_counter() - s3_started:.1f} seconds.\", flush=True)", "S3 duration timer")
    if not re.search(r"(?m)^import sys$", source):
        if re.search(r"(?m)^import io$", source):
            source = re.sub(r"(?m)^import io$", "import io\nimport sys", source, count=1)
        else:
            source = "import sys\n" + source
    if not re.search(r"(?m)^import time$", source):
        if re.search(r"(?m)^import sys$", source):
            source = re.sub(r"(?m)^import sys$", "import sys\nimport time", source, count=1)
        else:
            source = "import time\n" + source
    ast.parse(source, filename="01_export.py")
    return source


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply safe export performance optimizations")
    parser.add_argument("project_folder", nargs="?", default=".", help="Folder containing migration_core.py and 01_export.py")
    args = parser.parse_args()
    root = Path(args.project_folder).expanduser().resolve()
    core_path = root / "migration_core.py"
    export_path = root / "01_export.py"
    if not core_path.is_file() or not export_path.is_file():
        fail(f"Expected migration_core.py and 01_export.py inside: {root}")

    core_original = core_path.read_text(encoding="utf-8")
    export_original = export_path.read_text(encoding="utf-8")
    core_new = patch_core(core_original)
    export_new = patch_export_script(export_original)
    if core_new == core_original and export_new == export_original:
        print("No changes needed: export optimization is already applied.")
        return 0

    # Back up and write only after both files have passed exact-pattern checks and syntax parsing.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(core_path, core_path.with_name(f"migration_core.py.backup_{stamp}"))
    shutil.copy2(export_path, export_path.with_name(f"01_export.py.backup_{stamp}"))
    core_path.write_text(core_new, encoding="utf-8", newline="")
    export_path.write_text(export_new, encoding="utf-8", newline="")
    print("Export optimization applied.")
    print("- Related-object query chunks: 100 -> 200 IDs")
    print("- Salesforce query/page and related-batch progress visible in console")
    print("- S3 downloads: up to 6 URLs in parallel; 2 internal transfer threads each")
    print("- Reuses previous successful S3 downloads when URL and local file still match")
    print("- Phone recording case-number lookup optimized")
    print("- CSV/XLSX filenames and downstream export schema retained")
    print("Backup files were created beside the original scripts.")
    print("Run a small-date export first and compare Export_Summary.csv and S3_Attachment_Export.csv before using the full range.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"PATCH FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
