"""Search Strategy Generator (V2) — intent-based multilingual query planning.

Phase 1: builds the discovery query plan from the candidate vocabulary
(roles + synonyms + junior variants + localized terms per country) and applies
per-intent query templates with weights, then caps the plan per country and
globally (`discovery.max_queries_per_run`).

Plan items keep the legacy keys (`query`/`location`/`country`/`lang`) so
connectors and discovery stay source-compatible, and add `intent` + `weight`.
"""
from __future__ import annotations

from app.config import Preferences
from app.discovery.vocabulary import LANG_OF_COUNTRY, CandidateVocabulary

# intent -> (weight, query builder(term, country, lang))
# Weights drive the ranking that the per-run budget is spent on: the most
# targeted queries always get searched before opportunistic ones.
INTENT_TEMPLATES = (
    ("role", 1.00, lambda term, country, lang: term),
    ("sponsorship", 0.85, lambda term, country, lang: f'{term} "visa sponsorship"'),
    ("international", 0.80, lambda term, country, lang: f'{term} "international applicants"'),
    ("work_permit", 0.70, lambda term, country, lang: f'{term} "work permit"'),
    ("relocation", 0.55, lambda term, country, lang: f'{term} "relocation assistance"'),
)
LOCAL_INTENT = ("local_language", 0.95, lambda term, country, lang: term)


class SearchPlan:
    def __init__(self, prefs: Preferences, profile=None, cache_path=None, vocab=None):
        self.prefs = prefs
        self.vocab = vocab or CandidateVocabulary(profile=profile, prefs=prefs, cache_path=cache_path)

    def build(self, max_per_country: int = 3, max_queries_per_run: int | None = None) -> list[dict]:
        plan: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for country in self.prefs.countries:
            lang = LANG_OF_COUNTRY.get(country, "en")
            role_terms = self.vocab.roles()[:max_per_country]
            loc_terms = self.vocab.country_terms(country)[:max_per_country]
            candidates: list[tuple[float, dict]] = []
            for term in role_terms:
                for intent, weight, builder in INTENT_TEMPLATES:
                    query = builder(term, country, lang)
                    candidates.append((weight, {
                        "query": query, "location": country, "country": country,
                        "lang": lang, "intent": intent, "weight": weight,
                    }))
            for term in loc_terms:
                query = LOCAL_INTENT[2](term, country, lang)
                candidates.append((LOCAL_INTENT[1], {
                    "query": query, "location": country, "country": country,
                    "lang": lang, "intent": LOCAL_INTENT[0], "weight": LOCAL_INTENT[1],
                }))
            candidates.sort(key=lambda pair: pair[0], reverse=True)
            for _weight, item in candidates:
                key = (item["query"].lower(), country.lower())
                if key in seen:
                    continue
                seen.add(key)
                plan.append(item)
        plan.sort(key=lambda item: item["weight"], reverse=True)
        if max_queries_per_run:
            plan = plan[:max_queries_per_run]
        return plan

    def plan_for_country(self, country: str, max_per_country: int = 3) -> list[dict]:
        sub = self.prefs.model_copy(update={"target_countries": [country]})
        return SearchPlan(sub, vocab=self.vocab).build(max_per_country)
