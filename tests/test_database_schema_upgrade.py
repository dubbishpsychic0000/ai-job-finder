from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import database, models


def test_schema_upgrade_adds_missing_columns_idempotently(tmp_path, monkeypatch):
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with legacy_engine.begin() as connection:
        for table in ("emails", "jobs", "companies", "sources", "query_stats"):
            connection.execute(text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
    monkeypatch.setattr(database, "engine", legacy_engine)

    database._upgrade_schema()
    database._upgrade_schema()

    inspector = inspect(legacy_engine)
    assert {"message_id", "draft_id", "mode"} <= {
        column["name"] for column in inspector.get_columns("emails")
    }
    assert {
        "closing_at", "last_verified_at", "posted_at_source",
        "posted_at_evidence_url", "entity_type", "entity_state",
        "entity_reason_code", "entity_evidence_url",
        "public_competition",
    } <= {
        column["name"] for column in inspector.get_columns("jobs")
    }
    assert {"last_success_at", "last_failure_at"} <= {
        column["name"] for column in inspector.get_columns("sources")
    }
    legacy_engine.dispose()


def test_schema_upgrade_adds_portal_alert_dedup_index_when_notifications_exist(
    tmp_path, monkeypatch,
):
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'notification-legacy.db'}")
    with legacy_engine.begin() as connection:
        connection.execute(text("CREATE TABLE emails (id INTEGER PRIMARY KEY)"))
        connection.execute(text(
            "CREATE TABLE notifications (id INTEGER PRIMARY KEY, job_id INTEGER, "
            "event_type VARCHAR(64))"
        ))
    monkeypatch.setattr(database, "engine", legacy_engine)

    database._upgrade_schema()

    assert "uq_notifications_portal_alert_job" in {
        index["name"] for index in inspect(legacy_engine).get_indexes("notifications")
    }
    legacy_engine.dispose()


def test_phase_24_job_columns_are_migrated_before_orm_reads(tmp_path, monkeypatch):
    legacy_engine = create_engine(f"sqlite:///{tmp_path / 'phase-24-legacy.db'}")
    models.Base.metadata.create_all(legacy_engine)
    with legacy_engine.begin() as connection:
        connection.execute(text("DROP INDEX IF EXISTS ix_jobs_entity_type"))
        connection.execute(text("DROP INDEX IF EXISTS ix_jobs_entity_state"))
        for name in (
            "posted_at_source",
            "posted_at_evidence_url",
            "entity_type",
            "entity_state",
            "entity_reason_code",
            "entity_evidence_url",
            "public_competition",
        ):
            connection.execute(text(f"ALTER TABLE jobs DROP COLUMN {name}"))

    with Session(legacy_engine) as session, pytest.raises(OperationalError):
        session.execute(select(models.Job)).all()

    monkeypatch.setattr(database, "engine", legacy_engine)
    database._upgrade_schema()

    with Session(legacy_engine) as session:
        assert session.execute(select(models.Job)).all() == []
    legacy_engine.dispose()
