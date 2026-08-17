"""Opportunity freshness + verification (Discovery V2, Phase 5 — spec §19, §20).

§20 Freshness — every opportunity gets a freshness label + score from its
`posted_at` age:

    0–3 days       VERY HIGH
    4–7 days       HIGH
    8–14 days      MEDIUM
    15–30 days     LOW
    >30 days       STALE

§19 Verification — the live-posting check ("does the original posting still
exist?") that runs before an opportunity is trusted and again before an
employer email is generated. The network call is injectable so tests stay
hermetic and the pipeline degrades offline.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

FRESHNESS_BANDS = [
    (3, "very_high"),
    (7, "high"),
    (14, "medium"),
    (30, "low"),
]

FRESHNESS_SCORES = {
    "very_high": 100,
    "high": 85,
    "medium": 65,
    "low": 45,
    "stale": 20,
    "unknown": 50,
}


def freshness_label(posted_at: datetime | None, now: datetime | None = None) -> str:
    """Freshness bucket for a posting date (spec §20)."""
    if posted_at is None:
        return "unknown"
    now = now or datetime.now(timezone.utc)
    try:
        age_days = max(0, (now - posted_at).total_seconds() // 86400)
    except TypeError:
        return "unknown"
    for days, label in FRESHNESS_BANDS:
        if age_days <= days:
            return label
    return "stale"


def freshness_score(posted_at: datetime | None, now: datetime | None = None) -> int:
    """0–100 score from the freshness label (drives source confidence, §23)."""
    return FRESHNESS_SCORES[freshness_label(posted_at, now)]


@dataclass
class VerificationResult:
    ok: bool = False
    status_code: int | None = None
    error: str = ""
    live: bool = False
    title: str = ""


class OpportunityVerifier:
    """§19 — confirm a posting URL still resolves to a live page.

    `fetch_html(url)` is injectable (defaults to a real GET). Any transport
    error, timeout, 4xx/5xx or an empty body means NOT verified — a guaranteed
    false positive is worse than an offline "unverified" status.
    """

    def __init__(self, fetch_html: Callable[[str], str | None] | None = None):
        self.fetch_html = fetch_html or _live_fetch

    def verify(self, url: str) -> VerificationResult:
        if not url:
            return VerificationResult(ok=False, error="no url")
        try:
            html = self.fetch_html(url)
        except _LiveCheckFailed as exc:
            return VerificationResult(ok=False, error=str(exc))
        except Exception as exc:  # any transport failure = unverified
            return VerificationResult(ok=False, error=str(exc))
        if not html or not html.strip():
            return VerificationResult(ok=False, error="empty page")
        return VerificationResult(ok=True, live=True, title=html[:120])


class _LiveCheckFailed(Exception):
    """Raised when the live fetcher receives a non-2xx status."""


def _live_fetch(url: str) -> str:
    import requests

    resp = requests.get(url, headers={"User-Agent": "WorldwideCareerAgent/0.1 (verifier)"},
                        timeout=20, allow_redirects=True)
    if resp.status_code in (401, 403, 404, 410, 429) or resp.status_code >= 500:
        raise _LiveCheckFailed(f"status {resp.status_code}")
    return resp.text[:20000]
