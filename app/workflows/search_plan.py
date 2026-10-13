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
from app.discovery.vocabulary import (
    LANG_OF_COUNTRY,
    LANGS_OF_COUNTRY,
    CandidateVocabulary,
)

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

# Opportunistic query categories (§5) — filled in only from leftover per-run
# budget headroom so they never displace the targeted core plan.
OPPORTUNISTIC_INTENTS = (
    ("career", 0.50, lambda term, country, lang: f'{term} "career openings"'),
    ("salary", 0.45, lambda term, country, lang: f'{term} "salary"'),
    ("remote", 0.40, lambda term, country, lang: f'{term} remote'),
)

COMBINATION_INTENTS = (
    ("skill", 0.76, lambda role, value: f'{role} {value}'),
    ("software", 0.74, lambda role, value: f'{role} {value}'),
    ("industry", 0.72, lambda role, value: f'{role} {value}'),
    ("seniority", 0.78, lambda role, value: f'{role} {value}'),
    ("internship", 0.68, lambda role, value: f'{role} {value}'),
)

INTERNSHIP_SEARCHES = {
    "ar": [
        "تدريب الهندسة المدنية",
        "تدريب مشروع نهاية الدراسة الهندسة المدنية",
        "تدريب تقني الأشغال العمومية",
    ],
    "de": [
        "praktikum bauingenieurwesen",
        "abschlusspraktikum bauingenieurwesen",
        "bautechniker praktikum",
    ],
    "en": [
        "civil engineering technician internship",
        "civil engineering PFE internship",
        "graduate civil engineering technician trainee",
    ],
    "es": [
        "prácticas de ingeniería civil",
        "prácticas fin de carrera ingeniería civil",
        "técnico de obras prácticas",
    ],
    "fr": [
        "stage génie civil",
        "stage PFE génie civil",
        "stage de fin d'études génie civil",
        "stage pré-embauche technicien BTP",
    ],
    "nl": [
        "stage civiele techniek",
        "afstudeerstage civiele techniek",
        "junior bouwtechnicus trainee",
    ],
    "pt": [
        "estágio de engenharia civil",
        "estágio de fim de curso engenharia civil",
        "técnico de obras estágio",
    ],
}


