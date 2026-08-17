"""Country ranking (spec §12) — deterministic preference-affinity scoring.

Scores each target country from the candidate's LOCAL preference data only
(profile languages, relocation preferences). It never invents external facts:
score contributions are derived entirely from what the user configured, so the
ranking is auditable and stable for a given configuration.
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


def rank_countries(countries: list[str], prefs: Preferences | None = None,
                   profile: CandidateProfile | None = None) -> list[CountryScore]:
    """Score countries by preference affinity, best first (stable tie-break)."""
    langs = _profile_languages(profile)
    reloc = (getattr(profile, "relocation", None) or {}) if profile else {}
    preferred = [str(c).lower() for c in reloc.get("preferred_countries", [])]
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
    # stable sort: equal scores keep the input (target-countries) order
    scored.sort(key=lambda cs: cs.score, reverse=True)
    return scored
