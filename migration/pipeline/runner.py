"""Full migration runner orchestrating steps 1 to 5 with database tracking."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from migration.config import MigrationSettings
from migration.db.connection import get_db, init_engine
from migration.db.models import MigrationRun, MigrationTicket
from migration.db.schema import init_db
from migration.db.seeder import seed_mappings_if_needed
from migration.pipeline.step1_export import run_step1_export
from migration.pipeline.step2_entities import run_step2_entities
from migration.pipeline.step3_prepare import run_step3_prepare
from migration.pipeline.step4_import import run_step4_import


def run_full_migration(
    settings: MigrationSettings,
    start_date: str,
    end_date: str,
    max_tickets: Optional[int] = None,
    archive_immediately: Optional[bool] = None,
    source_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute the complete end-to-end migration pipeline."""
    limit_val = max_tickets if max_tickets is not None else settings.max_tickets
    active_source = (source_filter or settings.source_filter or "all").lower().strip()

    print("=" * 70)
    print("SALESFORCE -> ZENDESK DATABASE-BACKED MIGRATION PIPELINE")
    print("=" * 70)
    print(f"Date Range    : {start_date} -> {end_date} (inclusive)")
    print(f"Max Tickets   : {'ALL' if limit_val == 0 else limit_val}")
    print(f"Database URL  : {settings.database_url.split('@')[-1] if '@' in settings.database_url else settings.database_url}")
    print("=" * 70)

    # 1. Initialize Database & Schema
    engine = init_engine(settings.database_url)
    init_db(engine)

    start_time = time.perf_counter()
    summary: Dict[str, Any] = {}

    with get_db() as session:
        # Seed field mappings if not already in DB
        seed_res = seed_mappings_if_needed(session, settings.mapping_file)
        if seed_res.get("seeded"):
            print(f"Loaded {seed_res['field_mappings']} fields and {seed_res['value_mappings']} values into DB.")

        # Create MigrationRun
        run = MigrationRun(
            start_date=start_date,
            end_date=end_date,
            max_tickets=limit_val,
            status="in_progress",
        )
        session.add(run)
        session.commit()
        run_id = run.id

    try:
        # STEP 1: Export
        with get_db() as session:
            s1 = run_step1_export(
                settings=settings,
                session=session,
                start_date=start_date,
                end_date=end_date,
                max_tickets=limit_val,
                source_filter=active_source,
            )
            summary["step1_export"] = s1

        # STEP 2: Entities
        with get_db() as session:
            s2 = run_step2_entities(settings=settings, session=session)
            summary["step2_entities"] = s2

        # STEP 3: Prepare
        with get_db() as session:
            s3 = run_step3_prepare(
                settings=settings,
                session=session,
                start_date=start_date,
                end_date=end_date,
                max_tickets=limit_val,
            )
            summary["step3_prepare"] = s3

        # STEP 4: Import Standard Tickets
        with get_db() as session:
            s4 = run_step4_import(
                settings=settings,
                session=session,
                ticket_type="standard",
                archive_immediately=archive_immediately,
                start_date=start_date,
                end_date=end_date,
            )
            summary["step4_import_standard"] = s4

        # STEP 5: Import Phone Tickets
        with get_db() as session:
            s5 = run_step4_import(
                settings=settings,
                session=session,
                ticket_type="phone",
                archive_immediately=archive_immediately,
                start_date=start_date,
                end_date=end_date,
            )
            summary["step5_import_phone"] = s5

        duration = time.perf_counter() - start_time
        summary["duration_seconds"] = round(duration, 2)

        with get_db() as session:
            db_run = session.get(MigrationRun, run_id)
            if db_run:
                db_run.status = "completed"
                db_run.completed_at = datetime.now(timezone.utc)
                db_run.summary_json = summary

        print()
        print("=" * 70)
        print(f"MIGRATION COMPLETED SUCCESSFULLY IN {duration:.1f}s")
        print("=" * 70)

        return summary

    except Exception as exc:
        with get_db() as session:
            db_run = session.get(MigrationRun, run_id)
            if db_run:
                db_run.status = "failed"
                db_run.completed_at = datetime.now(timezone.utc)
                db_run.summary_json = {"error": str(exc)}
        print(f"MIGRATION FAILED: {exc}")
        raise
