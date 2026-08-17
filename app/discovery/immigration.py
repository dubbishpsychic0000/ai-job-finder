"""Immigration & work-pathway discovery (Discovery V2, Phase 4 — spec §11, §13).

A parallel system to the job hunt: researches migration entries, work permits,
skilled-worker programs and shortage-occupation lists across the target
countries, *independently* of any posting.

Safety property (inherits §11 + critique #8): a claim is only ever recorded when
its `source_url` host is on the official-domains whitelist
(`app.connectors.immigration.official.OFFICIAL_DOMAINS`). The engine never
invents rules — if a URL is not official the fact is dropped, not guessed.

Sources are config-driven (`config/immigration_sources.yaml`):

    - kind: html | rss | json
    - country, program, fact_type (shortage_occupation | program | work_permit | general)
    - urls: official URLs; `json` entries map items via list_path + mapping.

Every network dependency (`fetch_html`, `fetch_rss`, `fetch_json`) is injectable
so tests stay hermetic and offline.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.connectors.immigration.official import is_official

logger = logging.getLogger(__name__)

# Phrases that mark a mention as genuinely a shortage / in-demand situation.
SHORTAGE_MARKERS = (
    "shortage occupation", "shortage list", "skilled occupation list",
    "occupation in high demand", "in high demand", "scarce", "profession en tension",
    "métiers en tension", "metiers en tension", "liste des métiers",
    "fachkräfte", "fachkraefte", "engpass", "mangelberuf",
)

_CLAIM_CAP = 500
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+|(?:</p>\s*)+", re.MULTILINE)


@dataclass
class DiscoveredFact:
    """A single verifiable claim + evidence, ready for storage (spec §11)."""

    country: str
    program: str
    claim: str
    source_url: str
    source_domain: str
    retrieved_at: datetime
    confidence: int = 100
    occupation: str = ""
    fact_type: str = "general"
    matched: bool = False
    source_name: str = ""


@dataclass
class ImmigrationDiscoveryReport:
    facts_discovered: int = 0
    stored: int = 0
    matched: int = 0
    rejected_unofficial: int = 0
    priority_countries: list[tuple[str, int]] = field(default_factory=list)


def candidate_occupations(profile=None, prefs=None, vocab: Any | None = None) -> list[str]:
    """Occupation keyword space used for extraction + candidate matching."""
    from app.discovery.vocabulary import CandidateVocabulary

    v = vocab or CandidateVocabulary(profile=profile, prefs=prefs)
    return v.roles()


def _domain(url: str) -> str:
    return urlparse_host(url) or ""


def urlparse_host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc.lower()


def _trim(text: str, cap: int = _CLAIM_CAP) -> str:
    return " ".join((text or "").split())[:cap]


def _fact_type(claim: str) -> str:
    low = claim.lower()
    if any(m in low for m in SHORTAGE_MARKERS):
        return "shortage_occupation"
    if any(w in low for w in ("work permit", "permis de travail", "arbeitserlaubnis",
                              "foreign worker", "travailleur étranger")):
        return "work_permit"
    if any(w in low for w in ("program", "programme", "pathway", "skill")):
        return "program"
    return "general"


def _occupation_in(text: str, occupations: list[str]) -> str:
    low = text.lower()
    for occ in occupations:
        o = occ.strip().lower()
        if o and o in low:
            return occ.strip()
    return ""


def facts_from_text(text: str, country: str, program: str, source_url: str,
                    occupations: list[str], fact_type: str = "general",
                    limit: int = 10, retrieved_at: datetime | None = None) -> list[DiscoveredFact]:
    """Extract fact-worthy sentences from an official page.

    Only sentences that mention a candidate occupation (or a shortage marker)
    become facts — everything else is discarded so claims stay on-topic.
    """
    out: list[DiscoveredFact] = []
    if not text:
        return out
    now = retrieved_at or datetime.now(timezone.utc)
    seen: set[str] = set()
    for sentence in _SENTENCE_RE.split(text):
        sentence = _trim(sentence)
        if len(sentence) < 40:
            continue
        low = sentence.lower()
        if not (any(o and o in low for o in occupations) or any(m in low for m in SHORTAGE_MARKERS)):
            continue
        key = sentence[:140].lower()
        if key in seen or len(out) >= limit:
            continue
        seen.add(key)
        out.append(DiscoveredFact(
            country=country, program=program, claim=sentence,
            source_url=source_url, source_domain=_domain(source_url),
            retrieved_at=now, fact_type=_fact_type(sentence) if fact_type == "general" else fact_type,
            occupation=_occupation_in(sentence, occupations),
        ))
    return out


def facts_from_entries(entries: list[dict[str, Any]], country: str, program: str,
                       source_url: str, *, occupations: list[str] | None = None,
                       fact_type: str = "general",
                       limit: int = 10, retrieved_at: datetime | None = None) -> list[DiscoveredFact]:
    """Turn normalized RSS/Atom entry dicts into facts (claim: title + summary)."""
    out: list[DiscoveredFact] = []
    now = retrieved_at or datetime.now(timezone.utc)
    seen: set[str] = set()
    for entry in entries or []:
        if len(out) >= limit:
            break
        claim = _trim(f"{entry.get('title', '')} — {entry.get('summary', '')}")
        if len(claim) < 25:
            continue
        key = claim[:140].lower()
        if key in seen:
            continue
        seen.add(key)
        ftype = _fact_type(claim) if fact_type == "general" else fact_type
        out.append(DiscoveredFact(
            country=country, program=program, claim=claim,
            source_url=source_url, source_domain=_domain(source_url),
            retrieved_at=now, fact_type=ftype,
            occupation=_occupation_in(claim, occupations or []),
        ))
    return out


class ImmigrationDiscoveryEngine:
    """Discovers immigration/work-pathway facts from official sources.

    Fetchers (all injectable for hermetic tests):
        fetch_html(url) -> str            page text
        fetch_rss(url)  -> list[dict]     normalized entries (link, title, summary, published)
        fetch_json(url) -> dict | list    parsed JSON payload
    """

    DEFAULT_LIMIT_PER_COUNTRY = 25

    def __init__(self, *, countries: list[str], occupations: list[str],
                 sources: list[dict[str, Any]] | None = None,
                 fetch_html=None, fetch_rss=None, fetch_json=None,
                 now=None):
        self.countries = list(countries or [])
        self.occupations = list(occupations or [])
        self.sources = [s for s in (sources or []) if s.get("enabled", True)]
        self.fetch_html = fetch_html or _live_fetch_html
        self.fetch_rss = fetch_rss or _live_fetch_rss
        self.fetch_json = fetch_json or _live_fetch_json
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._last_facts: list[DiscoveredFact] = []
        self._rejected_unofficial = 0

    # ---- public API ----------------------------------------------------------

    async def discover(self, limit_per_country: int | None = None) -> list[DiscoveredFact]:
        limit = limit_per_country or self.DEFAULT_LIMIT_PER_COUNTRY
        facts: dict[str, list[DiscoveredFact]] = {}
        self._rejected_unofficial = 0
        for src in self.sources:
            country = src.get("country", "")
            if country not in self.countries:
                continue
            bucket = facts.setdefault(country, [])
            if len(bucket) >= limit:
                continue
            try:
                await self._discover_source(src, bucket, limit)
            except Exception as exc:
                logger.warning("immigration source %r failed: %s", src.get("name"), exc)
                continue
        out = [f for bucket in facts.values() for f in bucket]
        self._last_facts = out
        return out

    def rejected_unofficial(self) -> int:
        return self._rejected_unofficial

    def match_occupation(self, fact: DiscoveredFact) -> tuple[bool, str]:
        """Deterministic candidate-fit: is the candidate's occupation present?"""
        text = f"{fact.claim} {fact.occupation}".lower()
        for occ in self.occupations:
            o = occ.strip().lower()
            if o and o in text:
                return True, occ.strip()
        return False, ""

    def priority_countries(self, facts: list[DiscoveredFact] | None = None) -> list[tuple[str, int]]:
        """Countries by number of matched facts (reverse-opportunity signal, spec §13)."""
        facts = facts if facts is not None else self._last_facts
        counts = Counter(f.country for f in facts if f.matched)
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))

    # ---- internals -----------------------------------------------------------

    async def _discover_source(self, src: dict, bucket: list[DiscoveredFact], limit: int) -> None:
        country = src.get("country", "")
        program = src.get("program") or f"{country} official immigration / employment channel"
        ftype = src.get("fact_type", "general")
        source_name = src.get("name", "")
        kind = src.get("kind", "html")
        now = self._now()

        if kind == "rss":
            for url in src.get("urls") or []:
                if not is_official(url):
                    self._rejected_unofficial += 1
                    continue
                entries = (await self.fetch_rss(url)) or []
                for f in facts_from_entries(entries, country, program, url,
                                            occupations=self.occupations,
                                            fact_type=ftype, limit=limit - len(bucket),
                                            retrieved_at=now):
                    self._add(bucket, f, source_name, limit)
            return

        if kind == "json":
            for url in src.get("urls") or []:
                if not is_official(url):
                    self._rejected_unofficial += 1
                    continue
                payload = await self.fetch_json(url)
                items = _list_items(payload, src.get("list_path", ""))
                mapping = src.get("mapping", {})
                for raw in items:
                    if len(bucket) >= limit:
                        break
                    claim = _trim(str(raw.get(mapping.get("claim", "claim"), "") or
                                       raw.get(mapping.get("occupation", "occupation"), "") or ""))
                    if not claim:
                        continue
                    fact = DiscoveredFact(
                        country=country, program=program,
                        claim=claim[:_CLAIM_CAP],
                        source_url=url, source_domain=_domain(url),
                        retrieved_at=now, confidence=100,
                        occupation=raw.get(mapping.get("occupation", "occupation"), "") or "",
                        fact_type=raw.get(mapping.get("fact_type", "fact_type"), ftype) or ftype,
                        source_name=source_name)
                    self._add(bucket, fact, source_name, limit)
                if len(bucket) >= limit:
                    break
            return

        # html
        for url in src.get("urls") or []:
            if not is_official(url):
                self._rejected_unofficial += 1
                continue
            text = (await self.fetch_html(url)) or ""
            for f in facts_from_text(text, country, program, url, self.occupations,
                                     fact_type=ftype, limit=limit - len(bucket),
                                     retrieved_at=now):
                self._add(bucket, f, source_name, limit)

    def _add(self, bucket: list[DiscoveredFact], fact: DiscoveredFact,
             source_name: str, limit: int) -> None:
        if not is_official(fact.source_url):
            self._rejected_unofficial += 1
            return
        if len(bucket) >= limit:
            return
        fact.source_name = fact.source_name or source_name
        bucket.append(fact)


