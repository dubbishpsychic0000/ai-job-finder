"""Web dashboard — FastAPI app.

Endpoints for monitoring, review, and control (pause/resume). UI is a single
self-contained page that polls the JSON endpoints. No write-through editing of
the profile yet (that is a Phase 5 item); listing + control today.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import hmac

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
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


@app.get("/webhooks/whatsapp", include_in_schema=False)
def verify_whatsapp_webhook(mode: str = Query("", alias="hub.mode"),
                            verify_token: str = Query("", alias="hub.verify_token"),
                            challenge: str = Query("", alias="hub.challenge")):
    """Meta callback handshake; deploy this endpoint over public HTTPS."""
    from app.config import get_settings

    expected = get_settings().whatsapp_webhook_verify_token
    if mode == "subscribe" and expected and hmac.compare_digest(verify_token, expected):
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="WhatsApp webhook verification failed")


@app.post("/webhooks/whatsapp", include_in_schema=False)
async def receive_whatsapp_status(request: Request, db: Session = Depends(get_db)):
    """Record Meta sent/delivered/read/failed receipts for troubleshooting."""
    from app.config import get_settings

    settings = get_settings()
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not settings.whatsapp_app_secret:
        raise HTTPException(status_code=503, detail="WhatsApp webhook app secret is not configured")
    expected = "sha256=" + hmac.new(settings.whatsapp_app_secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=403, detail="invalid WhatsApp webhook signature")
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    statuses = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            statuses.extend((change.get("value") or {}).get("statuses") or [])
    for status in statuses:
        message_id = str(status.get("id", ""))
        state = str(status.get("status", "unknown"))
        error = ((status.get("errors") or [{}])[0].get("title", ""))
        mem.store.record_event(db, "whatsapp_delivery", f"{state}: {message_id[:80]}",
                               "error" if state == "failed" else "info",
                               {"message_id": message_id, "status": state, "error": error})
    return {"received": len(statuses)}


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
        "opportunity_id": mem.store.opportunity_id(j), "opportunity_type": j.opportunity_type,
        "application_method": j.application_method, "application_url": j.application_url,
    } for j in jobs]


@app.get("/api/jobs/{opportunity_id}", summary="Complete opportunity details")
def api_job_details(opportunity_id: str, db: Session = Depends(get_db)):
    from fastapi import HTTPException

    from app.notifications.service import NotificationService

    result = NotificationService(db).details(opportunity_id)
    if not result:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return result


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


@app.get("/api/analytics", summary="Discovery analytics (§30): ranking, learning, health")
def api_analytics(db: Session = Depends(get_db)):
    from app.config import ROOT_DIR, get_preferences, get_profile, load_yaml
    from app.discovery.query_learning import query_value

    jobs_by_country: dict[str, int] = {}
    for row in db.execute(
            select(models.Job.country, models.Job.id).where(models.Job.country != "")):
        jobs_by_country[row[0]] = jobs_by_country.get(row[0], 0) + 1

    countries, seen = [], set()
    for cs in _ranked_countries(get_preferences, get_profile):
        seen.add(cs.country.lower())
        countries.append({"country": cs.country, "score": cs.score,
                          "reasons": cs.reasons, "jobs": jobs_by_country.get(cs.country, 0),
                          "rank": len(countries) + 1})
    for country, n in sorted(jobs_by_country.items(), key=lambda kv: -kv[1]):
        if country.lower() not in seen:
            countries.append({"country": country, "score": 0.0, "reasons": [],
                              "jobs": n, "rank": len(countries) + 1})

    top = mem.store.best_queries(db, limit=25)
    queries = [{"query": q.query, "country": q.country, "source": q.source,
                "runs": q.runs, "jobs_found": q.jobs_found, "relevant_jobs": q.relevant_jobs,
                "applications": q.applications, "responses": q.responses,
                "interviews": q.interviews,  # §24 — interview callbacks per query
                "value": round(query_value(q), 3)} for q in top]

    try:
        configured = {c.get("name"): c for c in load_yaml(ROOT_DIR / "config" / "sources.yaml").get("connectors", [])}
    except (OSError, ValueError):
        configured = {}
    src_rows = db.execute(select(models.Source).order_by(models.Source.id)).scalars().all()
    source_health = []
    for s in src_rows:
        if s.last_error:
            health = "error"
        elif s.last_fetch_at:
            health = "healthy"
        else:
            health = "unknown"
        query_rows = db.execute(
            select(models.QueryStat).where(models.QueryStat.source == s.name)
        ).scalars().all()
        source_health.append({
            "name": s.name, "kind": s.kind, "enabled": s.enabled,
            "mode": configured.get(s.name, {}).get("mode", "unknown"),
            "items_found": s.items_found,
            "queries": sum(q.runs for q in query_rows),
            "normalized": sum(q.jobs_found for q in query_rows),
            "new": sum(q.relevant_jobs for q in query_rows),
            "last_fetch_at": s.last_fetch_at.isoformat() if s.last_fetch_at else None,
            "last_success_at": s.last_success_at.isoformat() if s.last_success_at else None,
            "last_failure_at": s.last_failure_at.isoformat() if s.last_failure_at else None,
            "rate_limit_status": s.rate_limit_status or "ok",
            "last_error": s.last_error, "health": health,  # §27 — connector health detail
        })

    return {"stats": mem.store.stats(db), "countries": countries,
            "top_queries": queries, "source_health": source_health}


def _ranked_countries(get_preferences_fn, get_profile_fn):
    """Country ranking for analytics — never crashes when the user prefs are absent."""
    try:
        prefs = get_preferences_fn()
        target = list(prefs.countries)
    except Exception:
        target = []
    try:
        profile = get_profile_fn()
    except Exception:
        profile = None
    from app.discovery.country_ranking import rank_countries

    return rank_countries(target, prefs if target else None, profile)


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
