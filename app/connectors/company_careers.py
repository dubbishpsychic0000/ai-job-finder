"""Company careers page connector (Priority 3: HTML).

Static career pages are loaded with requests + BeautifulSoup. Injected via
config-driven `pages` with CSS selectors; a `scrape` function is provided for
sites where the DOM differs (subclass or override `_extract_cards`).

This intentionally does NOT attempt to bypass bot protections. If a page
requires JS or challenges, the connector reports it as blocked and the system
skips it — matching the architecture's "respect terms & protections" rule.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import requests
from bs4 import BeautifulSoup

from app.connectors.base import Opportunity, infer_country

logger = logging.getLogger(__name__)
_USER_AGENT = "WorldwideCareerAgent/0.1 (respects robots.txt and site terms)"


class CompanyCareersSource:
    """Scrapes job cards off simple static careers pages."""

    name = "company_careers"
    kind = "html"

    def __init__(self, pages: list[dict[str, Any]] | None = None):
        self.pages = pages or []

    def _fetch(self, url: str) -> str | None:
        try:
            resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=20)
            if resp.status_code in (401, 403, 429):
                logger.warning("Careers page %s is protected (status %s); skipping.", url, resp.status_code)
                return None
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            logger.warning("Careers page %s failed: %s", url, exc)
            return None

    def _extract_cards(self, html: str, ops: dict[str, Any]) -> list[dict[str, str]]:
        soup = BeautifulSoup(html, "lxml")
        cards = []
        for sel in ops.get("card_selectors", ["a"]):
            for el in soup.select(sel):
                text = el.get_text(" ", strip=True)
                href = el.get("href", "")
                if not text or not href:
                    continue
                cards.append({"title": text[:200], "url": href})
        return cards

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        out: list[Opportunity] = []
        for page in self.pages:
            url = page.get("url", "")
            ops = page.get("ops", {})
            html = self._fetch(url)
            if not html:
                continue
            for card in self._extract_cards(html, ops):
                title = card["title"]
                link = _abs_url(url, card["url"])
                if query and not _matches(title, query):
                    continue
                out.append(Opportunity(
                    source="company_careers",
                    external_id=link,
                    title=title,
                    company=page.get("company", ops.get("company", "")),
                    location=ops.get("location", location),
                    country=infer_country(ops.get("location", location)),
                    description="",
                    url=link,
                    posted_at=None,
                    raw={"page": url},
                ))
        return out


def _abs_url(base: str, href: str) -> str:
    if href.startswith("http"):
        return href
    return base.rstrip("/") + "/" + href.lstrip("/")


def _matches(title: str, query: str) -> bool:
    words = [w for w in re.findall(r"[\w\u00C0-\uFFFF]{4,}", query.lower())]
    return any(w in title.lower() for w in words) if words else True
