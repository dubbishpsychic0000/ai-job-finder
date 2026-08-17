"""Deduplication layer.

Guards the jobs table against:
  * the same external posting re-discovered on every run,
  * the same role cross-posted on several boards (different external_ids),
  * reposted/similar roles within a freshness window.

Strategy — deterministic key first, then a cheap fuzzy pass:
  1) exact `dedup_key` match (already in the DB)  -> duplicate
  2) normalized title + company + country pair seen recently -> strong duplicate
  3) same company, title overlapping in the last N days -> probable duplicate

Anything uncertain is returned as `probable` so callers may choose to keep it
(scoring is cheap) without sending duplicate applications.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.models import utcnow

REPOST_FRESHNESS_DAYS = 30


def find_duplicates(session: Session, opportunities) -> dict[int, str]:
    """Return {idx: 'exact'|'probable'} for opportunities already seen."""
    dup: dict[int, str] = {}
    now = utcnow()
    cutoff = now - timedelta(days=REPOST_FRESHNESS_DAYS)
    for idx, opp in enumerate(opportunities):
        key = opp.dedup_key()

        existing = session.execute(
            select(models.Job).where(models.Job.dedup_key == key)
        ).scalar_one_or_none()
        if existing:
            dup[idx] = "exact"
            continue

        # §21 canonical identity: same vacancy posted on another board/ATS/agency
        # collapses onto the stored copy regardless of external_id or source.
        canonical = opp.canonical_job_id()
        existing_canon = session.execute(
            select(models.Job).where(
                models.Job.canonical_job_id == canonical,
                models.Job.canonical_job_id != "",
            )
        ).scalar_one_or_none()
        if existing_canon:
            dup[idx] = "exact"
            continue

        # fuzzy: normalized title prefix + same company within freshness window
        title_prefix = (opp.title or "")[:24].strip()
        filet = session.execute(
            select(models.Job).where(
                models.Job.discovered_at >= cutoff,
                models.Job.title.ilike(f"%{title_prefix}%"),
                models.Job.company_id.isnot(None),
            )
        ).scalars().all()
        for job in filet:
            if _same_company(session, job.company_id, opp.company):
                dup[idx] = "probable"
                break
    return dup


def _same_company(session: Session, company_id: int | None, name: str) -> bool:
    if not company_id or not name:
        return False
    company = session.get(models.Company, company_id)
    if not company:
        return False
    norm = _norm(name)
    key = _norm(company.normalized_name)
    return norm and (key == norm or key[:12] == norm[:12])


def _norm(text: str) -> str:
    import re
    import unicodedata

    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.sub(r"[^a-zA-Z0-9]", "", t.lower())
