#!/bin/bash
set -e

# If running against PostgreSQL, wait for database readiness
if [[ "$DATABASE_URL" =~ ^postgresql ]]; then
    echo "Waiting for PostgreSQL database to accept connections..."
    python3 -c "
import os, sys, time, urllib.parse
import psycopg2

db_url = os.getenv('DATABASE_URL', '')
if not db_url:
    sys.exit(0)

# Replace driver prefix if present
cleaned_url = db_url.replace('postgresql+psycopg2://', 'postgresql://')
parsed = urllib.parse.urlparse(cleaned_url)

for i in range(30):
    try:
        conn = psycopg2.connect(
            dbname=parsed.path.lstrip('/'),
            user=parsed.username,
            password=parsed.password,
            host=parsed.hostname,
            port=parsed.port or 5432,
            connect_timeout=3
        )
        conn.close()
        print('PostgreSQL is ready and accepting connections.')
        sys.exit(0)
    except Exception as exc:
        time.sleep(1)

print('Warning: PostgreSQL connection check timed out; continuing anyway.')
"
fi

# Automatically seed mappings if running a migration command
if [[ "$1" == "run" || "$1" == "export" || "$1" == "prepare" ]]; then
    echo "Verifying database mapping tables..."
    python3 -m migration seed || true
fi

# If command is provided, pass through to python -m migration
if [ "$#" -eq 0 ]; then
    exec python3 -m migration status
elif [ "$1" = "python" ] || [ "$1" = "python3" ] || [ "$1" = "bash" ] || [ "$1" = "sh" ]; then
    exec "$@"
else
    exec python3 -m migration "$@"
fi
