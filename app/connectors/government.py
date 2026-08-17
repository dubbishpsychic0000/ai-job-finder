"""Government & public employment portal connector (spec §2, §14).

Adapters for official government employment services, public-sector job
portals, national employment agencies and foreign-worker program sites.

Two data modes driven by `config.type`:

    - rss: public RSS/Atom feeds (USAJobs, UK Find a Job, EURES national feeds…)
    - api: public JSON APIs (Austria AMS jobapi.gv.at, UWV Netherlands…)

`access_mode` is pinned to `public` (spec §34) — a source configured as
`authorized_only` / `user_provided` is rejected at startup so an operator can
never silently scrape a gated portal. `data_path` makes tests hermetic: a
`.xml/.rss/.atom` file is parsed as a feed, a `.json` file as an API payload.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import feedparser
import requests

from app.connectors.base import Opportunity, infer_country, parse_date
from app.connectors.generic_api import _get_path

logger = logging.getLogger(__name__)
USER_AGENT = "WorldwideCareerAgent/0.1 (official public employment portals)"
DEFAULT_MAPPING = {
    "title": "title",
    "company": "company",
    "location": "location",
    "description": "description",
    "url": "url",
    "external_id": "id",
    "posted_at": "posted_at",
}


class GovernmentPortalSource:
    """Official government / public-employment job source (public access only)."""

    name = "government"
    kind = "rss"
    source_type = "government"
    access_mode = "public"
    policy_notice = ("Official government / public-employment portals with public "
                     "RSS feeds or JSON APIs only; source must declare access_mode: public (spec §34).")

    def __init__(self, config: dict[str, Any] | None = None, *, data_path=None, feeds: list[str] | None = None):
        self.config = config or {}
        if self.config.get("access_mode", "public") != "public":
            raise ValueError(
                "GovernmentPortalSource requires access_mode 'public' (spec §34); "
                f"got {self.config.get('access_mode')!r}")
        self.data_path = Path(data_path) if data_path else None
        self.feeds = feeds or self.config.get("feeds") or []
        self.type = self.config.get("type", "rss")
        self.kind = self.type
        self.country = self.config.get("country", "")
        self.language = self.config.get("language", "")
        self.list_path = self.config.get("list_path", "")
        self.mapping = {**DEFAULT_MAPPING, **self.config.get("mapping", {})}
        self.base_url = self.config.get("base_url", "")

    # ---- fetch ----------------------------------------------------------------

    def _fetch(self, query: str, location: str) -> Any:
        if self.data_path:
            text = self.data_path.read_text(encoding="utf-8")
            if self.data_path.suffix.lower() in (".xml", ".rss", ".atom"):
                return feedparser.parse(text).entries
            return json.loads(text)
        if self.type == "api":
            url = self.base_url.format(query=query.replace(" ", "+"),
                                       location=location.replace(" ", "+"))
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
            resp.raise_for_status()
            return resp.json()
        out: list[dict[str, Any]] = []
        for feed in self.feeds:
            url = feed.format(query=query.replace(" ", "+"), location=location.replace(" ", "+"))
            try:
                resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
                resp.raise_for_status()
                out.extend(feedparser.parse(resp.content).entries)
            except Exception as exc:
                logger.warning("government feed %s failed: %s", url, exc)
        return out

    # ---- normalization ---------------------------------------------------------

    def _from_entry(self, entry: Any, source_name: str) -> Opportunity:
        from app.connectors.rss import _clean_html

        title = _clean_html(getattr(entry, "title", "") or "")
        link = getattr(entry, "link", "") or ""
        summary = _clean_html(getattr(entry, "summary", "") or getattr(entry, "description", "") or "")
        published = getattr(entry, "published", "") or getattr(entry, "updated", "")

        company = ""
        location = self.country
        parts = [p.strip() for p in str(title).split(" - ")]
        if len(parts) >= 2:
            title, company = parts[0], parts[1]
        return Opportunity(
            source=source_name,
            source_type="government",
            external_id=getattr(entry, "id", "") or link,
            title=title,
            company=company,
            location=location,
            country=infer_country(location) or self.country,
            description=summary[:20000],
            url=link,
            posted_at=parse_date(published),
            employment_type="full_time",
            language=self.language,
            raw={"portal": source_name, "kind": "rss"},
        )

    def _from_item(self, raw: dict[str, Any], source_name: str) -> Opportunity:
        company = _get_path(raw, self.mapping.get("company", "")) or ""
        location = _get_path(raw, self.mapping.get("location", "")) or ""
        url = _get_path(raw, self.mapping.get("url", "")) or ""
        title = _get_path(raw, self.mapping.get("title", "")) or ""
        return Opportunity(
            source=source_name,
            source_type="government",
            external_id=str(_get_path(raw, self.mapping.get("external_id", "id")) or url or ""),
            title=title,
            company=company,
            location=location,
            country=infer_country(location) or self.country,
            description=_get_path(raw, self.mapping.get("description", "")) or "",
            url=url,
            posted_at=parse_date(_get_path(raw, self.mapping.get("posted_at", "")) or None),
            employment_type="full_time",
            language=self.language,
            raw={"portal": source_name, "kind": "api"},
        )

    # ---- protocol ---------------------------------------------------------------

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        if self.type == "api" and not self.base_url and not self.data_path:
            return []
        if self.type == "rss" and not self.feeds and not self.data_path:
            return []
        source_name = self.config.get("name", self.name)
        try:
            payload = self._fetch(query, location)
        except Exception as exc:
            logger.warning("government portal %s failed: %s", source_name, exc)
            return []
        if self.type == "api":
            items = _get_path(payload, self.list_path) if self.list_path else payload
            if not isinstance(items, list):
                items = [payload] if isinstance(payload, dict) else []
            out = [self._from_item(i, source_name) for i in items if isinstance(i, dict)]
        else:
            out = [self._from_entry(e, source_name) for e in payload]
        words = [w for w in query.lower().split() if len(w) > 3]
        if words:
            out = [o for o in out if any(w in o.title.lower() for w in words)]
        return out
