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
