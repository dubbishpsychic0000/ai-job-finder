"""Follow-up workflow — polite, capped reminders for sent applications.

State-machine discipline: an application may only be re-contacted when
  * it is in a sent/replied state,
  * follow_up_at has passed,
  * follow_ups_sent < max_follow_ups_per_application.

The cooldown check is intentionally skipped for the SAME application/address,
but a hard cap is enforced — the exact scenario the safety gate exists for.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session

from app import memory as mem
from app.config import AgentConfig, RunnerSettings
from app.email.safety_gate import validate
from app.email.service import resolve_attachment as _resolve_attachment

logger = logging.getLogger(__name__)


@dataclass
class FollowUpReport:
    sent: list[int] = field(default_factory=list)
    blocked: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def run_follow_ups(session: Session, config: AgentConfig, settings: RunnerSettings,
                   communicator) -> FollowUpReport:
    report = FollowUpReport()
    due = mem.store.applications_due_for_followup(session)
    for app in due:
        job = app.job
        if not job:
            continue
        days = int(config.rules.get("follow_up_days", [7])[0])
        body = _followup_body(app)
        attachment = _resolve_attachment_from(app)
        check = validate(
            to_addr=app.contact_email,
            subject=f"Re: Application — {job.title}",
            body=body,
            attachments=([attachment] if attachment else []),
            job=job,
            profile=communicator.profile,
            config=config,
            session=session,
            daily_sent=0,
            check_cooldown=False,
        )
        email = mem.store.add_email(session, app.id, app.contact_email, f"Re: Application — {job.title}",
                                    body, [attachment] if attachment else [], status="validated")
        email.validation_log = check.reasons
        if not check.allowed:
            email.status = "blocked"
            report.blocked.append(app.id)
            mem.store.record_event(session, "followup", f"blocked: {check.reasons}", "error",
                                   {"application_id": app.id, "job_id": job.id})
            continue

        from app.email import provider

        ok, _msg_id, err = provider.send(settings, to=app.contact_email,
                                         subject=f"Re: Application — {job.title}", body=body,
                                         attachments=[a for a in [attachment] if a])
        if ok:
            email.status = "sent"
            from app.models import utcnow

            email.sent_at = utcnow()
            app.follow_ups_sent += 1
            interval = days * (app.follow_ups_sent + 1)
            app.follow_up_at = utcnow() + timedelta(days=interval)
            report.sent.append(app.id)
            mem.store.record_event(session, "followup", f"follow-up sent for application #{app.id}",
                                   "info", {"application_id": app.id, "job_id": job.id})
        else:
            email.status = "failed"
            email.error = err
            report.errors.append(err)
    return report


def _followup_body(app) -> str:
    job_title = app.job.title if app.job else "your opening"
    return (
        f"Dear Hiring Team,\n\nI am following up on my application for the role of {job_title}.\n\n"
        "I remain very interested and available for an interview. "
        "Please let me know if you need any further information from my side.\n\n"
        "Kind regards,\nCandidate"
    )


def _resolve_attachment_from(app) -> str:
    from pathlib import Path

    lang = {"France": "fr", "Belgium": "fr", "Canada": "fr"}.get(app.job.country if app.job else "", "en")
    return _resolve_attachment(Path("candidate") / "cv", lang)
