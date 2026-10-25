#!/usr/bin/env python3
"""Step 2 - Bulk-optimized Organizations and End Users migration directly into database."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from migration.cli import parse_ticket_ids
from migration.config import load_config
from migration.db.connection import get_db, init_engine
from migration.db.schema import init_db
from migration.pipeline.step2_entities import run_step2_entities


def main():
    p = argparse.ArgumentParser(description="Step 2 - Bulk-optimized Organizations and End Users migration")
    p.add_argument("--config", default=None, help="Optional path to config JSON file")
    p.add_argument("--db-url", default=None, help="Override database URL")
    p.add_argument("--start-date", default=None, help="Filter by Salesforce Case created date >= YYYY-MM-DD")
    p.add_argument("--end-date", default=None, help="Filter by Salesforce Case created date <= YYYY-MM-DD")
    p.add_argument("--ticket-ids", default=None, help="Comma-separated or JSON list of Case IDs/Numbers")
    a = p.parse_args()

    ticket_ids = parse_ticket_ids(a.ticket_ids) if a.ticket_ids else None

    settings = load_config(a.config)
    if a.db_url:
        settings.database_url = a.db_url

    engine = init_engine(settings.database_url)
    init_db(engine)

    with get_db() as session:
        summary = run_step2_entities(
            settings=settings,
            session=session,
            start_date=a.start_date,
            end_date=a.end_date,
            ticket_ids=ticket_ids,
        )
        print(f"Step 2 Complete: {summary['organizations_mapped']} Orgs, {summary['users_mapped']} End Users.")


if __name__ == "__main__":
    main()
