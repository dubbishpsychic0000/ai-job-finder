"""Immigration Agent — the evidence-backed subsystem.

Flow: pick an official URL for (country, occupation) -> fetch via
OfficialSourceFetcher (whitelist enforced) -> LLM extracts claims -> each claim
stores {claim, source=official URL, verified_at}. Rejected pages (non-official
or unreachable) yield NO claims, not guesses.
"""
from __future__ import annotations

import json
import logging

from app.agents.llm import LLMProvider
from app.connectors.immigration.official import OfficialPage, OfficialSourceFetcher, is_official

logger = logging.getLogger(__name__)

_IMMIGRATION_SYSTEM = """You read OFFICIAL government pages about immigration /
work permits. Return ONLY JSON:
{"eligible_programs": [{"program": str, "occupation_notes": str,
  "language_level": str, "experience_years": str, "eligibility": str,
  "restrictions": str}], "occupation_match": bool, "summary": str}
Quote only what the page states. Empty list if nothing supports a program.
IMMIGRATION_ANALYST"""

# English-language official entry points per country (stable, official).
DEFAULT_OFFICIAL_URLS: dict[str, list[str]] = {
    "Canada": ["https://www.canada.ca/en/immigration-refugees-citizenship/services/immigrate-canada/express-entry.html"],
    "France": ["https://www.service-public.fr/particuliers/vosdroits/F17931"],
    "Germany": ["https://www.make-it-in-germany.com/en/visa-residence/employment/general-employment"],
    "Belgium": ["https://www.belgium.be/en/work/working_belgium/foreigners"],
    "Netherlands": ["https://ind.nl/en/work/working-in-the-netherlands"],
    "Spain": ["https://www.exteriores.gob.es/en/ServiciosAlCiudadano/SitiosPaginas/Trabajaryestudiar/motivostrabajar.aspx"],
    "Portugal": ["https://www.sef.pt/en/"],
    "Australia": ["https://immi.homeaffairs.gov.au/visas/getting-a-visa/visa-listing/skilled-independent-491"],
    "United Kingdom": ["https://www.gov.uk/skilled-worker-visa"],
    "USA": ["https://www.uscis.gov/working-in-the-united-states"],
    "Switzerland": ["https://www.sem.admin.ch/sem/en/home/themen/arbeit/nicht_eu_efta.html"],
}


class ImmigrationAgent:
    def __init__(self, llm: LLMProvider, fetcher: OfficialSourceFetcher | None = None):
        self.llm = llm
        self.fetcher = fetcher or OfficialSourceFetcher()

    async def research(self, country: str, occupation: str = "") -> dict:
        """Return claims with evidence for a country (no DB writes here)."""
        urls = DEFAULT_OFFICIAL_URLS.get(country, [])
        pages: list[OfficialPage] = []
        for url in urls:
            page = self.fetcher.verify(url)
            pages.append(page)
            if page.ok:
                break
        if not pages or not any(p.ok for p in pages):
            return {
                "country": country,
                "occupation": occupation,
                "eligible": False,
                "claims": [],
                "status": "unverified",
                "reason": pages[0].error if pages else "no official source configured",
            }

        ok_page = next(p for p in pages if p.ok)
        result = await self.llm.complete_json(
            _IMMIGRATION_SYSTEM,
            json.dumps({
                "country": country,
                "occupation": occupation,
                "page_url": ok_page.url,
                "page_text": ok_page.text[:20000],
            }, ensure_ascii=False),
        )
        if not isinstance(result, dict):
            result = {}
        programs = result.get("eligible_programs") or []
        claims = []
        for prog in programs:
            claims.append(ok_page.claim(
                f"{country}: {prog.get('program', 'skilled program')} may apply to {occupation or 'this occupation'}."
            ))
        return {
            "country": country,
            "occupation": occupation,
            "eligible": bool(programs),
            "programs": programs,
            "claims": claims,
            "source_url": ok_page.url,
            "status": "verified",
        }

    def validate_url(self, url: str) -> bool:
        return is_official(url)
