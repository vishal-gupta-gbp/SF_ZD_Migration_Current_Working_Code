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


def main():
    p = argparse.ArgumentParser(description="Step 1 - Export Salesforce data and download S3 attachments into database")
    p.add_argument("--config", default=None, help="Optional path to config JSON file")
    p.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    p.add_argument("--end-date", required=True, help="YYYY-MM-DD, inclusive")
    p.add_argument("--max-tickets", type=int, default=None, help="0 = all selected Cases")
    p.add_argument("--db-url", default=None, help="Override database URL")
    p.add_argument(
        "--source-filter",
        default=None,
        choices=["all", "portal_api", "non_portal_api"],
        help="Filter Salesforce cases by BP_Source__c ('portal_api' for Portal (Ticket API), 'non_portal_api', or 'all')",
    )
    a = p.parse_args()

    settings = load_config(a.config)
    if a.db_url:
        settings.database_url = a.db_url
    if a.source_filter:
        settings.source_filter = a.source_filter.lower().strip()

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
        )
        print(f"Step 1 Complete: {summary['cases']} Cases exported.")


if __name__ == "__main__":
    main()
