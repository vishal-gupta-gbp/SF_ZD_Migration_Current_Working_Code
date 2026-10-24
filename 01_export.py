#!/usr/bin/env python3
"""Step 1 - Export Salesforce data and download S3 attachments into database."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from migration.config import load_config
from migration.db.connection import get_db, init_engine
from migration.db.schema import init_db
from migration.db.seeder import seed_mappings_if_needed
from migration.pipeline.step1_export import run_step1_export


def parse_ticket_ids(val: Optional[str]) -> Optional[list[str]]:
    if not val:
        return None
    ids = [x.strip().strip("'\"") for x in val.split(",")]
    cleaned = [x for x in ids if x]
    return cleaned if cleaned else None


def main():
    p = argparse.ArgumentParser(description="Step 1 - Export Salesforce data and download S3 attachments into database")
    p.add_argument("--config", default=None, help="Optional path to config JSON file")
    p.add_argument("--start-date", default=None, help="YYYY-MM-DD")
    p.add_argument("--end-date", default=None, help="YYYY-MM-DD, inclusive")
    p.add_argument("--ticket-ids", "--case-ids", "--sf-ticket-ids", default=None, help="Comma-separated Salesforce Case IDs or Case Numbers")
    p.add_argument("--max-tickets", type=int, default=None, help="0 = all selected Cases")
    p.add_argument("--db-url", default=None, help="Override database URL")
    p.add_argument(
        "--source-filter",
        default=None,
        choices=["all", "portal_api", "non_portal_api"],
        help="Filter Salesforce cases by BP_Source__c ('portal_api' for Portal (Ticket API), 'non_portal_api', or 'all')",
    )
    p.add_argument("--skip-attachments", action="store_true", help="Skip S3 file downloads")
    a = p.parse_args()

    ticket_ids = parse_ticket_ids(a.ticket_ids)
    if not a.start_date and not ticket_ids:
        print("Error: Either --start-date or --ticket-ids must be provided.", file=sys.stderr)
        sys.exit(1)

    settings = load_config(a.config)
    if a.db_url:
        settings.database_url = a.db_url
    if a.source_filter:
        settings.source_filter = a.source_filter.lower().strip()
    if a.skip_attachments:
        settings.skip_attachments = True

    engine = init_engine(settings.database_url)
    init_db(engine)

    with get_db() as session:
        seed_mappings_if_needed(session, settings.mapping_file)
        summary = run_step1_export(
            settings=settings,
            session=session,
            start_date=a.start_date,
            end_date=a.end_date,
            max_tickets=a.max_tickets,
            ticket_ids=ticket_ids,
        )
        print(f"Step 1 Complete: {summary['cases']} Cases exported.")


if __name__ == "__main__":
    main()
