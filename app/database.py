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
