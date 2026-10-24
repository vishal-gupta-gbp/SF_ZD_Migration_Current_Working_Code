#!/usr/bin/env python3
"""
Salesforce -> Zendesk Migration Runner (Database-Driven Pipeline)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from migration.config import load_config
from migration.pipeline.runner import run_full_migration


def parse_args():
    p = argparse.ArgumentParser(description="Salesforce -> Zendesk Migration Runner (Database-Driven)")
    p.add_argument("--start-date", default=None, help="YYYY-MM-DD")
    p.add_argument("--end-date", default=None, help="YYYY-MM-DD inclusive")
    p.add_argument("--ticket-ids", "--case-ids", "--sf-ticket-ids", default=None, help="Comma-separated Salesforce Case IDs or Case Numbers")
    p.add_argument(
        "--max-tickets",
        type=int,
        default=None,
        help="Override config max_tickets; 0 = all",
    )
    p.add_argument("--config", default=None, help="Optional path to config JSON file")
    p.add_argument("--archive-immediately", action="store_true")
    p.add_argument("--db-url", default=None, help="Override database URL")
    p.add_argument(
        "--source-filter",
        default=None,
        choices=["all", "portal_api", "non_portal_api"],
        help="Filter Salesforce cases by BP_Source__c ('portal_api' for Portal (Ticket API), 'non_portal_api', or 'all')",
    )
    return p.parse_args()


def parse_ticket_ids(val: Optional[str]) -> Optional[list[str]]:
    if not val:
        return None
    ids = [x.strip().strip("'\"") for x in val.split(",")]
    cleaned = [x for x in ids if x]
    return cleaned if cleaned else None


def main():
    args = parse_args()
    settings = load_config(args.config)
    if args.db_url:
        settings.database_url = args.db_url
    if args.source_filter:
        settings.source_filter = args.source_filter.lower().strip()

    ticket_ids = parse_ticket_ids(args.ticket_ids)
    if not args.start_date and not ticket_ids:
        print("Error: Either --start-date or --ticket-ids must be provided.", file=sys.stderr)
        sys.exit(1)

    if args.max_tickets is not None:
        if args.max_tickets < 0:
            raise ValueError("max_tickets cannot be negative.")
        settings.max_tickets = args.max_tickets

    try:
        run_full_migration(
            settings=settings,
            start_date=args.start_date,
            end_date=args.end_date,
            max_tickets=settings.max_tickets,
            archive_immediately=args.archive_immediately,
            ticket_ids=ticket_ids,
        )
    except Exception as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
