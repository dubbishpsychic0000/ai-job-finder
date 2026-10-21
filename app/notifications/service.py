"""Notification queue, digest rendering, and full opportunity detail lookup."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import memory as mem
from app.config import ROOT_DIR, load_yaml
from app.discovery.opportunity_details import is_verified_individual_vacancy
from app.models import Job, Notification, utcnow

logger = logging.getLogger(__name__)


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

    def send_portal_application_alert(self, job, provider, *, now=None) -> dict[str, str]:
        """Send one verified portal-only vacancy alert, never retrying ambiguity."""
        from sqlalchemy.exc import IntegrityError

        from app.agents.decision_agent import DecisionAgent
        from app.config import get_config, get_preferences, get_profile
        from app.email.application_queue import _profile_eligibility_issue
        from app.workflows.action import lang_for_country

        now = now or utcnow()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        metadata = getattr(job, "public_competition", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        is_public_competition = (
            getattr(job, "source", "") == "emploi_public_ma"
            or metadata.get("source") == "emploi_public_ma"
        )
        route = metadata.get("submission_route") or {}
        if not is_public_competition:
            route = {
                "kind": "ONLINE_PORTAL",
                "destination_url": getattr(job, "application_url", ""),
                "evidence_url": getattr(job, "url", ""),
            }
        assessment = metadata.get("eligibility_assessment") or {}
        official_source_url = str(getattr(job, "url", "") or "")
        route_evidence_url = str(
            route.get("evidence_url") or (
                official_source_url if not is_public_competition else ""
            )
        )
        portal_url = str(
            route.get("destination_url") or getattr(job, "application_url", "")
        )
        try:
            official_host = (urlsplit(official_source_url).hostname or "").casefold()
            route_evidence_parts = urlsplit(route_evidence_url)
            route_evidence_host = (route_evidence_parts.hostname or "").casefold()
            portal_parts = urlsplit(portal_url)
            portal_host = (portal_parts.hostname or "").casefold()
            portal_valid = (
                portal_parts.scheme == "https"
                and portal_parts.username is None
                and portal_parts.password is None
                and portal_parts.port in {None, 443}
                and bool(portal_host)
            )
        except ValueError:
            official_host = route_evidence_host = ""
            portal_valid = False

        if is_public_competition:
            route_evidence_valid = (
                official_host in {"emploi-public.ma", "www.emploi-public.ma"}
                and route_evidence_host in {"emploi-public.ma", "www.emploi-public.ma"}
                and route_evidence_parts.path.startswith("/fr/concours/download/")
                and bool(metadata.get("official_portal_verified"))
                and bool(assessment.get("relevant"))
            )
        else:
            route_evidence_valid = (
                getattr(job, "source_type", "") == "ats"
                and route_evidence_url == official_source_url
                and getattr(job, "application_url", "") == portal_url
            )
            if route_evidence_valid:
                config = get_config()
                profile = get_profile()
                preferences = get_preferences()
                decision = mem.store.get_last_decision(self.session, job.id)
                analysis = mem.store.get_analysis(self.session, job.id)
                if decision is None or decision.decision != "APPLY" or analysis is None:
                    route_evidence_valid = False
                else:
                    hard_block, _ = DecisionAgent(
                        llm=None,
                        config=config,
                        profile=profile,
                        preferences=preferences,
                    ).hard_rules(
                        self.session,
                        job,
                        decision.overall_score,
                        config.threshold_for(decision.overall_score),
                    )
                    eligibility_issue = _profile_eligibility_issue(
                        job, analysis, profile, preferences,
                    )
                    if hard_block or eligibility_issue:
                        route_evidence_valid = False
                    else:
                        requirements = "; ".join(
                            value for value in (
                                str(getattr(analysis, "education_required", "") or ""),
                                (
                                    f"minimum experience: {analysis.experience_min} year(s)"
                                    if analysis.experience_min is not None else ""
                                ),
                                ", ".join(getattr(analysis, "skills_required", []) or []),
                            ) if value
                        ) or "See the verified posting."
                        assessment = {
                            "relevant": True,
                            "requirements_summary": requirements,
                            "summary": str(decision.reason or "Candidate/job requirements assessed as suitable."),
                        }
                        documents = []
                        description = str(getattr(job, "description", "") or "").casefold()
                        if any(term in description for term in ("cv", "resume", "curriculum vitae")):
                            from app.email.service import resolve_attachment

                            cv_path = resolve_attachment(
                                Path("candidate") / "cv",
                                lang_for_country(str(getattr(job, "country", "") or "")),
                            )
                            documents.append({
                                "name": "CV",
                                "available_locally": bool(cv_path and Path(cv_path).is_file()),
                            })
                        if any(term in description for term in ("diploma", "degree certificate")):
                            found_diploma = any(
                                any(term in path.name.casefold() for term in ("diplom", "degree"))
                                for path in (Path("candidate").rglob("*") if Path("candidate").is_dir() else [])
                                if path.is_file()
                            )
                            documents.append({
                                "name": "Diploma copy",
                                "available_locally": found_diploma,
                            })
                        metadata = {
                            **metadata,
                            "required_documents": documents,
                        }

        if (
            (is_public_competition and metadata.get("status") != "OPEN_ANNOUNCEMENT")
            or getattr(job, "status", "") in {"closed", "ignored"}
            or getattr(job, "verification_status", "") != "verified"
            or getattr(job, "application_method", "") != "COMPANY_PORTAL"
            or route.get("kind") != "ONLINE_PORTAL"
            or not is_verified_individual_vacancy(job)
            or not route_evidence_valid
            or not portal_valid
        ):
            return {"status": "skipped", "reason": "verified_relevant_portal_evidence_required"}
        if getattr(job, "closing_at", None) is not None:
            deadline = job.closing_at
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if deadline <= now:
                return {"status": "skipped", "reason": "application_deadline_elapsed"}

        existing = self.session.execute(
            select(Notification).where(
                Notification.event_type == "PORTAL_APPLICATION_REQUIRED",
                Notification.job_id == job.id,
            ).limit(1),
        ).scalar_one_or_none()
        if existing is not None:
            return {
                "status": "already_attempted",
                "reason": str(existing.payload.get("outcome", existing.status)),
            }

        company = getattr(job, "company", None)
        employer = str(getattr(company, "name", "") or "Employer")
        reference = str(
            metadata.get("reference") or metadata.get("portal_id")
            or getattr(job, "external_id", "") or ""
        )
        official_url = portal_url
        deadline = getattr(job, "closing_at", None)
        deadline_text = deadline.strftime("%d %B %Y") if deadline else "not stated"
        days = max(0, (deadline.date() - now.date()).days) if deadline else None
        urgency = (
            f"{days} day(s) remaining"
            if days is not None else "deadline not stated"
        )
        requirements = str(
            assessment.get("requirements_summary")
            or metadata.get("eligibility_requirements")
            or "See the official notice."
        )
        match = str(
            assessment.get("summary")
            or "Eligibility requires review against the official notice."
        )
        documents = metadata.get("required_documents") or []
        documents_text = "; ".join(
            f"{str(doc.get('name', 'required document'))[:64]} "
            f"({'available locally' if doc.get('available_locally', False) else 'missing locally'})"
            for doc in documents if isinstance(doc, dict)
        ) or "No documents stated in the verified posting."
        fields = {
            "employer": employer[:100],
            "title": str(job.title or "")[:120],
            "location": str(job.location or job.country or "")[:80],
            "reference": reference[:48],
            "deadline": deadline_text[:40],
            "urgency": urgency[:48],
            "requirements": requirements[:220],
            "match": match[:160],
            "documents": documents_text[:180],
        }

        def render_message() -> str:
            return (
                f"{fields['employer']} — {fields['title']}; {fields['location']}; "
                f"ref {fields['reference'] or 'not stated'}. "
                f"Deadline: {fields['deadline']} ({fields['urgency']}). "
                f"Eligibility: {fields['requirements']}. "
                f"Assessment: {fields['match']}. "
                f"Required documents: {fields['documents']}. "
                "This vacancy requires an application through the official portal. "
                f"You should apply here: {official_url}."
            )

        message = render_message()
        for field_name, minimum, step in (
            ("requirements", 60, 40),
            ("match", 45, 30),
            ("documents", 45, 30),
            ("title", 50, 30),
            ("employer", 40, 25),
            ("location", 30, 25),
        ):
            while len(message) > 1024 and len(fields[field_name]) > minimum:
                fields[field_name] = fields[field_name][:-step]
                message = render_message()
        if len(message) > 1024:
            return {
                "status": "skipped",
                "reason": "verified_alert_exceeds_whatsapp_template_limit",
            }
        row = Notification(
            event_type="PORTAL_APPLICATION_REQUIRED",
            job_id=job.id,
            priority="urgent",
            payload={"message": message, "outcome": "dispatching"},
            status="dispatching",
        )
        try:
            with self.session.begin_nested():
                self.session.add(row)
                self.session.flush()
        except IntegrityError:
            return {"status": "already_attempted", "reason": "deduplicated"}

        # Persist the send intent before contacting Meta so a crash never
        # triggers an automatic duplicate alert on a later workflow run.
        self.session.commit()
        if not getattr(provider, "configured", False):
            row.status = "failed"
            row.payload = {
                "outcome": "whatsapp_not_configured",
                "message": message,
            }
            self.session.commit()
            return {
                "status": "failed",
                "reason": "whatsapp_not_configured",
                "message": message,
            }

        try:
            accepted = provider.send(message)
        except Exception as exc:
            logger.exception("WhatsApp portal alert transport raised (%s)", type(exc).__name__)
            accepted = False
            failure_reason = "provider_delivery_raised"
        else:
            failure_reason = "provider_delivery_failed"
        if accepted:
            row.status = "accepted"
            row.payload = {
                "outcome": "accepted_by_meta_delivery_unconfirmed",
                "message": message,
                "provider_message_id": str(getattr(provider, "last_message_id", "") or ""),
            }
            self.session.commit()
            return {
                "status": "accepted_by_meta_delivery_unconfirmed",
                "message": message,
            }
        row.status = "failed"
        row.payload = {"outcome": failure_reason, "message": message}
        self.session.commit()
        return {
            "status": "failed",
            "reason": failure_reason,
            "message": message,
        }

    def alert_verified_portal_jobs(self, provider, *, now=None) -> list[dict[str, str]]:
        jobs = self.session.scalars(select(Job).order_by(Job.id)).all()
        outcomes = []
        for job in jobs:
            result = self.send_portal_application_alert(job, provider, now=now)
            if result.get("status") != "skipped":
                outcomes.append({
                    "job_id": mem.store.opportunity_id(job),
                    **result,
                })
        return outcomes

    def digest_due(self, now=None) -> bool:
        now = now or utcnow()
        minutes = int((self.config.get("digest") or {}).get("interval_minutes", 180))
        last = self.session.execute(select(Notification.delivered_at).where(
            Notification.status == "delivered"
        ).order_by(Notification.delivered_at.desc())).scalars().first()
        # SQLite returns ``DateTime`` values without tzinfo even when the
        # application wrote UTC-aware datetimes.  Treat such persisted values
        # as UTC so scheduled notification checks work on both SQLite and
        # PostgreSQL.
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
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
                (row.event_type == "EMAIL_DRAFT_CREATED" and row.priority in {"high", "urgent"}) or
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
            if row.payload.get("daily_limit_reached"):
                return f"⚠️ DAILY EMAIL LIMIT REACHED — Gmail draft created for {job.title if job else ''}"
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
