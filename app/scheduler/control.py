"""Simple runtime controls (pause/resume) persisted to data/control.json.

Separate from the env-based settings: operators can flip `wca pause` without
re-deploying. The pipeline checks this at the start of every run block.
"""
from __future__ import annotations

import json

from app.config import ROOT_DIR

CONTROL_FILE = ROOT_DIR / "data" / "control.json"


def _load() -> dict:
    if CONTROL_FILE.exists():
        try:
            return json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save(state: dict) -> None:
    CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONTROL_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def is_paused() -> bool:
    return bool(_load().get("paused", False))


def set_paused(value: bool) -> bool:
    state = _load()
    state["paused"] = value
    _save(state)
    return value
