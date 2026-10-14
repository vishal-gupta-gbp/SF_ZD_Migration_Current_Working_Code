#!/usr/bin/env python3
"""Step 5 - Import phone tickets using Bulk Import API and database tracking."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from migration.config import load_config
from migration.db.connection import get_db, init_engine
from migration.db.schema import init_db
from migration.pipeline.step4_import import run_step4_import


def main():
    p = argparse.ArgumentParser(description="Step 5 - Import phone tickets into Zendesk")
    p.add_argument("--config", default=None, help="Optional path to config JSON file")
    p.add_argument("--archive-immediately", action="store_true")
    p.add_argument("--start-date", default=None, help="YYYY-MM-DD")
    p.add_argument("--end-date", default=None, help="YYYY-MM-DD")
    p.add_argument("--db-url", default=None, help="Override database URL")
    a = p.parse_args()

    settings = load_config(a.config)
    if a.db_url:
        settings.database_url = a.db_url

    engine = init_engine(settings.database_url)
    init_db(engine)

    with get_db() as session:
        summary = run_step4_import(
            settings=settings,
            session=session,
            ticket_type="phone",
            archive_immediately=a.archive_immediately,
            start_date=a.start_date,
            end_date=a.end_date,
        )
        print(f"Step 5 Complete: {summary['completed']} imported, {summary['existing']} existing, {summary['failed']} failed.")


if __name__ == "__main__":
    main()
