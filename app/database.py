"""Database engine + session management. SQLite by default; Postgres via DATABASE_URL.

Usage (FastAPI/CLI):
    from app.database import engine, session_scope
    Base.metadata.create_all(engine)   # in main / alembic-less MVP
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.config import get_settings


def _build_engine() -> Engine:
    settings = get_settings()
    url = settings.database_url
    kwargs: dict = {"echo": False}
    if url.startswith("sqlite"):
        db_path = Path(url.split("///")[-1])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=True, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):  # pragma: no cover - sqlite only
    if engine.url.drivername == "sqlite":
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def init_db() -> None:
    models.Base.metadata.create_all(engine)
    _upgrade_schema()


def _upgrade_schema() -> None:
    """Idempotent ALTER TABLEs for columns added after the first release."""
    if not engine.url.drivername.startswith("sqlite"):
        return
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    email_columns = {c["name"] for c in insp.get_columns("emails")} if insp.has_table("emails") else set()
    with engine.begin() as conn:
        if "message_id" not in email_columns:
            conn.execute(text("ALTER TABLE emails ADD COLUMN message_id VARCHAR(255) DEFAULT ''"))
        if "draft_id" not in email_columns:
            conn.execute(text("ALTER TABLE emails ADD COLUMN draft_id VARCHAR(255) DEFAULT ''"))
        if "mode" not in email_columns:
            conn.execute(text("ALTER TABLE emails ADD COLUMN mode VARCHAR(16) DEFAULT ''"))

    _add_missing_columns(insp, "jobs", {
        "source_type": "VARCHAR(32) DEFAULT ''",
        "source_quality": "INTEGER",
        "source_confidence": "INTEGER",
        "closing_at": "DATETIME",
        "language": "VARCHAR(16) DEFAULT ''",
        "sponsorship_signal": "VARCHAR(16) DEFAULT 'unknown'",
        "international_candidate_signal": "VARCHAR(16) DEFAULT 'unknown'",
        "relocation_signal": "VARCHAR(16) DEFAULT 'unknown'",
        "work_permit_signal": "VARCHAR(16) DEFAULT 'unknown'",
        "verification_status": "VARCHAR(16) DEFAULT 'verified'",
        "search_query": "VARCHAR(512) DEFAULT ''",
        "search_language": "VARCHAR(16) DEFAULT ''",
        "search_country": "VARCHAR(64) DEFAULT ''",
        "canonical_job_id": "VARCHAR(64) DEFAULT ''",
        "freshness": "VARCHAR(16) DEFAULT 'unknown'",
        "last_verified_at": "DATETIME",
        "opportunity_type": "VARCHAR(32) DEFAULT 'JOB'",
        "application_method": "VARCHAR(32) DEFAULT 'UNKNOWN'",
        "application_url": "VARCHAR(1024) DEFAULT ''",
    })

    _add_missing_columns(insp, "companies", {
        "industry": "VARCHAR(128) DEFAULT ''",
        "careers_url": "VARCHAR(1024) DEFAULT ''",
        "recruitment_url": "VARCHAR(1024) DEFAULT ''",
        "international_recruitment_signal": "VARCHAR(16) DEFAULT 'unknown'",
        "sponsorship_signal": "VARCHAR(16) DEFAULT 'unknown'",
        "last_checked_at": "DATETIME",
        "source": "VARCHAR(128) DEFAULT ''",
    })

    _add_missing_columns(insp, "sources", {
        "last_success_at": "DATETIME",
        "last_failure_at": "DATETIME",
        "rate_limit_status": "VARCHAR(16) DEFAULT 'ok'",
    })

    _add_missing_columns(insp, "query_stats", {
        "interviews": "INTEGER DEFAULT 0",
    })


def _add_missing_columns(insp, table: str, columns: dict[str, str]) -> None:
    """Idempotently add missing columns to a table (SQLite ALTER TABLE)."""
    if not insp.has_table(table):
        return
    from sqlalchemy import text

    existing = {c["name"] for c in insp.get_columns(table)}
    with engine.begin() as conn:
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


@contextmanager
def session_scope():
    """Transactional session context manager."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db():
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
