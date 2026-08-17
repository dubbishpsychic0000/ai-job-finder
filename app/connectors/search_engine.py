"""Search-engine connector (Priority 1+ for web-discovery).

Uses DuckDuckGo's HTML endpoint (no API key) to find job listings across the
web. Results are blessed by a domain allowlist/denylist so the agent prefers
high-signal job sites and avoids scraping aggregators that forbid it.

Fallbacks/degradation:
  * network errors -> empty result + warning (never crashes discovery)
"""
from __future__ import annotations

import logging
import re
import urllib.parse

import requests
from bs4 import BeautifulSoup

from app.connectors.base import Opportunity, infer_country

logger = logging.getLogger(__name__)

PREFERRED_DOMAINS = [
    "indeed.com", "linkedin.com", "glassdoor.com", "monster.com", "careerbuilder.com",
    "totaljobs.com", "stepstone.de", "welcometothejungle.com", "eures.europa.eu",
    "usajobs.gov", "emploipublic.fr", "prairieregion", "jobberwocky.com",
]

BLOCKED_DOMAINS = []  # extend with scrapers/aggregators that forbid access


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


class SearchEngineSource:
    """Web search connector built on DuckDuckGo's HTML result page."""

    name = "search_engine"
    kind = "html_search"
    source_type = "search_engine"
    access_mode = "public"
    policy_notice = ("Uses a public search-engine result page. Does not log in, bypass "
                     "captchas, or target protected sites.")

    def __init__(self, results_per_query: int = 8):
        self.results_per_query = results_per_query

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        q = f"{query} {location} job".strip()
        params = {"q": q, "kl": "us-en", "ia": "web"}
        url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode(params)
        out: list[Opportunity] = []
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("Search engine query failed: %s", exc)
            return out

        soup = BeautifulSoup(resp.text, "lxml")
        for i, result in enumerate(soup.select(".result")):
            if i >= self.results_per_query:
                break
            a = result.select_one(".result__a")
            snippet = result.select_one(".result__snippet")
            if not a:
                continue
            href = a.get("href", "")
            title = _clean(a.get_text(" ")).strip()
            if not title:
                continue
            if not self._allowed(href):
                continue
            out.append(Opportunity(
                source="search_engine",
                external_id=href,
                title=title[:200],
                company="",
                location=location,
                country=infer_country(location),
                description=_clean(snippet.get_text(" ")) if snippet else "",
                url=href,
                posted_at=None,
                employment_type="",
                raw={"query": q},
            ))
        return out

    @staticmethod
    def _allowed(href: str) -> bool:
        if not href.startswith("http"):
            return False
        host = urllib.parse.urlparse(href).netloc.lower()
        return not any(b in host for b in BLOCKED_DOMAINS)
