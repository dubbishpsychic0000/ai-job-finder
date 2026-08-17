"""Country ranking (spec §12) — deterministic preference-affinity scoring.

Scores each target country from LOCAL data plus, when a DB session is provided,
the agent's own collected evidence (job demand, sponsorship / international-hiring
signals per posting, verified immigration programmes and discovery-led
opportunity counts). Weights are configurable via
`discovery.country_ranking_weights` (spec default 25/20/15/15/15/5/5).

Stability guarantee: the session-less path keeps the original pure behaviour
(language + preferred-relocation only) so analytics and hermetic tests that don't
carry a DB stay byte-stable for a given configuration.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.config import CandidateProfile, Preferences

# Official languages per country — used to reward a country whose official
# languages overlap the candidate's spoken languages.
OFFICIAL_LANGUAGES = {
    "Belgium": {"fr", "nl", "de", "en"},
    "Canada": {"en", "fr"},
    "France": {"fr"},
    "Germany": {"de"},
    "Netherlands": {"nl"},
    "Portugal": {"pt"},
    "Spain": {"es"},
    "Switzerland": {"de", "fr", "it"},
    "Luxembourg": {"fr", "de", "lb"},
    "Australia": {"en"},
    "United Kingdom": {"en"},
    "United States": {"en"},
    "Ireland": {"en"},
    "Italy": {"it"},
    "Austria": {"de"},
    "Quebec": {"fr"},
}

LANG_CONTRIBUTION = 1.5
RELOCATION_CONTRIBUTION = 2.0

# §12 — relative importance of each scoring dimension (sums to 100).
DEFAULT_WEIGHTS = {
    "language": 25,        # spoken language(s) overlap an official language
    "demand": 20,          # open roles already discovered there
    "international": 15,   # postings that hire international applicants
    "sponsorship": 15,     # postings that offer visa sponsorship
    "relocation": 15,      # candidate's preferred relocate-to countries
    "immigration": 5,      # verified immigration pathways on file
    "opportunities": 5,    # discovery-led opportunity sources / strong queries
}

# Profile language names -> ISO 639-1 codes used in OFFICIAL_LANGUAGES.
LANG_TO_ISO = {
    "french": "fr", "français": "fr", "francais": "fr",
    "english": "en", "anglais": "en",
    "german": "de", "deutsch": "de", "allemand": "de",
    "dutch": "nl", "neerlandais": "nl", "néerlandais": "nl",
    "portuguese": "pt", "portugais": "pt",
    "spanish": "es", "espagnol": "es", "español": "es",
    "italian": "it", "italien": "it",
    "swedish": "sv", "suédois": "sv",
    "arabic": "ar",
}

POSITIVE_SIGNALS = {"yes", "likely"}


@dataclass
class CountryScore:
    country: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def _profile_languages(profile: CandidateProfile | None) -> set[str]:
    if not profile:
        return set()
    out: set[str] = set()
    for name, _level in (profile.languages or {}).items():
        low = str(name).lower().strip()
        out.add(LANG_TO_ISO.get(low, low[:2]))
        out.add(low)
    return out


def _fraction(numerator: int, denominator: int) -> float:
    return float(numerator) / denominator if denominator else 0.0


def _db_evidence(session, countries: list[str]) -> dict[str, dict]:
    """Aggregate per-country evidence from the agent's own ledger (empty on failure)."""
    from app import models

    evidence = {c: {} for c in countries}
    try:
        from sqlalchemy import func, select

        jobs_in = models.Job.country.in_(countries)
        for country, total in session.execute(
                select(models.Job.country, func.count(models.Job.id)).where(jobs_in)
                .group_by(models.Job.country)).all():
            evidence[country]["jobs"] = total
        for country, hit in session.execute(
                select(models.Job.country, func.count(models.Job.id))
                .where(jobs_in, models.Job.sponsorship_signal.in_(POSITIVE_SIGNALS))
                .group_by(models.Job.country)).all():
            evidence[country]["sponsorship"] = hit
        for country, hit in session.execute(
                select(models.Job.country, func.count(models.Job.id))
                .where(jobs_in, models.Job.international_candidate_signal.in_(POSITIVE_SIGNALS))
                .group_by(models.Job.country)).all():
            evidence[country]["international"] = hit
        for country, n in session.execute(
                select(models.ImmigrationProgram.country, func.count(models.ImmigrationProgram.id))
                .where(models.ImmigrationProgram.country.in_(countries))
                .group_by(models.ImmigrationProgram.country)).all():
            evidence[country]["immigration"] = n
        for country, n in session.execute(
                select(models.QueryStat.country, func.sum(models.QueryStat.relevant_jobs))
                .where(models.QueryStat.country.in_(countries))
                .group_by(models.QueryStat.country)).all():
            evidence[country]["opportunities"] = n
    except Exception:
        # a read failure must never crash the scheduler — evidence degrades to zero
        evidence = {c: {} for c in countries}
    return evidence


