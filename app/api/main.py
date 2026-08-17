"""Web dashboard — FastAPI app.

Endpoints for monitoring, review, and control (pause/resume). UI is a single
self-contained page that polls the JSON endpoints. No write-through editing of
the profile yet (that is a Phase 5 item); listing + control today.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import memory as mem
from app import models
from app.database import get_db, init_db
from app.scheduler.control import is_paused, set_paused

init_db()

app = FastAPI(title="Worldwide Career Agent", version="0.1.0")
_static = Path(__file__).parent / "static"


@app.get("/")
def index():
    return FileResponse(_static / "dashboard.html")


@app.get("/api/stats")
def api_stats(db: Session = Depends(get_db)):
    st = mem.store.stats(db)
    st["paused"] = is_paused()
    return st


@app.get("/api/jobs", summary="List discovered jobs")
def api_jobs(db: Session = Depends(get_db),
             status: str = Query("", description="filter by status"),
             country: str = Query("", description="filter by country"),
             limit: int = Query(50, le=500)):
    q = select(models.Job).order_by(models.Job.discovered_at.desc())
    if status:
        q = q.where(models.Job.status == status)
    if country:
        q = q.where(models.Job.country == country)
    jobs = db.execute(q.limit(limit)).scalars().all()
    return [{
        "id": j.id, "title": j.title, "company": j.company.name if j.company else "",
        "location": j.location, "country": j.country, "status": j.status,
        "url": j.url, "score": _latest_score(db, j.id),
    } for j in jobs]


@app.get("/api/applications", summary="Application state machine view")
def api_applications(db: Session = Depends(get_db), limit: int = Query(50, le=500)):
    apps = db.execute(
        select(models.Application).order_by(models.Application.id.desc()).limit(limit)
    ).scalars().all()
    return [{
        "id": a.id, "job_id": a.job_id, "title": a.job.title if a.job else "",
        "status": a.status, "action": a.action, "score": a.score,
        "contact_email": a.contact_email, "sent_at": a.sent_at.isoformat() if a.sent_at else None,
        "follow_up_at": a.follow_up_at.isoformat() if a.follow_up_at else None,
        "follow_ups_sent": a.follow_ups_sent,
    } for a in apps]


@app.get("/api/immigration", summary="Verified immigration programs")
def api_immigration(db: Session = Depends(get_db), limit: int = Query(50, le=500)):
    rows = db.execute(
        select(models.ImmigrationProgram).order_by(models.ImmigrationProgram.id.desc()).limit(limit)
    ).scalars().all()
    return [{
        "id": r.id, "country": r.country, "program": r.program, "occupation": r.occupation,
        "eligibility": r.eligibility, "language_requirements": r.language_requirements,
        "official_source_url": r.official_source_url, "verified_at": r.verified_at.isoformat() if r.verified_at else None,
    } for r in rows]


@app.get("/api/immigration/facts", summary="Verified immigration facts (discovery §11)")
def api_immigration_facts(db: Session = Depends(get_db), limit: int = Query(50, le=500),
                          country: str = Query("", description="filter by target country")):
    stmt = select(models.ImmigrationFact).order_by(models.ImmigrationFact.id.desc()).limit(limit)
    if country:
        stmt = stmt.where(models.ImmigrationFact.country == country)
    rows = db.execute(stmt).scalars().all()
    return [{
        "id": r.id, "country": r.country, "program": r.program, "fact_type": r.fact_type,
        "claim": r.claim, "occupation": r.occupation, "source_url": r.source_url,
        "source_domain": r.source_domain, "confidence": r.confidence, "matched": r.matched,
        "retrieved_at": r.retrieved_at.isoformat() if r.retrieved_at else None,
    } for r in rows]


@app.get("/api/discovery/sources", summary="Discovered opportunity sources (§6)")
def api_opportunity_sources(db: Session = Depends(get_db), limit: int = Query(50, le=500),
                            kind: str = Query("", description="filter by source kind")):
    stmt = select(models.OpportunitySource).order_by(models.OpportunitySource.id.desc()).limit(limit)
    if kind:
        stmt = stmt.where(models.OpportunitySource.kind == kind)
    rows = db.execute(stmt).scalars().all()
    return [{
        "id": r.id, "kind": r.kind, "url": r.url, "title": r.title, "country": r.country,
        "source": r.source, "sponsorship_signal": r.sponsorship_signal,
        "international_recruitment_signal": r.international_recruitment_signal,
        "verification_status": r.verification_status,
        "discovered_at": r.discovered_at.isoformat(),
        "last_checked_at": r.last_checked_at.isoformat() if r.last_checked_at else None,
    } for r in rows]


@app.get("/api/events", summary="Recent audit events")
def api_events(db: Session = Depends(get_db), limit: int = Query(20, le=200)):
    rows = db.execute(select(models.Event).order_by(models.Event.id.desc()).limit(limit)).scalars().all()
    return [{"type": e.type, "level": e.level, "message": e.message,
             "at": e.occurred_at.isoformat()} for e in rows]


@app.post("/api/pause")
def api_pause():
    set_paused(True)
    return {"paused": True}


@app.post("/api/resume")
def api_resume():
    set_paused(False)
    return {"paused": False}


@app.get("/api/config", summary="Current scoring thresholds & weights")
def api_config():
    from app.config import get_config

    cfg = get_config()
    return {"score_thresholds": cfg.score_thresholds, "scoring_weights": cfg.scoring_weights}


def _latest_score(db: Session, job_id: int) -> float | None:
    d = mem.store.get_last_decision(db, job_id)
    return d.overall_score if d else None


app.mount("/static", StaticFiles(directory=str(_static)), name="static")
