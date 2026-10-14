# ==============================================================================
# Dockerfile for Salesforce -> Zendesk Migration (Database-Driven Pipeline)
# ==============================================================================
FROM python:3.11-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated application directory
WORKDIR /app

# Copy dependency specifications first to leverage Docker layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY migration /app/migration
COPY 00_run_migration.py /app/00_run_migration.py
COPY 01_export.py /app/01_export.py
COPY 02_migrate_entities.py /app/02_migrate_entities.py
COPY 03_prepare_tickets.py /app/03_prepare_tickets.py
COPY 04_import_standard_tickets.py /app/04_import_standard_tickets.py
COPY 05_import_phone_tickets.py /app/05_import_phone_tickets.py
COPY docker-entrypoint.sh /app/docker-entrypoint.sh
COPY Salesforce_to_Zendesk_Case_Field_Mapping.xlsx /app/Salesforce_to_Zendesk_Case_Field_Mapping.xlsx

# Create non-root user and setup directories
RUN useradd -m -u 1000 migration_user && \
    mkdir -p /app/migration_workspace && \
    chmod +x /app/docker-entrypoint.sh && \
    chown -R migration_user:migration_user /app

# Switch to non-root user
USER migration_user

# Set entrypoint
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# Default command displays current database status
CMD ["status"]
