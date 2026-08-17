"""Query learning + adaptive search schedule tests — Phase 7 (spec §24, §25, §26, §31).

Covers the value/repeat functions, the daily discovery budget, and the adaptive
plan: with an empty ledger it is byte-identical to the baseline plan (hermetic),
and with history/budget it expands, down-weights and caps accordingly.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app import memory as mem
from app import models
from app.discovery.query_learning import (
    budget_remaining,
    daily_queries_used,
    query_value,
    repeats_for,
)
from app.workflows.adaptive_plan import build_adaptive_plan
from app.workflows.discovery import run_discovery
from app.workflows.search_plan import SearchPlan

# ---- query_value / repeats_for (§31) --------------------------------------------


def test_query_value_defaults_to_middle_without_history():
    assert query_value(None) == 0.5


def test_query_value_rises_with_relevance():
    class _St:
        runs, relevant_jobs, applications, responses = 1, 2, 0, 0

    assert query_value(_St()) > 0.5


def test_query_value_falls_below_middle_for_zero_yield():
    class _St:
        runs, relevant_jobs, applications, responses = 2, 0, 0, 0

    assert query_value(_St()) < 0.5
    assert repeats_for(_St()) == 0  # poor query skipped but never deleted (§31)


def test_repeats_for():
    assert repeats_for(None) == 1
    assert repeats_for(type("_", (), {"runs": 0})()) == 1

    class _Good:
        runs, relevant_jobs, applications, responses = 1, 3, 0, 0

    assert repeats_for(_Good()) == 2

    class _Fair:
        runs, relevant_jobs, applications, responses = 2, 1, 0, 0  # rel 0.5 -> 0.6

    assert repeats_for(_Fair()) == 1


# ---- budget (§25) -----------------------------------------------------------------


def test_budget_unlimited_when_cap_absent(db):
    assert budget_remaining(db, None) is None
    assert budget_remaining(db, 0) is None


def test_budget_uses_ledger_today(db):
    mem.store.record_query(db, "q1", "France", source="src", jobs_found=5, relevant_jobs=3)
    assert daily_queries_used(db) == 1  # 1 run today
    assert budget_remaining(db, 10) == 9


# ---- adaptive plan (§12, §24, §25, §26) -------------------------------------------


def _base(prefs, max_queries=None):
    return SearchPlan(prefs).build(max_queries_per_run=max_queries)


def test_adaptive_matches_baseline_with_no_history(db, config, prefs):
    assert build_adaptive_plan(db, prefs, config, max_queries_per_run=None) == _base(prefs)


def test_adaptive_multiplies_good_queries(db, config, prefs):
    base = _base(prefs)
    good = base[0]
    mem.store.record_query(db, good["query"], good["country"], source="test",
                           jobs_found=5, relevant_jobs=5)
    adaptive = build_adaptive_plan(db, prefs, config, max_queries_per_run=None)
    assert len(adaptive) == len(base) + 1  # the good query is scheduled twice


def test_adaptive_skips_poor_queries(db, config, prefs):
    base = _base(prefs)
    poor = base[1]
    mem.store.record_query(db, poor["query"], poor["country"], source="test")  # zero yield
    adaptive = build_adaptive_plan(db, prefs, config, max_queries_per_run=None)
    assert len(adaptive) == len(base) - 1
    assert not any(item["query"] == poor["query"] and item["country"] == poor["country"]
                   for item in adaptive)


def test_budget_caps_the_plan(db, config, prefs):
    cfg = config.model_copy(update={
        "discovery": {**config.discovery, "max_daily_search_queries": 3}})
    mem.store.record_query(db, "seed-lead", "France", source="x", jobs_found=1, relevant_jobs=1)
    remaining = budget_remaining(db, 3)  # 3 cap - 1 used today
    assert remaining == 2
    adaptive = build_adaptive_plan(db, prefs, cfg, max_queries_per_run=None)
    assert len(adaptive) == remaining


# ---- discovery records the ledger (§24) -------------------------------------------


def test_discovery_records_query_stats(db, config, prefs, profile):
    from pathlib import Path

    asyncio.run(run_discovery(
        db, config, prefs,
        sources_path=Path(__file__).parent / "fixtures" / "sources_demo.yaml",
        profile=profile))
    rows = db.execute(select(models.QueryStat)).scalars().all()
    assert rows
    assert all(r.runs >= 1 and r.jobs_found >= 0 and r.relevant_jobs >= 0 for r in rows)


# ---- post-discovery outcomes (§24) -------------------------------------------------


def test_query_outcome_counters_accumulate(db):
    mem.store.record_query(db, "technicien genie civil", "France", source="demo")
    mem.store.record_query_outcome(db, "technicien genie civil", "France", source="demo",
                                   applications=2, responses=1, interviews=1)
    st = mem.store.aggregate_query_stat(db, "technicien genie civil", "France")
    assert st.applications == 2
    assert st.responses == 1
    assert st.interviews == 1


def test_query_outcome_is_noop_without_ledger_row(db):
    mem.store.record_query_outcome(db, "phantom query", "France", source="x",
                                   applications=9, interviews=3)
    assert mem.store.get_query_stat(db, "phantom query", "France", "x") is None
