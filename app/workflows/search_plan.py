"""Search Strategy Generator — turns preferences into query×location combos.

From the spec:
    Civil Engineering Technician / Technicien Génie Civil / VRD Technician / ...
    × France / Belgium / Canada / Germany / ...

Combines the English target roles with localized role variants per language.
"""
from __future__ import annotations

from app.config import Preferences

LANG_OF_COUNTRY = {
    "France": "fr",
    "Belgium": "nl",   # bilingual; defaults handled by caller
    "Canada": "fr",    # Quebec flavour; fine for FR queries
    "Germany": "de",
    "Spain": "es",
    "Portugal": "pt",
    "Netherlands": "nl",
}


class SearchPlan:
    def __init__(self, prefs: Preferences):
        self.prefs = prefs

    def build(self, max_per_country: int = 3) -> list[dict]:
        plan: list[dict] = []
        roles = list(self.prefs.target_roles)
        seen: set[tuple[str, str]] = set()
        for country in self.prefs.countries:
            lang = LANG_OF_COUNTRY.get(country, "en")
            localized = (self.prefs.localized_roles or {}).get(lang, [])
            queries = roles[:max_per_country]
            if lang != "en":
                queries = roles[0:1] + localized[: max(0, max_per_country - 1)]
            # cap
            queries = queries[:max_per_country]
            for q in queries:
                key = (q.lower(), country.lower())
                if key in seen:
                    continue
                seen.add(key)
                plan.append({"query": q, "location": country, "lang": lang,
                             "country": country})
        return plan

    def plan_for_country(self, country: str, max_per_country: int = 3) -> list[dict]:
        sub = self.prefs.model_copy(update={"target_countries": [country]})
        return SearchPlan(sub).build(max_per_country)
