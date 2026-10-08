from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from app import database


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
    assert {"closing_at", "last_verified_at"} <= {
        column["name"] for column in inspector.get_columns("jobs")
    }
    assert {"last_success_at", "last_failure_at"} <= {
        column["name"] for column in inspector.get_columns("sources")
    }
    legacy_engine.dispose()
