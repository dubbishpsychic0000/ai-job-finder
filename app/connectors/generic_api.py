"""Generic config-driven JSON API connector (spec §2).

Lets an operator add a new job API to discovery purely via sources.yaml — no
code. `config` supports:

    base_url:      URL template; {query} / {location} placeholders
    headers:       extra HTTP headers
    list_path:     dotted path to the array of items (e.g. "jobs" or "data.items")
    mapping:       item field -> Opportunity field
        title, company, location, country, description, url,
        external_id, posted_at, employment_type, salary

Hermetic testing is done through `data_path` (a local JSON file shaped like the
API response).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

from app.connectors.base import Opportunity, infer_country, parse_date

logger = logging.getLogger(__name__)
USER_AGENT = "WorldwideCareerAgent/0.1 (public API; respects ToS)"

DEFAULT_MAPPING = {
    "title": "title",
    "company": "company",
    "location": "location",
    "description": "description",
    "url": "url",
    "external_id": "id",
    "posted_at": "posted_at",
}


def _get_path(data: Any, path: str) -> Any:
    """Resolve a dotted path like 'data.items' into a value (None if missing)."""
    node: Any = data
    for part in (path or "").split("."):
        if not part:
            return None
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node


class GenericAPISource:
    """Config-driven REST/JSON job API adapter."""

    name = "generic_api"
    kind = "api"
    source_type = "job_board"
    access_mode = "public"
    policy_notice = "Public JSON API configured in sources.yaml; must comply with the provider's terms."

    def __init__(self, config: dict[str, Any] | None = None, data_path: str | Path | None = None):
        self.config = config or {}
        self.data_path = Path(data_path) if data_path else None

    def _fetch(self, query: str, location: str) -> dict | list:
        if self.data_path:
            return json.loads(self.data_path.read_text(encoding="utf-8"))
        url = (self.config.get("base_url") or "").format(
            query=query.replace(" ", "+"), location=location.replace(" ", "+"))
        headers = {"User-Agent": USER_AGENT, **self.config.get("headers", {})}
        resp = requests.get(url, headers=headers, params=self.config.get("params") or {}, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def _item(self, raw: dict[str, Any], mapping: dict[str, str]) -> Opportunity:
        company = _get_path(raw, mapping.get("company", "")) or ""
        location = _get_path(raw, mapping.get("location", "")) or ""
        url = _get_path(raw, mapping.get("url", "")) or ""
        return Opportunity(
            source=self.config.get("name", "generic_api"),
            source_type=self.config.get("source_type", "job_board"),
            external_id=str(_get_path(raw, mapping.get("external_id", "id")) or url or raw.get("id", "")),
            title=_get_path(raw, mapping.get("title", "title")) or "",
            company=company,
            location=location,
            country=infer_country(location) or _get_path(raw, mapping.get("country", "")) or "",
            description=_get_path(raw, mapping.get("description", "")) or "",
            url=url,
            posted_at=parse_date(_get_path(raw, mapping.get("posted_at", "")) or None),
            employment_type=(_get_path(raw, mapping.get("employment_type", "")) or "full_time"),
            salary=_get_path(raw, mapping.get("salary", "")),
            raw={"config": self.config.get("name", ""), "query_note": "generic_api"},
        )

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        mapping = {**DEFAULT_MAPPING, **self.config.get("mapping", {})}
        try:
            payload = self._fetch(query, location)
        except Exception as exc:
            logger.warning("Generic API %s failed: %s", self.config.get("name"), exc)
            return []
        items = _get_path(payload, self.config.get("list_path", ""))
        if items is None:
            items = payload if isinstance(payload, list) else []
        if not isinstance(items, list):
            items = []
        out = [self._item(item, mapping) for item in items if isinstance(item, dict)]
        # keep only items that match the query on title (when query non-empty)
        words = [w for w in query.lower().split() if len(w) > 3]
        if words:
            out = [o for o in out if any(w in o.title.lower() for w in words)]
        return out