def rank_countries(countries: list[str], prefs: Preferences | None = None,
                   profile: CandidateProfile | None = None,
                   session=None, weights: dict | None = None) -> list[CountryScore]:
    """Score countries by affinity, best first (stable tie-break).

    Without a session the legacy pure model (language + relocation) is used. With
    a session the full §12 model adds DB-derived demand, sponsorship,
    international-hiring, immigration and opportunity evidence.
    """
    langs = _profile_languages(profile)
    reloc = (getattr(profile, "relocation", None) or {}) if profile else {}
    preferred = [str(c).lower() for c in reloc.get("preferred_countries", [])]

    if session is None:  # pure preference-affinity path (legacy, byte-stable)
        scored: list[CountryScore] = []
        for country in countries:
            official = OFFICIAL_LANGUAGES.get(country, set())
            overlap = official & langs
            reasons: list[str] = []
            score = 0.0
            if overlap:
                score += LANG_CONTRIBUTION
                reasons.append("language " + ", ".join(sorted(overlap)))
            if country.lower() in preferred:
                score += RELOCATION_CONTRIBUTION
                reasons.append("preferred relocation")
            scored.append(CountryScore(country=country, score=score, reasons=reasons))
        scored.sort(key=lambda cs: cs.score, reverse=True)
        return scored

    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    evidence = _db_evidence(session, countries)
    max_demand = max((e.get("jobs", 0) for e in evidence.values()), default=0)
    max_intl = max((e.get("international", 0) for e in evidence.values()), default=0)
    max_spo = max((e.get("sponsorship", 0) for e in evidence.values()), default=0)
    max_imm = max((e.get("immigration", 0) for e in evidence.values()), default=0)
    max_opp = max((e.get("opportunities", 0) for e in evidence.values()), default=0)

    scored = []
    for country in countries:
        ev = evidence[country]
        reasons: list[str] = []
        score = 0.0
        official = OFFICIAL_LANGUAGES.get(country, set())
        overlap = sorted(official & langs)
        if overlap:
            score += float(w["language"]) * min(1.0, 0.5 + 0.5 * len(overlap))
            reasons.append("language " + ", ".join(overlap))
        if country.lower() in preferred:
            score += float(w["relocation"])
            reasons.append("preferred relocation")
        demand = _fraction(ev.get("jobs", 0), max_demand)
        if demand:
            score += float(w["demand"]) * demand
            reasons.append(f"{ev.get('jobs', 0)} open roles")
        intl = _fraction(ev.get("international", 0), max_intl)
        if intl:
            score += float(w["international"]) * intl
            reasons.append("international hiring")
        spo = _fraction(ev.get("sponsorship", 0), max_spo)
        if spo:
            score += float(w["sponsorship"]) * spo
            reasons.append("visa sponsorship")
        imm = _fraction(ev.get("immigration", 0), max_imm)
        if imm:
            score += float(w["immigration"]) * imm
            reasons.append("immigration pathways")
        opp = _fraction(ev.get("opportunities", 0), max_opp)
        if opp:
            score += float(w["opportunities"]) * opp
            reasons.append("opportunity signal")
        scored.append(CountryScore(country=country, score=round(score, 2),
                                   reasons=reasons))
    scored.sort(key=lambda cs: cs.score, reverse=True)
    return scored
