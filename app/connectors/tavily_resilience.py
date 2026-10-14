"""Resilience layer for Tavily API — key rotation + per-key daily budgets.

The Tavily free tier caps requests per day per API key. To keep the agent
functional regardless of quota or transient API failures:

  * multiple Tavily API keys form a *ring*: each key has its own daily budget,
    and calls use them in order — a key that hits a quota error (429/432) is
    exhausted for the day and the ring moves to the next key;
  * successful responses are cached on disk (query-hash key), so re-runs
    cost zero extra calls;
  * when every key is spent/failing, the connector returns empty results —
    the pipeline degrades gracefully without stalling.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from app.connectors.base import Opportunity

logger = logging.getLogger(__name__)

TAVILY_DAILY_REQUEST_LIMIT = 20
BTP_TAVILY_DAILY_RESERVE = 3
GENERAL_TAVILY_DAILY_LIMIT = TAVILY_DAILY_REQUEST_LIMIT - BTP_TAVILY_DAILY_RESERVE
TAVILY_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
_budget_locks_guard = threading.Lock()
_budget_locks: dict[str, threading.RLock] = {}


def _budget_thread_lock(identity: str) -> threading.RLock:
    with _budget_locks_guard:
        return _budget_locks.setdefault(identity, threading.RLock())


def _cache_key(query: str, location: str) -> str:
    payload = f"tavily\x00{query}\x00{location}".encode()
    return hashlib.sha256(payload).hexdigest()


def _is_quota_error(exc: Exception) -> bool:
    """True for Tavily quota/rate-limit errors (429, 432)."""
    message = str(exc).lower()
    return any(token in message for token in ("429", "432", "quota", "rate-limit", "daily limit", "too many requests"))


def key_fingerprint(api_key: str, length: int = 8) -> str:
    """Short, non-reversible id for a credential — safe for logs/file names."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:length]


