#!/usr/bin/env python3
"""Safely patch migration_core.py to use Zendesk ticket bulk import (up to 100/request)."""
from __future__ import annotations

import ast
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "migration_core.py"
FUNCTION_FILE = ROOT / "bulk_import_function.txt"
OLD_BATCH = "BATCH_SIZE = 1  # synchronous Ticket Import calls; bulk create_many is documented separately for production planning"
NEW_BATCH = "BATCH_SIZE = 100  # maximum ticket objects per Zendesk bulk-import request"
MARKER = "def import_prepared_tickets_modular("


def main() -> int:
    if not TARGET.exists():
        print(f"ERROR: {TARGET} was not found.")
        print("Extract this patch into the same folder that contains migration_core.py.")
        return 2
    if not FUNCTION_FILE.exists():
        print(f"ERROR: {FUNCTION_FILE.name} was not found beside this script.")
        return 2

    source = TARGET.read_text(encoding="utf-8")
    replacement = FUNCTION_FILE.read_text(encoding="utf-8").rstrip() + "\n"

    locations = [m.start() for m in re.finditer(r"(?m)^" + re.escape(MARKER), source)]
    if len(locations) != 1:
        print(f"ERROR: Expected exactly one {MARKER!r} definition; found {len(locations)}.")
        print("No changes were made.")
        return 3
    if source.count(OLD_BATCH) != 1:
        print("ERROR: The expected BATCH_SIZE = 1 line was not found exactly once.")
        print("No changes were made; your migration_core.py may be a different version.")
        return 3

    start = locations[0]
    # This migration_core version has import_prepared_tickets_modular as its final definition.
    if source[start:].strip().count("\ndef ") > 0 or source[start:].strip().count("\nclass ") > 0:
        print("ERROR: The import function is not the final definition in migration_core.py.")
        print("No changes were made to avoid replacing unrelated code.")
        return 3

    updated = source[:start] + replacement
    updated = updated.replace(OLD_BATCH, NEW_BATCH, 1)

    try:
        ast.parse(updated)
    except SyntaxError as exc:
        print(f"ERROR: Updated code failed syntax validation at line {exc.lineno}: {exc.msg}")
        print("No changes were made.")
        return 4

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ROOT / f"migration_core.py.backup_bulk_import_{stamp}"
    shutil.copy2(TARGET, backup)
    TARGET.write_text(updated, encoding="utf-8", newline="\n")

    print("Bulk ticket import patch applied.")
    print(f"Backup created: {backup.name}")
    print("Ticket batch size: 100 maximum per request.")
    print("The existing 00_run_migration.py runner and step scripts remain unchanged.")
    print("Syntax validation: passed.")
    print("Note: Zendesk create_many requires an Admin role; on HTTP 403, the script falls back to one-ticket imports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
