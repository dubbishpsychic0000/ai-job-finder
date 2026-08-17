"""Pytest configuration.

Forces an isolated test database + the deterministic offline LLM + email
disabled, BEFORE any app module is imported (module-level engine reads env).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["DATABASE_URL"] = f"sqlite:///{ROOT / 'data' / 'test_agent.db'}"
os.environ["LLM_PROVIDER"] = "null"
os.environ["ENABLE_EMAIL"] = "false"

# wipe any previous test DB
_db_path = ROOT / "data" / "test_agent.db"
_db_path.unlink(missing_ok=True)

import pytest  # noqa: E402  (test env must be set before app imports)

from app.config import get_config, get_preferences, get_profile, get_settings  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402


@pytest.fixture(scope="session")
def session_factory():
    init_db()
    return SessionLocal


@pytest.fixture(autouse=True)
def _clean_tables(session_factory):
    """Truncate every table before each test for full isolation."""
    session = session_factory()
    try:
        from app import models

        for table in reversed(models.Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    finally:
        session.close()


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def config():
    return get_config()


@pytest.fixture()
def profile():
    """Deterministic test profile (independent of the user's candidate/profile.yaml)."""
    return get_profile(Path(__file__).parent / "fixtures" / "profile.yaml")


@pytest.fixture()
def prefs():
    """Deterministic test preferences (independent of the user's candidate/preferences.yaml)."""
    return get_preferences(Path(__file__).parent / "fixtures" / "preferences.yaml")


@pytest.fixture()
def settings():
    return get_settings()


@pytest.fixture(autouse=True)
def _fake_cv(tmp_path, monkeypatch):
    """Tests must never depend on the gitignored real candidate/ CVs (absent in CI)."""
    from app import config as _config
    from app.config import get_profile
    from app.email import service as _email_service

    cv = tmp_path / "cv_test.pdf"
    cv.write_bytes(b"%PDF-1.4 fake CV")
    monkeypatch.setattr(_email_service, "resolve_attachment", lambda cv_dir, lang: str(cv))
    fixture_profile = get_profile(ROOT / "tests" / "fixtures" / "profile.yaml")
    monkeypatch.setattr(_config, "get_profile", lambda: fixture_profile)