@contextmanager
def _budget_file_lock(path: Path):
    """Serialize ledger updates across threads and processes on Windows and POSIX."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EDEADLOCK} and getattr(
                        exc, "winerror", None
                    ) not in {33, 36, 158}:
                        raise
                    time.sleep(0.05)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class TavilyCache:
    """Disk-backed query cache with bounded freshness and atomic writes."""

    def __init__(
        self,
        path: Path,
        max_entries: int = 2000,
        ttl_seconds: int = TAVILY_CACHE_TTL_SECONDS,
    ):
        self.path = path
        self.max_entries = max_entries
        self.ttl_seconds = max(0, int(ttl_seconds))
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("Tavily cache root must be an object")
            self._data = data
        except Exception as exc:
            logger.warning("Tavily cache unreadable (%s); starting fresh", exc)
            self._data = {}

    def get(self, key: str) -> Any:
        entry = self._data.get(key)
        if not isinstance(entry, dict) or "cached_at" not in entry:
            return None
        try:
            cached_at = float(entry["cached_at"])
        except (TypeError, ValueError):
            return None
        age = datetime.now(timezone.utc).timestamp() - cached_at
        if age < 0 or age >= self.ttl_seconds:
            return None
        return entry.get("results")

    def has_stale_entry(self, key: str) -> bool:
        return key in self._data and self.get(key) is None

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = {
                "cached_at": datetime.now(timezone.utc).timestamp(),
                "results": value,
            }
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
            logger.warning("could not persist Tavily cache: %s", exc)


class DailyBudget:
    """Tracks how many Tavily calls were spent *today* (UTC)."""

    def __init__(
        self,
        path: Path,
        limit: int,
        *,
        durable_reserver: Callable[[Path, str, int], dict] | None = None,
        durable_exhauster: Callable[[Path, str, int], dict] | None = None,
    ):
        self.path = path
        self.identity = str(path.resolve())
        self.limit = int(limit)
        self.durable_reserver = durable_reserver
        self.durable_exhauster = durable_exhauster
        self._lock = _budget_thread_lock(self.identity)
        self._date = ""
        self._count = 0
        self._exhausted = False
        self.last_reservation_error = ""
        self._ledger_error = False
        self._load()

    @staticmethod
    def _today(now: datetime | None = None) -> str:
        instant = now or datetime.now(timezone.utc)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return instant.astimezone(timezone.utc).date().isoformat()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if raw.get("date") == self._today():
                self._date = raw["date"]
                self._count = int(raw.get("count", 0))
                self._exhausted = bool(raw.get("exhausted", False))
        except Exception:
            self._date, self._count, self._exhausted = "", 0, False

    def _reload_for_date(self, today: str) -> None:
        if self._ledger_error and self._date == today:
            return
        self._date, self._count, self._exhausted = "", 0, False
        self._ledger_error = False
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError("budget ledger is not an object")
            if raw.get("date") == today:
                self._date = today
                self._count = max(0, int(raw.get("count", 0)))
                self._exhausted = bool(raw.get("exhausted", False))
        except (OSError, ValueError, TypeError, AttributeError):
            self._date = today
            self._exhausted = True
            self._ledger_error = True
            logger.error("could not read Tavily budget ledger at %s", self.path)

    def _save(self) -> bool:
        tmp = self.path.with_suffix(".json.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump({
                    "date": self._date,
                    "count": self._count,
                    "exhausted": self._exhausted,
                }, fh)
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            logger.error("could not persist Tavily budget at %s: %s", self.path, exc)
            return False

    def remaining(self, now: datetime | None = None) -> int:
        if self.limit <= 0:
            return 1 << 30
        today = self._today(now)
        with self._lock:
            try:
                with _budget_file_lock(self.path):
                    self._reload_for_date(today)
                    if self._ledger_error or self._exhausted:
                        return 0
                    return max(0, self.limit - self._count)
            except OSError as exc:
                self._date = today
                self._ledger_error = True
                self.last_reservation_error = "budget_ledger_unavailable"
                logger.error("could not lock Tavily budget ledger at %s: %s", self.path, exc)
                return 0

    def reserve(self, now: datetime | None = None) -> bool:
        """Persist one request reservation before dispatch, including failed calls."""
        self.last_reservation_error = ""
        if self.limit <= 0:
            return True
        today = self._today(now)
        with self._lock:
            if self.durable_reserver is not None:
                if self._ledger_error and self._date == today:
                    self.last_reservation_error = (
                        "provider_budget_persistence_unavailable"
                    )
                    return False
                try:
                    state = self.durable_reserver(self.path, today, self.limit)
                except Exception:
                    state = None
                if not isinstance(state, dict):
                    self.last_reservation_error = "provider_budget_persistence_unavailable"
                    self._date = today
                    self._ledger_error = True
                    return False
                if (
                    state.get("reserved") is not True
                    or state.get("date") != today
                    or not isinstance(state.get("count"), int)
                    or isinstance(state.get("count"), bool)
                    or not 1 <= state.get("count", 0) <= self.limit
                    or not isinstance(state.get("exhausted"), bool)
                    or state.get("exhausted") is not False
                ):
                    reason = state.get("reason")
                    self.last_reservation_error = (
                        reason if reason in {
                            "daily_request_budget_exhausted",
                            "provider_budget_persistence_unavailable",
                            "budget_ledger_unavailable",
                        }
                        else "provider_budget_persistence_unavailable"
                    )
                    self._date = today
                    count = state.get("count", 0)
                    self._count = (
                        count if isinstance(count, int) and not isinstance(count, bool)
                        and count >= 0 else 0
                    )
                    self._exhausted = state.get("exhausted") is True
                    self._ledger_error = (
                        self.last_reservation_error in {
                            "provider_budget_persistence_unavailable",
                            "budget_ledger_unavailable",
                        }
                    )
                    return False
                self._date = today
                self._count = state["count"]
                self._exhausted = state["exhausted"]
                self._ledger_error = False
                if not self._save():
                    self.last_reservation_error = "provider_budget_persistence_unavailable"
                    self._ledger_error = True
                    return False
                return True
            try:
                with _budget_file_lock(self.path):
                    self._reload_for_date(today)
                    if self._ledger_error:
                        self.last_reservation_error = "budget_ledger_unavailable"
                        return False
                    if self._exhausted or self._count >= self.limit:
                        self.last_reservation_error = "daily_request_budget_exhausted"
                        return False
                    self._date = today
                    self._count += 1
                    if not self._save():
                        self._count -= 1
                        self.last_reservation_error = "budget_ledger_unavailable"
                        self._ledger_error = True
                        return False
                    return True
            except OSError as exc:
                self._date = today
                self.last_reservation_error = "budget_ledger_unavailable"
                self._ledger_error = True
                logger.error("could not reserve Tavily request at %s: %s", self.path, exc)
                return False

    def record(self) -> None:
        """Compatibility alias for reserving an outbound request."""
        self.reserve()

    def exhaust(self) -> None:
        """Stop further calls after the provider confirms the key is quota-exhausted."""
        today = self._today()
        with self._lock:
            if self.durable_exhauster is not None:
                try:
                    state = self.durable_exhauster(self.path, today, self.limit)
                except Exception as exc:
                    self._ledger_error = True
                    self.last_reservation_error = (
                        "provider_budget_persistence_unavailable"
                    )
                    raise OSError(self.last_reservation_error) from exc
                if (
                    not isinstance(state, dict)
                    or state.get("date") != today
                    or not isinstance(state.get("count"), int)
                    or isinstance(state.get("count"), bool)
                    or state.get("exhausted") is not True
                ):
                    self._ledger_error = True
                    self.last_reservation_error = (
                        "provider_budget_persistence_unavailable"
                    )
                    raise OSError(self.last_reservation_error)
                self._date = today
                self._count = state["count"]
                self._exhausted = True
                if not self._save():
                    self._ledger_error = True
                    self.last_reservation_error = (
                        "provider_budget_persistence_unavailable"
                    )
                    raise OSError(self.last_reservation_error)
                return
            with _budget_file_lock(self.path):
                self._reload_for_date(today)
                self._date = today
                self._exhausted = True
                if not self._save():
                    self._ledger_error = True
                    raise OSError("could not persist Tavily quota exhaustion")


class ApiAttemptBudget:
    """Thread-safe aggregate cap shared by all Tavily connectors in one run."""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self._lock = threading.Lock()
        self._attempts = 0
        self._successes = 0
        self._failures = 0
        self._retries = 0

    def remaining(self) -> int:
        with self._lock:
            return max(0, self.limit - self._attempts)

    def reserve(self, daily_budget: DailyBudget, *, retry: bool = False) -> str:
        """Reserve both caps immediately before a provider request is dispatched."""
        with self._lock:
            if self._attempts >= self.limit:
                return "run_api_attempt_budget_exhausted"
            if not daily_budget.reserve():
                return (
                    daily_budget.last_reservation_error
                    or "daily_request_budget_exhausted"
                )
            self._attempts += 1
            if retry:
                self._retries += 1
            return ""

    def record_success(self) -> None:
        with self._lock:
            self._successes += 1

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1

    def as_dict(self) -> dict[str, int]:
        with self._lock:
            return {
                "limit": self.limit,
                "attempts": self._attempts,
                "successes": self._successes,
                "failed_attempts": self._failures,
                "retry_attempts": self._retries,
                "remaining": max(0, self.limit - self._attempts),
            }


@dataclass
class TavilyKey:
    """One Tavily API credential + its own daily spending budget."""
    api_key: str
    budget: DailyBudget
    fp: str


class ResilientTavily:
    """Wrapper: cache -> (budget-gated) rotating API keys -> empty fallback.

    `keys` is a ring of API keys. Each call tries the keys in order, skipping
    any whose daily budget is spent; a quota error permanently exhausts that
    key's budget for the day so the ring fast-forwards to the next key.
    Only when every key is spent or failing does it return empty results.
    """

    name = "tavily_resilient"

    def __init__(
        self,
        keys: list[TavilyKey] | None = None,
        cache: TavilyCache | None = None,
        results_per_query: int = 10,
        official_only: bool = False,
        domains: list[str] | None = None,
        source_name: str = "tavily",
        result_source_type: str = "search_engine",
        query_suffix: str = "job",
        max_key_attempts: int | None = None,
        attempt_budget: ApiAttemptBudget | None = None,
    ):
        if keys:
            unique_keys: dict[str, TavilyKey] = {}
            for key in keys:
                unique_keys.setdefault(key_fingerprint(key.api_key, 64), key)
            self.keys = list(unique_keys.values())
        else:
            # Single key fallback
            api_key = os.getenv("TAVILY_API_KEY", "")
            if not api_key:
                raise TypeError("provide keys=[] or set TAVILY_API_KEY")
            budget_path = Path("data/tavily_budget.json")
            budget_path.parent.mkdir(parents=True, exist_ok=True)
            budget = DailyBudget(budget_path, limit=TAVILY_DAILY_REQUEST_LIMIT)
            self.keys = [TavilyKey(
                api_key=api_key,
                budget=budget,
                fp=key_fingerprint(api_key),
            )]

        self.cache = cache or TavilyCache(Path("data/tavily_cache.json"))
        self.results_per_query = results_per_query
        self.official_only = official_only
        self.domains = [domain.lower().lstrip(".") for domain in (domains or [])]
        self.source_name = source_name
        self.result_source_type = result_source_type
        self.query_suffix = query_suffix
        self.max_key_attempts = max_key_attempts
        self.attempt_budget = attempt_budget or ApiAttemptBudget(GENERAL_TAVILY_DAILY_LIMIT)
        self.name = f"tavily_resilient:{self.keys[0].fp}"
        self.last_status = "ready"
        self.last_origin = "none"
        self.last_error_code = ""
        self.last_deferred_reason = ""
        self.last_call_attempts = 0
        self._attempt_stats = {
            "attempts": 0,
            "successes": 0,
            "failed_attempts": 0,
            "retry_attempts": 0,
        }

    def attempt_metrics(self) -> dict[str, int]:
        return dict(self._attempt_stats)

    def has_cached_result(self, query: str, location: str = "") -> bool:
        q = self._build_query(query, location)
        return self.cache.get(_cache_key(q, location)) is not None

    def has_stale_cached_result(self, query: str, location: str = "") -> bool:
        q = self._build_query(query, location)
        return self.cache.has_stale_entry(_cache_key(q, location))

    def _build_query(self, query: str, location: str) -> str:
        q = f"{query} {location} {self.query_suffix}".strip()
        if self.official_only:
            q += " (site:boards.greenhouse.io OR site:jobs.lever.co OR site:myworkdayjobs.com OR site:jobs.smartrecruiters.com OR site:icims.com OR site:jobs.ashbyhq.com OR site:recruitee.com)"
        elif self.domains:
            q += " (" + " OR ".join(f"site:{domain}" for domain in self.domains) + ")"
        return q

    def _allowed(self, href: str, *, ats: str = "") -> bool:
        from urllib.parse import urlparse
        if not href.startswith("http"):
            return False
        host = urlparse(href).netloc.lower()
        in_domain_scope = not self.domains or any(host == d or host.endswith("." + d) for d in self.domains)
        BLOCKED_DOMAINS = ["linkedin.com", "facebook.com", "instagram.com"]
        return (not any(b in host for b in BLOCKED_DOMAINS) and in_domain_scope and
                (not self.official_only or bool(ats)))

    def _company_from_ats_url(self, url: str) -> str:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if parts:
            return parts[0].replace("-", " ").replace("_", " ").title()
        host = parsed.netloc.lower().split(":")[0]
        return host.split(".")[0].replace("-", " ").title()

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        self.last_origin = "none"
        self.last_error_code = ""
        self.last_deferred_reason = ""
        self.last_call_attempts = 0
        q = self._build_query(query, location)

        cache_key = _cache_key(q, location)
        cached = self.cache.get(cache_key)
        if cached is not None:
            self.last_origin = "cache"
            self.last_status = "ok" if cached else "empty"
            # Convert cached dicts back to Opportunity objects
            return [Opportunity(**d) for d in cached]

        keys = self.keys
        saw_quota_error = False
        saw_request_error = False
        saw_daily_budget = False
        saw_run_budget = False
        saw_ledger_error = False
        saw_persistence_error = False
        attempts_this_query = 0
        for key in keys:
            if (
                self.max_key_attempts is not None
                and attempts_this_query >= max(0, self.max_key_attempts)
            ):
                continue

            reservation_error = self.attempt_budget.reserve(
                key.budget, retry=attempts_this_query > 0,
            )
            if reservation_error == "run_api_attempt_budget_exhausted":
                saw_run_budget = True
                self.last_deferred_reason = reservation_error
                break
            if reservation_error == "daily_request_budget_exhausted":
                saw_daily_budget = True
                self.last_deferred_reason = reservation_error
                continue
            if reservation_error == "budget_ledger_unavailable":
                saw_ledger_error = True
                continue
            if reservation_error == "provider_budget_persistence_unavailable":
                saw_ledger_error = True
                saw_persistence_error = True
                continue
            attempts_this_query += 1
            self.last_call_attempts += 1
            self._attempt_stats["attempts"] += 1
            if attempts_this_query > 1:
                self._attempt_stats["retry_attempts"] += 1
            try:
                resp = requests.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": key.api_key,
                        "query": q,
                        "search_depth": os.getenv("TAVILY_SEARCH_DEPTH", "basic"),
                        "include_answer": False,
                        "include_raw_content": False,
                        "max_results": self.results_per_query,
                    },
                    timeout=30,
                )

                if resp.status_code == 429 or resp.status_code == 432:
                    raise requests.HTTPError(f"{resp.status_code} Client Error", response=resp)

                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                    raise ValueError("Tavily response is missing a results list")

                # Debug: persist a compact raw response for forensic analysis when enabled
                if os.getenv('TAVILY_DEBUG', '').lower() in ('1', 'true', 'yes'):
                    try:
                        dbg = {
                            'ts': datetime.now(timezone.utc).isoformat(),
                            'query_hash': hashlib.sha256(q.encode("utf-8")).hexdigest(),
                            'key_fp': key.fp,
                            'status_code': getattr(resp, 'status_code', None),
                            'results_len': len(data["results"]),
                        }
                        import json as _json
                        with open('/tmp/tavily_debug.json', 'a', encoding='utf-8') as _fh:
                            _json.dump(dbg, _fh, ensure_ascii=False)
                            _fh.write('\n')
                    except Exception as _dbg_exc:
                        logger.warning('Failed to write tavily debug: %s', _dbg_exc)

                results = data.get("results", [])

                out: list[Opportunity] = []
                from app.connectors.ats_detect import detect_ats
                from app.connectors.base import infer_country

                for i, result in enumerate(results):
                    if i >= self.results_per_query:
                        break
                    href = result.get("url", "")
                    if not href:
                        continue
                    title = result.get("title", "").strip()
                    if not title:
                        continue
                    ats = detect_ats(href)
                    if not self._allowed(href, ats=ats):
                        continue
                    out.append(Opportunity(
                        source=self.source_name,
                        source_type="ats" if ats else self.result_source_type,
                        external_id=href,
                        title=title[:200],
                        company=self._company_from_ats_url(href) if ats else "",
                        location=location,
                        country=infer_country(location),
                        description=result.get("content", "")[:3000],
                        url=href,
                        posted_at=None,
                        employment_type="",
                        raw={"query": q},
                    ))

                # An empty, successful response is a valid negative result.
                # Cache it and charge the one request so key rotation cannot
                # turn an empty query into one request per credential.
                self.attempt_budget.record_success()
                self._attempt_stats["successes"] += 1
                self.cache.put(cache_key, [o.to_dict() for o in out])
                if out:
                    logger.info("Tavily key %s success, %d results", key.fp, len(out))
                else:
                    logger.info("Tavily key %s returned empty results", key.fp)
                self.last_origin = "api"
                self.last_status = "ok" if out else "empty"
                return out

            except Exception as exc:
                self.attempt_budget.record_failure()
                self._attempt_stats["failed_attempts"] += 1
                logger.warning("Tavily key %s failed: %s -> trying next key", key.fp, str(exc)[:200])
                if _is_quota_error(exc):
                    saw_quota_error = True
                    try:
                        key.budget.exhaust()
                    except OSError:
                        saw_persistence_error = True
                        self.last_error_code = (
                            "provider_budget_persistence_unavailable"
                        )
                        self.last_status = "degraded"
                        return []
                else:
                    saw_request_error = True

        if saw_quota_error and not saw_request_error:
            self.last_error_code = "provider_quota_exhausted"
        elif saw_request_error:
            self.last_error_code = "provider_request_failed"
        elif saw_quota_error:
            self.last_error_code = "provider_quota_exhausted"
        elif saw_run_budget:
            self.last_error_code = "run_api_attempt_budget_exhausted"
        elif saw_daily_budget:
            self.last_error_code = "daily_request_budget_exhausted"
        elif saw_ledger_error:
            self.last_error_code = (
                "provider_budget_persistence_unavailable"
                if saw_persistence_error else "budget_ledger_unavailable"
            )
        else:
            self.last_error_code = "daily_request_budget_exhausted"
        self.last_status = "degraded"
        logger.info("all Tavily keys spent/failed -> returning empty")
        return []
