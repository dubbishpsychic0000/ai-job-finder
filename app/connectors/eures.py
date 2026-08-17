"""EURES — European Employment Services connector (spec §2, §14).

EURES is the official EU network of public employment services. This adapter
consumes EURES/public-employment RSS feeds (public, no credentials). When a
country has no feed configured it simply returns nothing — the connector is
config-driven so new official feeds can be added without code changes.
"""
from __future__ import annotations

import logging
from typing import Any

import feedparser
import requests

from app.connectors.base import Opportunity
from app.connectors.rss import RSSJobSource

logger = logging.getLogger(__name__)
USER_AGENT = "WorldwideCareerAgent/0.1 (official EURES public feeds)"


class EuresSource(RSSJobSource):
    """Government employment feed adapter (defaults to EURES-style feeds)."""

    name = "eures"
    kind = "rss"
    source_type = "government"
    access_mode = "public"
    policy_notice = "Consumes official EURES / public-employment-service RSS feeds only."

    def __init__(self, feeds: list[str] | None = None, countries: list[str] | None = None):
        super().__init__(feeds=feeds or [])
        self.countries = countries or []

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        out: list[Opportunity] = []
        if not self.feeds:
            return out
        for feed in self.feeds:
            url = feed.format(query=query.replace(" ", "+"), location=location.replace(" ", "+"))
            try:
                resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
                resp.raise_for_status()
                parsed = feedparser.parse(resp.content)
            except Exception as exc:  # one bad feed must not kill discovery
                logger.warning("EURES feed %s failed: %s", url, exc)
                continue
            for entry in parsed.entries:
                opp = self._from_entry(entry, url, query=query)
                opp.source = "eures"
                opp.source_type = "government"
                opp.language = _language_for(opp)
                if opp.title and opp.url and _title_contains(opp.title, query):
                    out.append(opp)
        return out


def _language_for(opp: Any) -> str:
    text = f"{opp.title} {opp.location}".lower()
    if any(w in text for w in ("ingenieur", "technicien", "travaux", "voirie", "vrds")):
        return "fr"
    if any(w in text for w in ("bau", "techniker", "straße", "strasse")):
        return "de"
    return ""


def _title_contains(title: str, query: str) -> bool:
    words = [w for w in query.lower().split() if len(w) > 3]
    if not words:
        return True
    tl = title.lower()
    return any(w in tl for w in words) or words[0] in tl
