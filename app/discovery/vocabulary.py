"""Candidate Search Vocabulary (Discovery V2, Phase 1).

Turns the candidate profile + preferences into the full set of search terms the
discovery engine should query:

* canonical target roles from `preferences.target_roles`
* occupation synonyms, alternative titles and junior/entry-level variants
* skill-derived terms from the candidate profile
* localized role terms per target country (curated map + `localized_roles`)

Everything is deterministic and offline by default. When `discovery.vocab_llm`
is enabled, an LLM can expand roles further; expansions are cached on disk so
the same role is never expanded twice.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app import config as _cfg
from app.config import ROOT_DIR, CandidateProfile, Preferences

logger = logging.getLogger(__name__)

# Language spoken in each target country (drives localized term selection).
LANG_OF_COUNTRY = {
    "France": "fr",
    "Belgium": "nl",   # bilingual; defaults handled by caller
    "Canada": "fr",    # Quebec flavour; fine for FR queries
    "Germany": "de",
    "Spain": "es",
    "Portugal": "pt",
    "Netherlands": "nl",
}

JUNIOR_PREFIXES = ("junior ", "assistant ", "entry-level ", "trainee ", "apprentice ")

# Canonical role (lowercased) -> occupation synonyms / alternative titles.
ROLE_SYNONYMS: dict[str, list[str]] = {
    "civil engineering technician": [
        "civil engineering technologist",
        "civil works technician",
        "construction technician",
        "site technician",
        "road technician",
        "infrastructure technician",
        "vrd technician",
        "geotechnical technician",
        "cad technician",
        "civil cad technician",
        "assistant engineer civil",
    ],
    "site technician": [
        "works technician",
        "site inspector technician",
        "construction site technician",
        "civil site technician",
        "field technician construction",
    ],
    "road construction technician": [
        "road works technician",
        "highway technician",
        "roads maintenance technician",
        "roadworks technician",
    ],
    "technicien genie civil": [
        "technicien en genie civil",
        "technicien travaux publics",
        "technicien de chantier",
        "technicien constructions civiles",
    ],
    "technicien vrd": [
        "technicien voirie et reseaux divers",
        "technicien voirie",
        "dessinateur vrd",
        "technicien reseaux",
    ],
    "technicien travaux publics": [
        "technicien tp",
        "technicien en travaux publics",
        "aide conducteur de travaux",
        "technicien chantier tp",
    ],
    "constructie": [
        "werfleider",
        "uitvoerder",
        "bouwkundig tekenaar",
        "technicus wegenbouw",
    ],
}

# Language -> extra localized role terms (complements preferences.localized_roles).
LOCALIZED_SYNONYMS: dict[str, list[str]] = {
    "fr": [
        "technicien chantier",
        "technicien études",
        "technicien voirie",
        "dessinateur projeteur",
        "assistant conducteur de travaux",
    ],
    "de": [
        "bauzeichner",
        "tiefbauzeichner",
        "bauleiter assistant",
        "vermessungstechniker bau",
    ],
    "es": [
        "técnico de obras",
        "técnico auxiliar de obra",
        "auxiliar de ingeniería civil",
    ],
    "pt": [
        "técnico de construção",
        "técnico de vias",
        "desenhador de projetos civis",
    ],
    "nl": [
        "bouwkundig tekenaar",
        "tekenaar civiele techniek",
        "medewerker wegenbouw",
    ],
}


def _dedup_stable(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(item.strip())
    return out


class CandidateVocabulary:
    """Expands the candidate into the searchable term space (deterministic)."""

    def __init__(self, profile: CandidateProfile | None = None,
                 prefs: Preferences | None = None, cache_path: str | Path | None = None):
        self.profile = profile or _cfg.get_profile()
        self.prefs = prefs or _cfg.get_preferences()
        self.cache_path = Path(cache_path) if cache_path else None
        self.extra_terms: list[str] = []  # populated by llm_expanded_roles()

    # ---- deterministic vocabulary -------------------------------------------------

    def roles(self, with_skills: bool = True) -> list[str]:
        """English/role search terms: canonical roles + synonyms + junior variants + skills."""
        terms: list[str] = []
        for role in self.prefs.target_roles:
            r = role.strip().lower()
            terms.append(role.strip())
            for syn in ROLE_SYNONYMS.get(r, []):
                terms.append(syn)
                for prefix in JUNIOR_PREFIXES:
                    terms.append(f"{prefix}{syn}")
            for prefix in JUNIOR_PREFIXES:
                terms.append(f"{prefix}{role.strip()}")
        if with_skills:
            terms.extend(skill.strip() for skill in self.profile.skills)
        terms.extend(self.extra_terms)
        return _dedup_stable(terms)

    def localized_terms(self, lang: str) -> list[str]:
        """Localized role terms for a language (curated map + preferences)."""
        prefs_terms = (self.prefs.localized_roles or {}).get(lang, [])
        return _dedup_stable(list(prefs_terms) + LOCALIZED_SYNONYMS.get(lang, []))

    def country_terms(self, country: str) -> list[str]:
        """Localized role terms for a target country."""
        return self.localized_terms(LANG_OF_COUNTRY.get(country, "en"))

    # ---- optional LLM expansion (cached on disk) ----------------------------------

    def _load_cache(self) -> dict[str, Any]:
        path = self._cache_file()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("vocab cache unreadable at %s — ignoring", path)
            return {}

    def _save_cache(self, data: dict[str, Any]) -> None:
        path = self._cache_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("vocab cache write failed at %s: %s", path, exc)

    def _cache_file(self) -> Path:
        return self.cache_path or ROOT_DIR / "data" / "search_vocab_cache.json"

    async def llm_expanded_roles(self, llm: Any) -> list[str]:
        """Expand roles via LLM. Results are cached per role; never re-queried."""
        cached = self._load_cache()
        extras: list[str] = []
        changed = False
        for role in self.prefs.target_roles:
            key = role.strip().lower()
            if key not in cached:
                changed = True
                try:
                    payload = await llm.complete_json(
                        system=(
                            "You are a recruitment search vocabulary expert for a "
                            "civil-engineering technician job hunter who wants to "
                            "relocate abroad. Return ONLY a JSON object."
                        ),
                        user=(
                            f"Target role: {role!r}.\n"
                            "Return a JSON object like {\"synonyms\": [\"...\", \"...\"]} "
                            "with 5-8 occupation synonyms, alternative job titles and "
                            "common employer search terms in English. No other text."
                        ),
                    )
                except Exception as exc:  # never break discovery on LLM failure
                    logger.warning("LLM vocab expansion failed for %r: %s", role, exc)
                    payload = {"synonyms": []}
                cached[key] = [s for s in payload.get("synonyms", []) if isinstance(s, str)]
            extras.extend(cached[key])
        if changed:
            self._save_cache(cached)
        self.extra_terms = [s for s in extras if s.strip().lower() not in self.roles()]
        return _dedup_stable(self.roles() + extras)
