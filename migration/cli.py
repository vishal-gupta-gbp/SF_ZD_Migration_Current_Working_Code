"""Command-line interface for the database-backed migration pipeline."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from migration.config import load_config
from migration.db.connection import get_db, init_engine
from migration.db.models import (
    MigrationAttachment,
    MigrationRun,
    MigrationTicket,
    SFCase,
    ZendeskOrganization,
    ZendeskUser,
)
from migration.db.schema import init_db, reset_db
from migration.db.seeder import seed_mappings_if_needed
from migration.pipeline.runner import run_full_migration
from migration.pipeline.step1_export import run_step1_export
from migration.pipeline.step2_entities import run_step2_entities
from migration.pipeline.step3_prepare import run_step3_prepare
from migration.pipeline.step4_import import run_step4_import
from migration.reports.exporter import export_migration_reports


def build_parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--config", default=None, help="Optional path to config JSON file")
    shared.add_argument("--db-url", default=None, help="Override DATABASE_URL")

    parser = argparse.ArgumentParser(
        prog="migration",
        description="Salesforce to Zendesk Migration (Database-Driven Pipeline)",
        parents=[shared],
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Command: run (full pipeline)
    run_p = subparsers.add_parser("run", parents=[shared], help="Run full migration pipeline (steps 1 to 5)")
    run_p.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    run_p.add_argument("--end-date", required=True, help="YYYY-MM-DD (inclusive)")
    run_p.add_argument("--max-tickets", type=int, default=None, help="Limit number of tickets (0 = all)")
    run_p.add_argument("--archive-immediately", action="store_true", help="Archive tickets immediately in Zendesk")

    # Command: export (step 1)
    exp_p = subparsers.add_parser("export", parents=[shared], help="Step 1: Export Salesforce data & download S3 attachments")
    exp_p.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    exp_p.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    exp_p.add_argument("--max-tickets", type=int, default=None)

    # Command: entities (step 2)
    subparsers.add_parser("entities", parents=[shared], help="Step 2: Sync Zendesk Organizations and End Users")

    # Command: prepare (step 3)
    prep_p = subparsers.add_parser("prepare", parents=[shared], help="Step 3: Prepare Zendesk ticket payloads in database")
    prep_p.add_argument("--start-date", default=None)
    prep_p.add_argument("--end-date", default=None)
    prep_p.add_argument("--max-tickets", type=int, default=None)

    # Command: import (step 4 & 5)
    imp_p = subparsers.add_parser("import", parents=[shared], help="Step 4/5: Import prepared tickets into Zendesk")
    imp_p.add_argument("--type", choices=["standard", "phone", "all"], default="all", help="Ticket type to import")
    imp_p.add_argument("--archive-immediately", action="store_true")
    imp_p.add_argument("--start-date", default=None, help="YYYY-MM-DD")
    imp_p.add_argument("--end-date", default=None, help="YYYY-MM-DD")

    # Command: seed (mappings)
    seed_p = subparsers.add_parser("seed", parents=[shared], help="Seed mapping tables into database from Excel")
    seed_p.add_argument("--force", action="store_true", help="Force reload mappings")

    # Command: status (database overview)
    subparsers.add_parser("status", parents=[shared], help="Show current migration status and counts in database")

    # Command: report (export Excel/CSV)
    rep_p = subparsers.add_parser("report", parents=[shared], help="Export reconciliation reports from database")
    rep_p.add_argument("--output-dir", default=None, help="Output directory for reports")

    # Command: reset-db (caution)
    subparsers.add_parser("reset-db", parents=[shared], help="Reset database tables (drops and recreates schema)")

    return parser


def main(args: Optional[list] = None) -> int:
    parser = build_parser()
    parsed = parser.parse_args(args)

    settings = load_config(parsed.config)
    if parsed.db_url:
        settings.database_url = parsed.db_url

    engine = init_engine(settings.database_url)
    init_db(engine)

    if not parsed.command or parsed.command == "run":
        if not hasattr(parsed, "start_date") or not parsed.start_date:
            parser.print_help()
            return 1
        run_full_migration(
            settings=settings,
            start_date=parsed.start_date,
            end_date=parsed.end_date,
            max_tickets=parsed.max_tickets,
            archive_immediately=parsed.archive_immediately,
        )
        return 0

    elif parsed.command == "export":
        with get_db() as session:
            seed_mappings_if_needed(session, settings.mapping_file)
            run_step1_export(
                settings=settings,
                session=session,
                start_date=parsed.start_date,
                end_date=parsed.end_date,
                max_tickets=parsed.max_tickets,
            )
        return 0

    elif parsed.command == "entities":
        with get_db() as session:
            run_step2_entities(settings=settings, session=session)
        return 0

    elif parsed.command == "prepare":
        with get_db() as session:
            seed_mappings_if_needed(session, settings.mapping_file)
            run_step3_prepare(
                settings=settings,
                session=session,
                start_date=parsed.start_date,
                end_date=parsed.end_date,
                max_tickets=parsed.max_tickets,
            )
        return 0

    elif parsed.command == "import":
        with get_db() as session:
            ttype = None if parsed.type == "all" else parsed.type
            run_step4_import(
                settings=settings,
                session=session,
                ticket_type=ttype,
                archive_immediately=parsed.archive_immediately,
                start_date=parsed.start_date,
                end_date=parsed.end_date,
            )
        return 0

    elif parsed.command == "seed":
        with get_db() as session:
            res = seed_mappings_if_needed(session, settings.mapping_file, force=parsed.force)
            print(f"Mapping seed complete: {res}")
        return 0

    elif parsed.command == "status":
        with get_db() as session:
            total_cases = session.query(SFCase).count()
            orgs = session.query(ZendeskOrganization).count()
            users = session.query(ZendeskUser).count()
            tickets = session.query(MigrationTicket).count()
            completed = session.query(MigrationTicket).filter(MigrationTicket.status.in_(["completed", "existing", "completed_with_warnings"])).count()
            failed = session.query(MigrationTicket).filter_by(status="failed").count()
            review = session.query(MigrationTicket).filter_by(status="manual_review_required").count()
            attachments = session.query(MigrationAttachment).count()
            att_downloaded = session.query(MigrationAttachment).filter_by(download_status="success").count()
            att_uploaded = session.query(MigrationAttachment).filter_by(upload_status="success").count()

            print("=" * 60)
            print("DATABASE MIGRATION STATUS")
            print("=" * 60)
            print(f"Salesforce Cases in DB       : {total_cases:,}")
            print(f"Zendesk Organizations Synced : {orgs:,}")
            print(f"Zendesk End Users Synced     : {users:,}")
            print(f"Prepared Tickets in DB       : {tickets:,}")
            print(f"Imported / Completed Tickets : {completed:,}")
            print(f"Failed Tickets               : {failed:,}")
            print(f"Tickets Needing Review       : {review:,}")
            print(f"Attachments (Downloaded)     : {att_downloaded:,} / {attachments:,}")
            print(f"Attachments (Uploaded to ZD) : {att_uploaded:,} / {attachments:,}")
            print("=" * 60)
        return 0

    elif parsed.command == "report":
        out_dir = Path(parsed.output_dir) if parsed.output_dir else settings.workspace_dir / "import"
        with get_db() as session:
            res = export_migration_reports(session, out_dir)
            print(f"Reports generated: {res}")
        return 0

    elif parsed.command == "reset-db":
        confirm = input("Are you sure you want to drop and reset all migration database tables? (y/N): ")
        if confirm.lower() == "y":
            reset_db(engine)
            print("Database schema reset successfully.")
        else:
            print("Operation aborted.")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
