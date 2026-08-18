"""Notification queue, digest rendering, and full opportunity detail lookup."""
from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import memory as mem
from app.config import ROOT_DIR, load_yaml
from app.models import Notification, utcnow


class NotificationService:
    def __init__(self, session: Session, config_path: Path | None = None):
        self.session = session
        self.config = load_yaml(config_path or ROOT_DIR / "candidate" / "notification_config.yaml")

    def enqueue(self, event_type: str, *, job_id: int | None = None, priority: str = "normal",
                payload: dict[str, Any] | None = None) -> Notification:
        allowed = set(self.config.get("notification_events", []))
        if allowed and event_type not in allowed:
            raise ValueError(f"unsupported notification event: {event_type}")
        return mem.store.enqueue_notification(self.session, event_type, job_id=job_id,
                                              priority=priority, payload=payload)

    def immediate(self, sender: Callable[[str], bool] | None = None) -> list[str]:
        """Render only high-priority items and mark them delivered.

        Transport (WhatsApp, etc.) is deliberately outside this class, keeping
        the notification decision independent from the safety boundary.
        """
        settings = self.config.get("immediate", {})
        results: list[str] = []
        for row in mem.store.queued_notifications(self.session, priorities=("high", "urgent")):
            if not self._immediate_enabled(row, settings):
                continue
            message = self._render_event(row)
            results.append(message)
            if sender is None or sender(message):
                self._deliver(row)
        return results

    def digest_due(self, now=None) -> bool:
        now = now or utcnow()
        minutes = int((self.config.get("digest") or {}).get("interval_minutes", 180))
        last = self.session.execute(select(Notification.delivered_at).where(
            Notification.status == "delivered"
        ).order_by(Notification.delivered_at.desc())).scalars().first()
        return last is None or now - last >= timedelta(minutes=minutes)

    def digest(self, *, force: bool = False, sender: Callable[[str], bool] | None = None) -> str | None:
        if not force and not self.digest_due():
            return None
        rows = mem.store.queued_notifications(self.session)
        # High alerts may already have been delivered; don't repeat them.
        if not rows:
            return None
        lines = ["🤖 JOB AGENT — NEW OPPORTUNITIES", "", f"{len(rows)} new agent event(s)", ""]
        activity = {"jobs": 0, "online": 0, "no_email": 0, "drafts": 0, "sent": 0}
        for row in rows:
            lines.append(self._render_event(row, compact=True))
            if row.event_type in {"JOB_FOUND", "JOB_RANKED", "INTERNSHIP_FOUND"}:
                activity["jobs"] += 1
            if row.event_type == "ONLINE_APPLICATION_REQUIRED":
                activity["online"] += 1
            if row.event_type == "NO_EMAIL_FOUND":
                activity["no_email"] += 1
            if row.event_type == "EMAIL_DRAFT_CREATED":
                activity["drafts"] += 1
            if row.event_type == "EMAIL_SENT":
                activity["sent"] += 1
        lines.extend(["", "Agent activity", f"🆕 {activity['jobs']} opportunities", f"📧 {activity['drafts']} drafts created · {activity['sent']} emails sent", f"🌐 {activity['online']} online applications · ⚠️ {activity['no_email']} without verified email"])
        message = "\n".join(lines)[:int((self.config.get("whatsapp") or {}).get("max_message_length", 4096))]
        if sender is None or sender(message):
            for row in rows:
                self._deliver(row)
        return message

    def details(self, opportunity_id: str) -> dict[str, Any] | None:
        job = mem.store.get_job_by_opportunity_id(self.session, opportunity_id)
        if not job:
            return None
        analysis = mem.store.get_analysis(self.session, job.id)
        decision = mem.store.get_last_decision(self.session, job.id)
        verification = mem.store.get_verified_email(self.session, job.id)
        return {
            "opportunity_id": mem.store.opportunity_id(job), "title": job.title,
            "company": job.company.name if job.company else "", "location": job.location,
            "country": job.country, "description": job.description, "requirements": analysis.skills_required if analysis else [],
            "salary": job.salary or (analysis.salary_estimate if analysis else ""), "contract": job.employment_type,
            "posted_date": job.posted_at.isoformat() if job.posted_at else None,
            "application_deadline": job.closing_at.isoformat() if job.closing_at else None,
            "match_score": decision.overall_score if decision else None,
            "why_it_matches": decision.reason if decision else "", "opportunity_type": job.opportunity_type,
            "application_method": job.application_method, "email": verification.email if verification else "",
            "email_verification": verification.verification_method if verification else "none",
            "application_url": job.application_url or job.url, "source": job.url,
            "agent_decision": decision.decision if decision else "", "action_taken": job.status,
        }

    def _immediate_enabled(self, row: Notification, settings: dict) -> bool:
        return ((row.event_type == "EMAIL_SENT" and settings.get("application_sent", True)) or
                (row.event_type == "IMMIGRATION_OPPORTUNITY" and settings.get("immigration_alert", True)) or
                (row.event_type == "ACTION_FAILED" and settings.get("errors", True)) or
                (row.event_type == "JOB_RANKED" and settings.get("high_match", True)))

    @staticmethod
    def _deliver(row: Notification) -> None:
        row.status = "delivered"
        row.delivered_at = utcnow()

    def _render_event(self, row: Notification, compact: bool = False) -> str:
        job = mem.store.get_job(self.session, row.job_id) if row.job_id else None
        if row.event_type == "ONLINE_APPLICATION_REQUIRED":
            return f"🌐 ONLINE APPLICATION REQUIRED — {job.title if job else ''}\nNo email sent. Apply: {row.payload.get('url', '')}"
        if row.event_type == "NO_EMAIL_FOUND":
            return f"⚠️ NO EMAIL SENT — {job.title if job else ''}\nNo verified employer email found."
        if row.event_type == "EMAIL_SENT":
            return f"📤 EMAIL SENT — {job.title if job else ''}"
        if row.event_type == "EMAIL_DRAFT_CREATED":
            return f"📧 EMAIL DRAFT CREATED — {job.title if job else ''}"
        if not job:
            return f"{row.event_type}"
        score = row.payload.get("score", "")
        headline = "🎓 INTERNSHIP FOUND" if job.opportunity_type == "INTERNSHIP" else "🤖 OPPORTUNITY"
        if compact:
            return f"{headline}: {job.title} — {job.location or job.country} · Match: {score or '?'} · {mem.store.opportunity_id(job)}"
        return (f"{headline}\n\n{job.country} — {job.location}\n{job.title}\n"
                f"🏢 Company: {job.company.name if job.company else 'Unknown'}\n📊 Match: {score or '?'}\n"
                f"Application: {job.application_method}\nID: {mem.store.opportunity_id(job)}\n🔗 {job.application_url or job.url}")
