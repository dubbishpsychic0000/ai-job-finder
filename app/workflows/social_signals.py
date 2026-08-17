"""Social signal discovery workflow (spec §15–§18) — authorized channels only.

Surfaces LinkedIn and Meta/Facebook opportunity signals through the ONLY legal
channels the spec permits (§34): the public search-engine index and URLs/leads
the user explicitly provides. No login, no session reuse, no scraping of the
platforms — the connectors enforce this, and every result is persisted as an
`opportunity_sources` row with `kind="social_signal"` and the default
`unverified` status.

Opt-in via `discovery.social_signal_discovery`; `sources_path` and `search_fn`
are injectable so tests stay hermetic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app import memory as mem
from app.config import ROOT_DIR, AgentConfig, CandidateProfile, Preferences

logger = logging.getLogger(__name__)

DEFAULT_SOCIAL_SOURCES_PATH = ROOT_DIR / "config" / "social_sources.yaml"
SOURCE = "social_signal_discovery"


@dataclass
class SocialSignalsReport:
    discovered: int = 0
    stored: int = 0
    sources_run: int = 0
    errors: list[str] = field(default_factory=list)


async def run_social_signal_discovery(session: Session, config: AgentConfig,
                                      prefs: Preferences, *,
                                      sources_path: Path | None = None,
                                      profile: CandidateProfile | None = None,
                                      search_fn=None):
    """Run the Linkedin/Meta connectors and persist each surface as a source.

    `search_fn` is forwarded to the LinkedIn connector (index channel); the Meta
    connector never performs network access regardless.
    """
    report = SocialSignalsReport()
    queries = _queries(prefs, profile)
    for source_cfg, connector in _connectors(sources_path):
        report.sources_run += 1
        results: list[Any] = []
        try:
            for query, location in queries:
                try:
                    results.extend(await connector.search(query, location))
                except (TypeError, NotImplementedError):
                    break
        except Exception as exc:
            report.errors.append(f"{source_cfg.get('name')}: {exc}")
            continue
        report.discovered += len(results)
        by_channel: dict[str, int] = {}
        for o in results:
            if not o.url:
                continue
            channel = (o.raw or {}).get("channel", "user_provided")
            by_channel[channel] = by_channel.get(channel, 0) + 1
            _, created = mem.store.upsert_opportunity_source(
                session,
                kind="social_signal",
                url=o.url,
                title=o.title,
                country=o.country,
                source=o.source,
                notes=channel + "; " + connector.policy_notice[:120],
            )
            if created:
                report.stored += 1
        if results:
            detail = ", ".join(f"{k}:{v}" for k, v in by_channel.items()) or "total:0"
            mem.store.record_event(
                session, SOURCE,
                f"{connector.name}: surfaced {len(results)} social signals ({detail})",
                "info", {"job_id": None})
    return report


def _connectors(sources_path: Path | None) -> list[tuple[dict, Any]]:
    from app.workflows.discovery import _load_source_connectors

    return _load_source_connectors(sources_path or DEFAULT_SOCIAL_SOURCES_PATH)


def _queries(prefs: Preferences, profile: CandidateProfile | None) -> list[tuple[str, str]]:
    """(query, location) pairs — one per target country, led by the first role."""
    role = (prefs.target_roles or [profile.title if profile else ""])[0] if (
        prefs.target_roles or (profile and profile.title)) else "job"
    countries = prefs.countries or [""]
    return [(f"{role} job", c) for c in countries]
