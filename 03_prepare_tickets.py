#!/usr/bin/env python3
"""Step 3 - Prepare ticket payloads and store in database migration_tickets table."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from migration.config import load_config
from migration.db.connection import get_db, init_engine
from migration.db.schema import init_db
from migration.db.seeder import seed_mappings_if_needed
from migration.pipeline.step3_prepare import run_step3_prepare


def parse_ticket_ids(val: Optional[str]) -> Optional[list[str]]:
    if not val:
        return None
    ids = [x.strip().strip("'\"") for x in val.split(",")]
    cleaned = [x for x in ids if x]
    return cleaned if cleaned else None


def main():
    p = argparse.ArgumentParser(description="Step 3 - Prepare ticket payloads in database")
    p.add_argument("--config", default=None, help="Optional path to config JSON file")
    p.add_argument("--start-date", required=False, default=None)
    p.add_argument("--end-date", required=False, default=None)
    p.add_argument("--ticket-ids", "--case-ids", "--sf-ticket-ids", default=None, help="Comma-separated Salesforce Case IDs or Case Numbers")
    p.add_argument("--max-tickets", type=int, default=None)
    p.add_argument("--db-url", default=None, help="Override database URL")
    a = p.parse_args()

    ticket_ids = parse_ticket_ids(a.ticket_ids)

    settings = load_config(a.config)
    if a.db_url:
        settings.database_url = a.db_url

    engine = init_engine(settings.database_url)
    init_db(engine)

    with get_db() as session:
        seed_mappings_if_needed(session, settings.mapping_file)
        summary = run_step3_prepare(
            settings=settings,
            session=session,
            start_date=a.start_date,
            end_date=a.end_date,
            max_tickets=a.max_tickets,
            ticket_ids=ticket_ids,
        )
        print(f"Step 3 Complete: {summary['payloads']} payloads ready ({summary['standard_cases']} standard, {summary['phone_cases']} phone).")


if __name__ == "__main__":
    main()
