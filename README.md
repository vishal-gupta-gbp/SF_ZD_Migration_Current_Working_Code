# Salesforce to Zendesk Migration (Database-Driven Pipeline)

A containerized, database-backed migration engine for migrating Salesforce Cases, EmailMessages, Contacts, Accounts, Tasks, and S3 attachments into Zendesk Sandbox/Production with 1:1 `external_id` validation and fault tolerance.

---

## Key Architectural Upgrades

1. **Database-Centric Architecture (Replaced Excel/CSV Intermediate Files)**:
   - Intermediate state is no longer tracked via loose CSVs, Excel workbooks, or mutable JSON files (`organization_map.json`, `ticket_state.json`, etc.).
   - All state is managed by **PostgreSQL** (with optional SQLite support) with transactional integrity (ACID), B-Tree indexes, and structured tables.
   - Field mappings and picklist value mappings from `Salesforce_to_Zendesk_Case_Field_Mapping.xlsx` are seeded into relational tables (`mapping_fields` and `mapping_values`), eliminating repeated Excel parsing.

2. **Dockerized & Orchestrated**:
   - Production-ready `Dockerfile` (Python 3.11-slim, non-root user, multi-stage caching).
   - `docker-compose.yml` orchestrates PostgreSQL 16 Alpine and the migration worker container with healthchecks and volume persistence.

3. **Performance & Concurrency**:
   - **S3 Downloads**: Multi-threaded downloader with SHA256 checksum validation and automatic deduplication / resumption.
   - **Zendesk Bulk Import**: Batches up to 100 tickets per request using Zendesk's `/api/v2/imports/tickets/create_many.json` API with automated async job status polling.
   - **Attachment Uploads**: Concurrent uploads directly to Zendesk `/api/v2/uploads.json`, linking tokens to specific comment IDs.

4. **100% Business Logic Fidelity**:
   - **Authorship**: Customer requester preserved for incoming emails; agent assignment / Akarsh Shrivastava proxy author preserved for outgoing messages.
   - **Signature & HTML Conversion**: Converts signature image markup (`[img]<url>` -> `<a><img></a>`), sanitized HTML bodies.
   - **Deterministic Synthetic Emails**: Cases without email addresses generate deterministic `sfcase_{sha12(case_id)}@sfmigrateduser.com` identities.
   - **Phone-Origin Cases**: Extracts call recording URLs from Task records and appends them to private internal notes.
   - **Invariant Enforcement**: Every Zendesk ticket's `external_id` is strictly verified against Salesforce `Case.Id`.

---

## Quick Start with Docker

### 1. Environment Setup

Configure your credentials in `.env` (Salesforce consumer key/secret, Zendesk token, AWS credentials, PostgreSQL settings).

### 2. Start PostgreSQL Database

```bash
docker compose up -d postgres
```

### 3. Check Current Database Migration Status

```bash
docker compose run --rm migration status
```

### 4. Run Migration

Run the full end-to-end pipeline (Export -> Entities -> Prepare -> Import):

```bash
# Test with 10 tickets
docker compose run --rm migration run --start-date 2026-09-01 --end-date 2026-09-30 --max-tickets 10

# Full migration (all cases in range)
docker compose run --rm migration run --start-date 2026-09-01 --end-date 2026-09-30 --max-tickets 0
```

### 5. Export Reconciliation Reports

Generate final executive summary Excel (`Migration_Summary.xlsx`) and CSV reports from the database:

```bash
docker compose run --rm migration report
```

Reports are saved to `migration_workspace/import/Migration_Summary.xlsx`.

---

## Step-by-Step CLI Commands

You can run individual stages independently:

```bash
# Step 1: Export Salesforce data & download S3 attachments into database
docker compose run --rm migration export --start-date 2026-09-01 --end-date 2026-09-30 --max-tickets 100

# Step 2: Migrate Organizations and End Users
docker compose run --rm migration entities

# Step 3: Prepare ticket payloads into database
docker compose run --rm migration prepare

# Step 4: Import standard tickets
docker compose run --rm migration import --type standard

# Step 5: Import phone tickets
docker compose run --rm migration import --type phone

# Seed/update mapping tables from Excel
docker compose run --rm migration seed --force
```

---

## Local Execution (Without Docker)

You can run directly with Python using local PostgreSQL or embedded SQLite:

```bash
# Install dependencies
pip install -r requirements.txt

# Run full pipeline with SQLite
python -m migration run --start-date 2026-09-01 --end-date 2026-09-30 --max-tickets 10 --db-url sqlite:///migration_workspace/migration.db

# Or use backward-compatible runners:
python 00_run_migration.py --start-date 2026-09-01 --end-date 2026-09-30 --max-tickets 10
python 01_export.py --start-date 2026-09-01 --end-date 2026-09-30
python 02_migrate_entities.py
python 03_prepare_tickets.py
python 04_import_standard_tickets.py
python 05_import_phone_tickets.py
```

---

## Database Schema Overview

| Table | Purpose |
|---|---|
| `migration_runs` | Tracks overall migration executions, parameters, and status |
| `mapping_fields` | Salesforce API fields to Zendesk custom fields mapping |
| `mapping_values` | Picklist and option values mapping |
| `sf_cases` | Salesforce raw Case records staged in DB |
| `sf_contacts` | Salesforce Contact records |
| `sf_accounts` | Salesforce Account records |
| `sf_users` | Salesforce User records |
| `sf_record_types` | Salesforce RecordType definitions |
| `sf_email_messages`| EmailMessage threading data |
| `sf_s3_records` | BP_S3__c attachment metadata |
| `sf_tasks` | Phone Tasks with call recording links |
| `zendesk_organizations` | Salesforce Account to Zendesk Organization ID mapping cache |
| `zendesk_users` | Customer identity to Zendesk User ID mapping cache |
| `migration_attachments` | S3 download & Zendesk upload token tracking |
| `migration_tickets` | Staged ticket payloads, import status, attempts, error classification |
| `migration_audit_logs` | Real-time audit and error log records |
