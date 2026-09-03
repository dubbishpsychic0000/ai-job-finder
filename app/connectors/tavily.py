"""Tavily search connector (Priority 1+ for web-discovery).

Uses Tavily API to find job listings across the web. Results are filtered
by domain allowlist/denylist so the agent prefers high-signal job sites.

Fallbacks/degradation:
  * network errors -> empty result + warning (never crashes discovery)
"""
from __future__ import annotations

import os
import logging
import urllib.parse
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from app.connectors.ats_detect import detect_ats
from app.connectors.base import Opportunity, infer_country
from app.tavily_rotation import get_tavily_key

# Load .env so os.getenv() works for API keys
load_dotenv()

logger = logging.getLogger(__name__)

PREFERRED_DOMAINS = [
    "indeed.com", "linkedin.com", "glassdoor.com", "monster.com", "careerbuilder.com",
    "totaljobs.com", "stepstone.de", "welcometothejungle.com", "eures.europa.eu",
    "usajobs.gov", "emploipublic.fr", "jobberwocky.com",
]

BLOCKED_DOMAINS = ["linkedin.com", "facebook.com", "instagram.com"]


class TavilySource:
    """Web search connector built on Tavily API."""

    name = "tavily"
    kind = "api_search"
    source_type = "search_engine"
    access_mode = "public"
    policy_notice = ("Uses Tavily API for job search. Respects robots.txt and terms of service.")

    def __init__(self, results_per_query: int = 10, official_only: bool = False,
                 domains: list[str] | None = None, source_name: str = "tavily",
                 result_source_type: str = "search_engine", api_key: str | None = None):
        self.results_per_query = results_per_query
        self.official_only = official_only
        self.domains = [domain.lower().lstrip(".") for domain in (domains or [])]
        self.source_name = source_name
        self.result_source_type = result_source_type
        self._api_key = api_key  # Can be None, resolved at search time

    def _get_api_key(self) -> str:
        return self._api_key or get_tavily_key() or os.getenv("TAVILY_API_KEY", "")

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        q = f"{query} {location} job".strip()
        if self.official_only:
            q += " (site:boards.greenhouse.io OR site:jobs.lever.co OR site:myworkdayjobs.com OR site:jobs.smartrecruiters.com OR site:icims.com OR site:jobs.ashbyhq.com OR site:recruitee.com)"
        elif self.domains:
            q += " (" + " OR ".join(f"site:{domain}" for domain in self.domains) + ")"

        out: list[Opportunity] = []
        api_key = self._get_api_key()
        if not api_key:
            logger.warning("Tavily API key not configured")
            return out

        try:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": api_key,
                    "query": q,
                    "search_depth": "advanced",
                    "include_answer": False,
                    "include_raw_content": False,
                    "max_results": self.results_per_query,
                },
                timeout=30,
            )
            # Optional verbose debug: print raw response when TAVILY_DEBUG is set
            try:
                resp.raise_for_status()
            except Exception:
                logger.warning("Tavily HTTP error: status=%s text=%s", getattr(resp, 'status_code', None), getattr(resp, 'text', None))
                raise

            # Parse JSON safely
            try:
                data = resp.json()
            except Exception as exc_json:
                # If debug enabled, log raw body for investigation
                if os.getenv('TAVILY_DEBUG', '').lower() in ('1', 'true', 'yes'):
                    logger.warning("Tavily returned non-JSON response: %s", getattr(resp, 'text', '')[:2000])
                raise exc_json

            # If debug enabled, log summary of response
            if os.getenv('TAVILY_DEBUG', '').lower() in ('1', 'true', 'yes'):
                logger.info("Tavily response status=%s, keys=%s", resp.status_code, list(data.keys()) if isinstance(data, dict) else type(data))
        except Exception as exc:
            logger.warning("Tavily search query failed: %s", exc)
            return out

        results = data.get("results", [])
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
            if not self._allowed(href, official_only=self.official_only, ats=ats, domains=self.domains):
                continue

            out.append(Opportunity(
                source=self.source_name,
                source_type="ats" if ats else self.result_source_type,
                external_id=href,
                title=title[:200],
                company=_company_from_ats_url(href) if ats else "",
                location=location,
                country=infer_country(location),
                description=result.get("content", "")[:3000],
                url=href,
                posted_at=None,
                employment_type="",
                raw={"query": q},
            ))
        return out

    @staticmethod
    def _allowed(href: str, *, official_only: bool = False, ats: str = "",
                 domains: list[str] | None = None) -> bool:
        if not href.startswith("http"):
            return False
        host = urllib.parse.urlparse(href).netloc.lower()
        in_domain_scope = not domains or any(host == d or host.endswith("." + d) for d in domains)
        return (not any(b in host for b in BLOCKED_DOMAINS) and in_domain_scope and
                (not official_only or bool(ats)))


def _company_from_ats_url(url: str) -> str:
    """Best-effort public ATS tenant name; never guesses a contact."""
    parsed = urllib.parse.urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parts:
        return parts[0].replace("-", " ").replace("_", " ").title()
    host = parsed.netloc.lower().split(":")[0]
    return host.split(".")[0].replace("-", " ").title()