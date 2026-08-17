"""Discovery V2 Phase 1: candidate vocabulary + intent-based query planning."""
from __future__ import annotations

import asyncio

from app.agents.llm import NullLLM
from app.discovery.vocabulary import LANG_OF_COUNTRY, CandidateVocabulary
from app.workflows.search_plan import SearchPlan

# ---- candidate vocabulary -----------------------------------------------------


def test_vocabulary_roles_expand_target_roles(profile, prefs):
    vocab = CandidateVocabulary(profile=profile, prefs=prefs)
    roles = vocab.roles()
    assert "civil engineering technician" in roles          # canonical role
    assert "civil engineering technologist" in roles        # synonym
    assert "junior civil engineering technician" in roles   # junior variant
    assert "AutoCAD" in roles                                # skill-derived term
    lowered = [r.lower() for r in roles]
    assert len(roles) == len(set(lowered)), "vocabulary must be deduplicated"


def test_vocabulary_localized_terms(profile, prefs):
    vocab = CandidateVocabulary(profile=profile, prefs=prefs)
    fr = vocab.localized_terms("fr")
    assert "technicien genie civil" in fr   # from preferences
    assert "technicien chantier" in fr      # from curated map
    de = vocab.localized_terms("de")
    assert "bautechniker" in de


def test_vocabulary_country_terms(profile, prefs):
    vocab = CandidateVocabulary(profile=profile, prefs=prefs)
    assert LANG_OF_COUNTRY["Germany"] == "de"
    assert LANG_OF_COUNTRY["France"] == "fr"
    assert "tiefbautechniker" in vocab.country_terms("Germany")


def test_vocabulary_llm_expansion_cached(profile, prefs, tmp_path):
    cache = tmp_path / "vocab_cache.json"
    vocab = CandidateVocabulary(profile=profile, prefs=prefs, cache_path=cache)
    llm = NullLLM(profile)
    expanded = asyncio.run(vocab.llm_expanded_roles(llm))
    assert expanded, "base vocabulary must still be returned when LLM adds nothing"
    assert cache.exists(), "LLM expansions must be cached on disk"
    # a second vocabulary with the same cache must hit the cache and stay stable
    again = CandidateVocabulary(profile=profile, prefs=prefs, cache_path=cache)
    expanded2 = asyncio.run(again.llm_expanded_roles(llm))
    assert expanded == expanded2


# ---- intent-based query planning ----------------------------------------------


def test_query_plan_budget_and_ranking(profile, prefs):
    plan = SearchPlan(prefs, profile=profile).build(max_per_country=2, max_queries_per_run=40)
    assert 0 < len(plan) <= 40, "global per-run budget must cap the plan"
    weights = [p["weight"] for p in plan]
    assert weights == sorted(weights, reverse=True), "plan must be ranked by weight"
    intents = {p["intent"] for p in plan}
    assert "role" in intents
    assert "local_language" in intents
    assert "sponsorship" in intents
    keys = [(p["query"].lower(), p["location"].lower()) for p in plan]
    assert len(keys) == len(set(keys)), "query×location combos must be unique"


def test_query_plan_backward_compatible_keys(profile, prefs):
    plan = SearchPlan(prefs, profile=profile).build(max_per_country=2)
    for p in plan:
        assert {"query", "location", "country", "lang"} <= set(p)
    assert "France" in {p["country"] for p in plan}


def test_query_plan_includes_localized_terms(profile, prefs):
    plan = SearchPlan(prefs, profile=profile).build(max_per_country=2, max_queries_per_run=200)
    de_local = [p for p in plan if p["country"] == "Germany" and p["intent"] == "local_language"]
    assert de_local, "Germany must get local_language queries"
    assert any(p["query"] == "bautechniker" for p in de_local)


def test_plan_for_country_scoped(profile, prefs):
    plan = SearchPlan(prefs, profile=profile).plan_for_country("France", max_per_country=2)
    assert plan
    assert all(p["country"] == "France" for p in plan)
