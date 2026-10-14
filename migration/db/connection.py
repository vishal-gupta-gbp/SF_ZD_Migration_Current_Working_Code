"""Database connection and session management."""
from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

Base = declarative_base()

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def init_engine(db_url: str) -> Engine:
    """Initialize SQLAlchemy engine with connection pool settings."""
    global _engine, _SessionFactory

    # If SQLite file path doesn't exist, ensure parent directory exists
    if db_url.startswith("sqlite:///"):
        sqlite_path = db_url.replace("sqlite:///", "")
        if sqlite_path and sqlite_path != ":memory:":
            Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            db_url,
            connect_args={"check_same_thread": False},
        )
    else:
        # PostgreSQL pool configuration
        engine = create_engine(
            db_url,
            pool_size=15,
            max_overflow=25,
            pool_timeout=30,
            pool_pre_ping=True,
        )

    _engine = engine
    _SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database engine not initialized. Call init_engine(db_url) first.")
    return _engine


@contextlib.contextmanager
def get_db() -> Generator[Session, None, None]:
    """Provide a transactional database session scope."""
    if _SessionFactory is None:
        raise RuntimeError("Database session factory not initialized. Call init_engine(db_url) first.")
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
