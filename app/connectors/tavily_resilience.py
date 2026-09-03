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

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from app.connectors.base import Opportunity

logger = logging.getLogger(__name__)


def _cache_key(query: str, location: str) -> str:
    payload = f"tavily\x00{query}\x00{location}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _is_quota_error(exc: Exception) -> bool:
    """True for Tavily quota/rate-limit errors (429, 432)."""
    message = str(exc).lower()
    return any(token in message for token in ("429", "432", "quota", "rate-limit", "daily limit", "too many requests"))


def key_fingerprint(api_key: str, length: int = 8) -> str:
    """Short, non-reversible id for a credential — safe for logs/file names."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:length]


class TavilyCache:
    """Disk-backed query -> response cache with atomic writes."""

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
        except Exception as exc:
            logger.warning("Tavily cache unreadable (%s); starting fresh", exc)
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
            logger.warning("could not persist Tavily cache: %s", exc)


class DailyBudget:
    """Tracks how many Tavily calls were spent *today* (UTC)."""

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
            logger.warning("could not persist Tavily budget: %s", exc)

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
    ):
        if keys:
            self.keys = list(keys)
        else:
            # Single key fallback
            api_key = os.getenv("TAVILY_API_KEY", "")
            if not api_key:
                raise TypeError("provide keys=[] or set TAVILY_API_KEY")
            budget_path = Path("data/tavily_budget.json")
            budget_path.parent.mkdir(parents=True, exist_ok=True)
            budget = DailyBudget(budget_path, limit=20)
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
        self.name = f"tavily_resilient:{self.keys[0].fp}"

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
        q = f"{query} {location} job".strip()
        if self.official_only:
            q += " (site:boards.greenhouse.io OR site:jobs.lever.co OR site:myworkdayjobs.com OR site:jobs.smartrecruiters.com OR site:icims.com OR site:jobs.ashbyhq.com OR site:recruitee.com)"
        elif self.domains:
            q += " (" + " OR ".join(f"site:{domain}" for domain in self.domains) + ")"

        cache_key = _cache_key(q, location)
        cached = self.cache.get(cache_key)
        if cached is not None:
            # Convert cached dicts back to Opportunity objects
            return [Opportunity(**d) for d in cached]

        for key in self.keys:
            if key.budget.remaining() <= 0:
                logger.debug("Tavily key %s budget exhausted, skipping", key.fp)
                continue

            try:
                resp = requests.post(
                    "https://api.tavily.com/search",
                    json={
                        "api_key": key.api_key,
                        "query": q,
                        "search_depth": "advanced",
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

                # Debug: persist a compact raw response for forensic analysis when enabled
                if os.getenv('TAVILY_DEBUG', '').lower() in ('1', 'true', 'yes'):
                    try:
                        results_preview = data.get('results', [])[:3] if isinstance(data, dict) else []
                        dbg = {
                            'ts': datetime.now(timezone.utc).isoformat(),
                            'query': q,
                            'key_fp': key.fp,
                            'status_code': getattr(resp, 'status_code', None),
                            'results_len': len(data.get('results', [])) if isinstance(data, dict) and data.get('results') else 0,
                            'sample': results_preview,
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

                if out:
                    key.budget.record()
                    # Convert Opportunity objects to dicts for JSON serialization
                    self.cache.put(cache_key, [o.to_dict() for o in out])
                    logger.info("Tavily key %s success, %d results", key.fp, len(out))
                    return out
                else:
                    logger.warning("Tavily key %s returned empty results", key.fp)

            except Exception as exc:
                logger.warning("Tavily key %s failed: %s -> trying next key", key.fp, str(exc)[:200])
                if _is_quota_error(exc):
                    key.budget.exhaust()

        logger.info("all Tavily keys spent/failed -> returning empty")
        return []