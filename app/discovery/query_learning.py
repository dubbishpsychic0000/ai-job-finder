"""Query learning (spec §24, §26, §31) — the agent learns from its search ledger.

Every (query, country, source) is tracked in `query_stats`. This module turns
that ledger into the scheduler's next decisions without ever deleting history:

  * `query_value`   — 0..1 performance from relevance + applications + replies;
  * `repeats_for`   — how many times a query runs this schedule (0 = down-weighted
                      but still recorded, never deleted — §31);
  * `budget_remaining` — the daily discovery budget (§25): cap<=0 means unlimited.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import QueryStat, utcnow


def query_value(st: QueryStat | None) -> float:
    """Outcome quality for a query.

    Skewed so that *zero* return (no relevant jobs ever found) scores below the
    0.5 mid-point and gets down-weighted to zero repetitions — while genuinely
    productive queries approach 1.0.
    """
    if not st or st.runs <= 0:
        return 0.5
    relevance = st.relevant_jobs / st.runs
    applications = st.applications / st.runs
    responses = st.responses / max(1, st.runs)
    value = 0.45 + 0.30 * min(relevance, 2.0) + 0.20 * min(applications, 1.0) \
        + 0.10 * min(responses, 1.0)
    return min(1.0, max(0.0, value))


def repeats_for(st: QueryStat | None) -> int:
    """How many times this (query, country) should appear in the schedule.

    Great queries run twice, average ones once, and poor ones are skipped for
    the run — but the row is kept (never lost, spec §31).
    """
    if not st or st.runs <= 0:
        return 1
    value = query_value(st)
    if value >= 0.85:
        return 2
    if value < 0.5:
        return 0
    return 1


def daily_queries_used(session: Session, now: datetime | None = None) -> int:
    """Number of queries already spent today, from the query ledger (§25)."""
    now = now or utcnow()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = session.execute(
        select(QueryStat).where(QueryStat.last_run_at >= day_start)
    ).scalars().all()
    return sum(r.runs for r in rows)


def budget_remaining(session: Session, cap: int | None = None,
                     now: datetime | None = None) -> int | None:
    """Remaining queries today against the daily budget. None/0 cap => unlimited."""
    if not cap or cap <= 0:
        return None
    return max(0, cap - daily_queries_used(session, now))
