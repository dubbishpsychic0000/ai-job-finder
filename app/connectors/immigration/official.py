"""Immigration connectors — fetch official government content behind a domain whitelist.

`verify(url) -> Page` only accepts URLs whose host ends with one of the
approved suffixes below. Anything else is rejected so a blog post can never be
recorded as immigration law. The returned page keeps the full URL + fetch-time
for the evidence field.

If a government URL is unreachable, the connector returns an error page rather
than a hallucinated summary — the agent must report "unverified".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

# Whitelist of official immigration/visa domains. Extend deliberately.
OFFICIAL_DOMAINS = (
    ".gouv.fr",            # France
    ".service-public.fr",
    ".fi.de", ".arbeitundleben.de", ".make-it-in-germany.com",  # Germany (official gov portal)
    ".canada.ca", ".cic.gc.ca",      # Canada
    ".gc.ca",
    ".belgium.be", ".europa.eu",     # Belgium / EU
    ".gov.au",                       # Australia
    ".gov.uk",                       # UK (despite political name drift)
    ".usa.gov", ".uscis.gov", ".travel.state.gov",  # USA
    ".gov.nl",                       # Netherlands
    ".nzqa.govt.nz", ".govt.nz",
    ".mjusticia.gob.es",             # Spain
    ".gov.pt",                       # Portugal
    ".gouv.it",
    ".ch",                           # Switzerland (.admin.ch)
    ".admin.ch",
)

USER_AGENT = "WorldwideCareerAgent/0.1 (reads official pages only)"


@dataclass
class OfficialPage:
    url: str
    host: str
    fetched_at: datetime
    text: str
    title: str = ""
    ok: bool = True
    error: str = field(default="")

    def claim(self, claim_text: str) -> dict:
        """Build the evidence object stored with every immigration conclusion."""
        return {
            "claim": claim_text,
            "source": self.url,
            "verified_at": self.fetched_at.date().isoformat(),
        }


def is_official(url: str) -> bool:
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower()
    return any(host.endswith(s) for s in OFFICIAL_DOMAINS)


class OfficialSourceFetcher:
    """Fetches a whitelisted official page; rejects non-official URLs outright."""

    name = "immigration_official"
    kind = "official_http"

    def __init__(self, timeout: int = 25):
        self.timeout = timeout

    def verify(self, url: str) -> OfficialPage:
        if not is_official(url):
            return OfficialPage(
                url=url, host=_host(url), fetched_at=_now(),
                text="", ok=False, error=f"Domain not on official whitelist: {_host(url)}",
            )
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout)
            resp.raise_for_status()
        except Exception as exc:
            return OfficialPage(url=url, host=_host(url), fetched_at=_now(),
                                text="", ok=False, error=f"Fetch failed: {exc}")
        text = _to_text(resp.text)
        return OfficialPage(url=url, host=_host(url), fetched_at=_now(), text=text,
                            title=_title_from(text))

    async def search(self, query: str, location: str = "") -> list:
        """Protocol compatibility: NOT for jobs. Raises to signal misuse."""
        raise NotImplementedError("Immigration connectors do not serve job Opportunities.")


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_text(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for bad in soup(["script", "style", "nav", "footer"]):
        bad.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())[:30000]


def _title_from(text: str) -> str:
    return text[:120] or ""