def _list_items(payload: Any, path: str) -> list[dict[str, Any]]:
    """Resolve a dotted path (e.g. 'results.list') to a list of dicts."""
    node = payload
    for part in (path or "").split("."):
        if not part:
            return []
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return []
        else:
            return []
    if isinstance(node, list):
        return [i for i in node if isinstance(i, dict)]
    if isinstance(node, dict):
        return [node]
    return []


# ---- live (production) fetchers -------------------------------------------------
def _live_fetch_html(url: str) -> str:
    from app.connectors.immigration.official import OfficialSourceFetcher

    return OfficialSourceFetcher().verify(url).text


def _live_fetch_rss(url: str) -> list[dict[str, Any]]:
    import feedparser
    import requests as _req

    resp = _req.get(url, headers={"User-Agent": "WorldwideCareerAgent/0.1 (official feeds)"}, timeout=20)
    resp.raise_for_status()
    return [{
        "link": getattr(e, "link", "") or "",
        "title": getattr(e, "title", "") or "",
        "summary": getattr(e, "summary", "") or getattr(e, "description", "") or "",
        "published": getattr(e, "published", "") or getattr(e, "updated", ""),
    } for e in feedparser.parse(resp.content).entries]


def _live_fetch_json(url: str) -> Any:
    import requests as _req

    resp = _req.get(url, headers={"User-Agent": "WorldwideCareerAgent/0.1 (official API)"}, timeout=20)
    resp.raise_for_status()
    return resp.json()
