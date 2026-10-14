#!/usr/bin/env python3
"""Step 2 - Bulk-optimized Organizations and End Users migration directly into database."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from migration.config import load_config
from migration.db.connection import get_db, init_engine
from migration.db.schema import init_db
from migration.pipeline.step2_entities import run_step2_entities


def main():
    p = argparse.ArgumentParser(description="Step 2 - Bulk-optimized Organizations and End Users migration")
    p.add_argument("--config", default=None, help="Optional path to config JSON file")
    p.add_argument("--db-url", default=None, help="Override database URL")
    a = p.parse_args()

    settings = load_config(a.config)
    if a.db_url:
        settings.database_url = a.db_url

    engine = init_engine(settings.database_url)
    init_db(engine)

    with get_db() as session:
        summary = run_step2_entities(settings=settings, session=session)
        print(f"Step 2 Complete: {summary['organizations_mapped']} Orgs, {summary['users_mapped']} End Users.")


if __name__ == "__main__":
    main()
