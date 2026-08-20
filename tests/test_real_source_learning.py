"""A new real source must not inherit a demo source's zero-result penalty."""
from __future__ import annotations

from app import memory as mem
from app.workflows.adaptive_plan import build_adaptive_plan


def test_new_real_source_has_a_fresh_learning_ledger(db, config, prefs):
    baseline = build_adaptive_plan(db, prefs, config, max_queries_per_run=1,
                                   learning_sources={"public_web_search"})
    item = baseline[0]
    mem.store.record_query(db, item["query"], item["country"], source="demo_jobs")
    plan = build_adaptive_plan(db, prefs, config, max_queries_per_run=1,
                               learning_sources={"public_web_search"})
    assert plan == baseline


def test_exploration_floor_keeps_searching_all_countries(db, config, prefs):
    """Historical zero yields must not permanently silence a target country."""
    cfg = config.model_copy(update={"discovery": {**config.discovery,
                                                     "min_exploration_per_country": 2}})
    baseline = build_adaptive_plan(db, prefs, cfg, max_queries_per_run=None,
                                   learning_sources={"public_web_search"})
    for item in baseline:
        mem.store.record_query(db, item["query"], item["country"], source="public_web_search")
    plan = build_adaptive_plan(db, prefs, cfg, max_queries_per_run=None,
                               learning_sources={"public_web_search"})
    counts = {}
    for item in plan:
        counts[item["country"]] = counts.get(item["country"], 0) + 1
    assert all(counts.get(country, 0) >= 2 for country in prefs.countries)
