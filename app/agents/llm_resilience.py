"""Resilience layer for LLM calls — cache + per-key budgets + key rotation +
heuristic fallback.

The Gemini free tier caps requests per day per API key (e.g. 20). To keep the
agent functional regardless of quota or transient API failures:

  * multiple Gemini API keys form a *ring*: each key has its own daily budget,
    and calls use them in order — a key that hits a quota error (429) is
    exhausted for the day and the ring moves to the next key;
  * every *successful* LLM response is cached on disk (prompt-hash key), so
    re-runs and repeated analyses cost zero extra calls;
  * when every key is spent/failing, the deterministic offline heuristics run
    for the remaining calls — the pipeline never stalls on a busy API.

Fallback responses are deliberately NOT cached, so once quota frees up the
Gemini keys are tried again.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agents.llm import LLMProvider

logger = logging.getLogger(__name__)


def _cache_key(provider: str, system: str, user: str) -> str:
    payload = f"{provider}\x00{system}\x00{user}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_quota_error(exc: Exception) -> bool:
    """True for provider quota/rate-limit errors (429, RESOURCE_EXHAUSTED...)."""
    message = str(exc).lower()
    return any(token in message for token in (
        "429", "quota", "resource_exhausted", "rate-limit", "daily limit"))


def key_fingerprint(api_key: str, length: int = 8) -> str:
    """Short, non-reversible id for a credential — safe for logs/file names."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:length]


class LLMCache:
    """Disk-backed prompt -> response cache with atomic writes."""

    def __init__(self, path: Path, max_entries: int = 2000):
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        except Exception as exc:  # corrupt cache must never break the agent
            logger.warning("LLM cache unreadable (%s); starting fresh", exc)
            self._data = {}

    def get(self, key: str) -> Any:
        return self._data.get(key)

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value
            if len(self._data) > self.max_entries:
                for old in list(self._data)[: len(self._data) - self.max_entries]:
                    self._data.pop(old, None)
            self._save()

    def _save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("could not persist LLM cache: %s", exc)


class DailyBudget:
    """Tracks how many primary-LLM calls were spent *today* (UTC)."""

    def __init__(self, path: Path, limit: int):
        self.path = path
        self.limit = int(limit)
        self._lock = threading.Lock()
        self._date = ""
        self._count = 0
        self._load()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if raw.get("date") == self._today():
                self._date = raw["date"]
                self._count = int(raw.get("count", 0))
        except Exception:
            self._date, self._count = "", 0

    def _save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump({"date": self._date, "count": self._count}, fh)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("could not persist LLM budget: %s", exc)

    def remaining(self) -> int:
        if self.limit <= 0:
            return 1 << 30  # unlimited
        if self._date != self._today():
            return self.limit
        return max(0, self.limit - self._count)

    def record(self) -> None:
        if self.limit <= 0:
            return
        with self._lock:
            if self._date != self._today():
                self._date, self._count = self._today(), 0
            self._count += 1
            self._save()

    def exhaust(self) -> None:
        """Mark the daily budget as spent (e.g. after an API quota error)."""
        if self.limit <= 0:
            return
        with self._lock:
            self._date = self._today()
            self._count = self.limit
            self._save()


@dataclass
class PrimaryKey:
    """One candidate API credential + its own daily spending budget."""
    llm: LLMProvider
    budget: DailyBudget


class ResilientLLM:
    """Wrapper: cache -> (budget-gated) rotating API keys -> heuristic fallback.

    `primaries` is a ring of keys. Each call tries the keys in order, skipping
    any whose daily budget is spent; a quota error permanently exhausts that
    key's budget for the day so the ring fast-forwards to the next key. Only
    when every key is spent or failing does the deterministic `fallback` run.
    """

    name = "resilient"

    def __init__(self, fallback: LLMProvider, cache: LLMCache,
                 primaries: list[PrimaryKey] | None = None,
                 primary: LLMProvider | None = None,
                 budget: DailyBudget | None = None):
        if primaries:
            self.primaries = list(primaries)
        elif primary is not None and budget is not None:
            self.primaries = [PrimaryKey(llm=primary, budget=budget)]
        else:
            raise TypeError("provide either primaries=[] or primary=+budget=")
        self.fallback = fallback
        self.cache = cache
        self.name = f"resilient:{self.primaries[0].llm.name}"

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        key = _cache_key(self.primaries[0].llm.name, system, user)
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        for pk in self.primaries:
            if pk.budget.remaining() <= 0:
                continue
            try:
                result = await pk.llm.complete_json(system, user)
                if isinstance(result, dict) and result:
                    pk.budget.record()
                    self.cache.put(key, result)
                    return result
            except Exception as exc:
                logger.warning("LLM %s failed: %s -> trying next key/fallback",
                               pk.llm.name, str(exc)[:200])
                if _is_quota_error(exc):
                    pk.budget.exhaust()

        if len(self.primaries) > 1:
            logger.info("all Gemini keys spent/failed -> heuristic fallback")
        return await self.fallback.complete_json(system, user)