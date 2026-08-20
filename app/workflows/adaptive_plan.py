"""Adaptive search schedule (spec §12, §24, §25, §26, §31).

Wraps the static `SearchPlan` with the agent's learned history:

  * country ranking (§12) orders the schedule by preference affinity;
  * query learning (§24/§31) repeats high-performing queries and down-weights
    poor ones;
  * the daily budget (§25) caps how many queries run today.

Isolation guarantee: with an empty ledger and no budget cap, the output is
byte-for-byte the baseline plan — so hermetic tests without query history are
unaffected.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import AgentConfig, CandidateProfile, Preferences
from app.discovery.country_ranking import rank_countries
from app.discovery.query_learning import budget_remaining, repeats_for
from app.discovery.vocabulary import CandidateVocabulary
from app.memory.store import aggregate_query_stat
from app.workflows.search_plan import SearchPlan


def build_adaptive_plan(session: Session, prefs: Preferences, config: AgentConfig,
                        *, vocab: CandidateVocabulary | None = None,
                        profile: CandidateProfile | None = None,
                        max_per_country: int = 3,
                        max_queries_per_run: int | None = None,
                        learning_sources: set[str] | None = None) -> list[dict]:
    """Build the run's search schedule from the baseline plan + learned history."""
    base = SearchPlan(prefs, vocab=vocab).build(max_per_country=max_per_country,
                                                max_queries_per_run=max_queries_per_run)
    dcfg = config.discovery or {}
    ranking = {cs.country: idx for idx, cs in enumerate(
        rank_countries(prefs.countries, prefs, profile, session=session,
                       weights=dcfg.get("country_ranking_weights")))}
    has_history = any(aggregate_query_stat(session, item["query"], item["country"], learning_sources) is not None
                      for item in base)
    remaining = budget_remaining(session, int(dcfg.get("max_daily_search_queries") or 0))

    # nothing learned, unlimited budget -> exact legacy behaviour
    if not has_history and remaining is None:
        return base

    expanded: list[tuple[int, dict]] = []
    for idx, item in enumerate(base):
        stat = aggregate_query_stat(session, item["query"], item["country"], learning_sources)
        for _ in range(repeats_for(stat)):
            expanded.append((idx, item))

    # Keep exploring every target country even when old zero-result searches
    # were down-weighted.  Otherwise a new source, a changed labour market, or
    # a newly added role can be starved forever by its historical ledger.
    exploration_per_country = int(dcfg.get("min_exploration_per_country", 0))
    if exploration_per_country > 0:
        present = {(item["query"], item["country"]) for _idx, item in expanded}
        exploration_counts: dict[str, int] = {}
        for idx, item in enumerate(base):
            country = item["country"]
            key = (item["query"], country)
            if exploration_counts.get(country, 0) >= exploration_per_country:
                continue
            if key not in present:
                expanded.append((idx, item))
                present.add(key)
            exploration_counts[country] = exploration_counts.get(country, 0) + 1

    if remaining is not None:
        expanded = expanded[:max(0, remaining)]

    expanded.sort(key=lambda pair: (ranking.get(pair[1]["country"], 99), pair[0]))
    plan = [item for _idx, item in expanded]
    if max_queries_per_run:
        plan = plan[:max_queries_per_run]
    return plan
