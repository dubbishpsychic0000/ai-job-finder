"""Production configuration must never silently fall back to fixture jobs."""
from __future__ import annotations

from app.config import ROOT_DIR, load_yaml
from app.workflows.discovery import _load_source_connectors


def test_production_sources_are_public_and_fixture_free():
    cfg = load_yaml(ROOT_DIR / "config" / "sources.yaml")
    enabled = [item for item in cfg["connectors"] if item.get("enabled", True)]
    assert enabled
    assert all(item.get("mode") == "real" for item in enabled)
    assert all(item["kind"] != "static_files" for item in cfg["connectors"])


def test_demo_sources_are_explicitly_offline_only():
    cfg = load_yaml(ROOT_DIR / "config" / "sources_demo.yaml")
    assert cfg["connectors"][0]["kind"] == "static_files"
    assert cfg["connectors"][0]["mode"] == "demo"


def test_connector_loader_keeps_mode_as_diagnostics_metadata():
    rows = _load_source_connectors(ROOT_DIR / "config" / "sources_demo.yaml")
    assert rows and rows[0][0]["mode"] == "demo"