def _fair_cap(
    items: list[dict],
    limit: int | None,
    countries: list[str],
) -> list[dict]:
    if not limit or limit >= len(items):
        return items
    if limit <= 0:
        return []

    ranked = list(enumerate(items))
    buckets: dict[tuple[str, str], list[tuple[int, dict]]] = {}
    bucket_queries: dict[tuple[str, str], set[str]] = {}
    for index, item in ranked:
        bucket_key = (item["country"].casefold(), item.get("lang", ""))
        query = item["query"].casefold()
        if query not in bucket_queries.setdefault(bucket_key, set()):
            buckets.setdefault(bucket_key, []).append((index, item))
            bucket_queries[bucket_key].add(query)

    # Guarantee each target country and language gets a small initial slice;
    # spend the remaining cap on the globally highest-weight combinations.
    selected_indexes: set[int] = set()
    country_languages: list[list[tuple[str, str]]] = []
    for country in countries:
        prefix = country.casefold()
        langs = list(dict.fromkeys(
            lang for (bucket_country, lang) in buckets if bucket_country == prefix
        ))
        country_languages.append([(prefix, lang) for lang in langs])
    initial_slice = min(3, max(1, limit // max(1, len(country_languages))))
    max_languages = max((len(group) for group in country_languages), default=0)

    def select_round(language_index: int, slice_index: int) -> None:
        for language_buckets in country_languages:
            if language_index >= len(language_buckets):
                continue
            bucket = buckets.get(language_buckets[language_index], [])
            if slice_index >= len(bucket) or len(selected_indexes) >= limit:
                continue
            index, _item = bucket[slice_index]
            selected_indexes.add(index)

    for language_index in range(max_languages):
        select_round(language_index, 0)
    # Keep the established discovery facets represented when there is enough
    # budget after the first country/language pass.
    covered_intents = {
        items[index].get("intent") for index in selected_indexes
    }
    for index, item in ranked:
        intent = item.get("intent")
        if intent in {
            "sponsorship", "international", "work_permit", "relocation",
            "skill", "software", "industry", "seniority", "internship",
        } and intent not in covered_intents and len(selected_indexes) < limit:
            selected_indexes.add(index)
            covered_intents.add(intent)
    for language_index in range(max_languages):
        for slice_index in range(1, initial_slice):
            select_round(language_index, slice_index)
    for index, _item in ranked:
        if len(selected_indexes) >= limit:
            break
        selected_indexes.add(index)
    return [item for index, item in ranked if index in selected_indexes]


class SearchPlan:
    def __init__(self, prefs: Preferences, profile=None, cache_path=None, vocab=None):
        self.prefs = prefs
        self.vocab = vocab or CandidateVocabulary(profile=profile, prefs=prefs, cache_path=cache_path)

    def build(self, max_per_country: int = 3, max_queries_per_run: int | None = None) -> list[dict]:
        def core_for(country: str, lang: str, seen: set[tuple[str, str]]) -> list[dict]:
            items: list[tuple[float, dict]] = []
            local_seen = set(seen)
            for term in self.vocab.roles()[:max_per_country]:
                for intent, weight, builder in INTENT_TEMPLATES:
                    query = builder(term, country, lang)
                    if (query.lower(), country.lower()) in local_seen:
                        continue
                    local_seen.add((query.lower(), country.lower()))
                    items.append((weight, {
                        "query": query, "location": country, "country": country,
                        "lang": lang, "intent": intent, "weight": weight,
                    }))
            for term in self.vocab.country_terms(country)[:max_per_country]:
                query = LOCAL_INTENT[2](term, country, lang)
                if (query.lower(), country.lower()) in local_seen:
                    continue
                local_seen.add((query.lower(), country.lower()))
                items.append((LOCAL_INTENT[1], {
                    "query": query, "location": country, "country": country,
                    "lang": lang, "intent": LOCAL_INTENT[0], "weight": LOCAL_INTENT[1],
                }))
            languages = LANGS_OF_COUNTRY.get(country, (lang,))
            for local_lang in languages:
                local_terms = self.vocab.localized_terms(local_lang)
                for term in local_terms[:max_per_country]:
                    query = term
                    if (query.lower(), country.lower()) in local_seen:
                        continue
                    local_seen.add((query.lower(), country.lower()))
                    items.append((LOCAL_INTENT[1], {
                        "query": query,
                        "location": country,
                        "country": country,
                        "lang": local_lang,
                        "intent": LOCAL_INTENT[0],
                        "weight": LOCAL_INTENT[1],
                    }))
                for query in INTERNSHIP_SEARCHES.get(local_lang, []):
                    if (query.lower(), country.lower()) in local_seen:
                        continue
                    local_seen.add((query.lower(), country.lower()))
                    items.append((0.92, {
                        "query": query,
                        "location": country,
                        "country": country,
                        "lang": local_lang,
                        "intent": "internship",
                        "opportunity_type": "INTERNSHIP",
                        "weight": 0.92,
                    }))
            items.sort(key=lambda pair: pair[0], reverse=True)
            return [item for _w, item in items]

        plan: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for country in self.prefs.countries:
            lang = LANG_OF_COUNTRY.get(country, "en")
            for item in core_for(country, lang, seen):
                seen.add((item["query"].lower(), country.lower()))
                plan.append(item)

            # Broad structured searches: title + skill/software/industry/
            # seniority, then selected city terms.  These are lower-weight
            # than exact roles, so a tight budget retains the focused plan.
            roles = self.vocab.roles()[:max_per_country]
            combination_sources = (
                ("skill", self.vocab.terms_for("skills")[:2]),
                ("software", self.vocab.terms_for("software")[:2]),
                ("industry", self.vocab.terms_for("industries")[:2]),
                ("seniority", self.vocab.terms_for("seniority")[:2]),
                ("internship", ["stage", "internship"]),
            )
            template_by_intent = {name: (weight, builder) for name, weight, builder in COMBINATION_INTENTS}
            for role in roles:
                for intent, values in combination_sources:
                    weight, builder = template_by_intent[intent]
                    for value in values:
                        query = builder(role, value)
                        if (query.lower(), country.lower()) in seen:
                            continue
                        seen.add((query.lower(), country.lower()))
                        plan.append({"query": query, "location": country, "country": country,
                                     "lang": lang, "intent": intent, "weight": weight})
                for city in self.vocab.country_locations(country)[:max_per_country]:
                    query = role
                    key = (f"{query}|{city}".lower(), country.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    plan.append({"query": query, "location": city, "country": country,
                                 "lang": lang, "intent": "city", "weight": 0.82})

        # Opportunistic categories (§5) only fill leftover budget headroom.
        opportunistic: list[dict] = []
        for country in self.prefs.countries:
            lang = LANG_OF_COUNTRY.get(country, "en")
            for term in self.vocab.roles()[:max_per_country]:
                for intent, weight, builder in OPPORTUNISTIC_INTENTS:
                    query = builder(term, country, lang)
                    if (query.lower(), country.lower()) in seen:
                        continue
                    seen.add((query.lower(), country.lower()))
                    opportunistic.append({"query": query, "location": country,
                                          "country": country, "lang": lang,
                                          "intent": intent, "weight": weight})

        plan.sort(key=lambda item: item["weight"], reverse=True)
        opportunistic.sort(key=lambda item: item["weight"], reverse=True)
        if max_queries_per_run:
            plan = _fair_cap(plan, max_queries_per_run, self.prefs.countries)
            plan.extend(opportunistic[: max(0, max_queries_per_run - len(plan))])
        else:
            plan.extend(opportunistic)
        return plan

    def plan_for_country(self, country: str, max_per_country: int = 3) -> list[dict]:
        sub = self.prefs.model_copy(update={"target_countries": [country]})
        return SearchPlan(sub, vocab=self.vocab).build(max_per_country)
