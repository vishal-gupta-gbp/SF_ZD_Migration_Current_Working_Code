"""Database schema initialization and maintenance."""
from __future__ import annotations

from sqlalchemy.engine import Engine

from migration.db.connection import Base
import migration.db.models  # ensure models are imported and registered on Base


def init_db(engine: Engine) -> None:
    """Create all database tables and indexes."""
    Base.metadata.create_all(bind=engine)


def reset_db(engine: Engine) -> None:
    """Drop and recreate all tables (use with caution)."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
