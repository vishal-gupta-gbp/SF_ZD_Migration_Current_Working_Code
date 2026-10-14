"""Entrypoint for python -m migration."""
import sys
from migration.cli import main

if __name__ == "__main__":
    sys.exit(main())
