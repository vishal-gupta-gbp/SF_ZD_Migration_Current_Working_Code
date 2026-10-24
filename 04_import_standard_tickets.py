#!/usr/bin/env python3
"""Step 4 - Import standard tickets using Bulk Import API and database tracking."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from migration.config import load_config
from migration.db.connection import get_db, init_engine
from migration.db.schema import init_db
from migration.pipeline.step4_import import run_step4_import


def parse_ticket_ids(val: Optional[str]) -> Optional[list[str]]:
    if not val:
        return None
    ids = [x.strip().strip("'\"") for x in val.split(",")]
    cleaned = [x for x in ids if x]
    return cleaned if cleaned else None


def main():
    p = argparse.ArgumentParser(description="Step 4 - Import standard tickets into Zendesk")
    p.add_argument("--config", default=None, help="Optional path to config JSON file")
    p.add_argument("--archive-immediately", action="store_true")
    p.add_argument("--start-date", default=None, help="YYYY-MM-DD")
    p.add_argument("--end-date", default=None, help="YYYY-MM-DD")
    p.add_argument("--ticket-ids", "--case-ids", "--sf-ticket-ids", default=None, help="Comma-separated Salesforce Case IDs or Case Numbers")
    p.add_argument("--db-url", default=None, help="Override database URL")
    a = p.parse_args()

    ticket_ids = parse_ticket_ids(a.ticket_ids)

    settings = load_config(a.config)
    if a.db_url:
        settings.database_url = a.db_url

    engine = init_engine(settings.database_url)
    init_db(engine)

    with get_db() as session:
        summary = run_step4_import(
            settings=settings,
            session=session,
            ticket_type="standard",
            archive_immediately=a.archive_immediately,
            start_date=a.start_date,
            end_date=a.end_date,
            ticket_ids=ticket_ids,
        )
        print(f"Step 4 Complete: {summary['completed']} imported, {summary['existing']} existing, {summary['failed']} failed.")


if __name__ == "__main__":
    main()
