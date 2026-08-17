"""Application Engine — orchestrates generate -> validate -> save -> outbound.

Every outbound message passes through the Safety Gate first, then the send is
routed by the configured `email_mode`:

  dry_run  — generate + validate, record outcome, NEVER touch Gmail
  draft    — create a Gmail Draft (nothing is sent; a human reviews & sends)
  live     — actually send through Gmail

A message is recorded as `blocked` when any gate check fails, when the global
emergency pause is on, or when the master `ENABLE_EMAIL=false` switch blocks
Gmail interaction (dry_run mode is allowed while disabled — it is fully local).
Nothing is ever silently dropped: every communication and its outcome is stored.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app import memory as mem
from app.agents.communication_agent import CommunicationAgent
from app.config import AgentConfig, CandidateProfile, RunnerSettings
from app.email import provider
from app.email.safety_gate import validate
from app.models import utcnow

logger = logging.getLogger(__name__)

LANG_TO_CV = {
    "fr": ["CV_Omar_Benhamid_FR.pdf", "CV_Omar_Benhamid.pdf"],
    "en": ["CV_Omar_Benhamid_EN.pdf", "CV_Omar_Benhamid.pdf"],
    "de": ["CV_Omar_Benhamid_DE.pdf", "CV_Omar_Benhamid.pdf"],
    "nl": ["CV_Omar_Benhamid_NL.pdf", "CV_Omar_Benhamid.pdf"],
}

# Checks that may trip only because a daily outbound budget is spent. When these
# are the ONLY failures, live mode keeps the message as a Gmail draft instead of
# blocking it. Any content/recipient/cooldown/duplicate/freshness/attachment
# failure stays a hard block.
CAP_CHECKS = {"rate_limit", "daily_applications", "daily_inquiries"}


def _only_cap_blocked(report) -> bool:
    return bool(report.checks) and set(report.checks.keys()) <= CAP_CHECKS


def resolve_attachment(cv_dir: Path, lang: str) -> str:
    cv_dir = Path(cv_dir)
    for name in LANG_TO_CV.get(lang, LANG_TO_CV["en"]):
        p = cv_dir / name
        if p.exists():
            return str(p)
    return ""


class ApplicationEngine:
    def __init__(self, session: Session, config: AgentConfig, settings: RunnerSettings,
                 profile: CandidateProfile, communicator: CommunicationAgent,
                 verify_url_fn=None):
        self.session = session
        self.config = config
        self.settings = settings
        self.profile = profile
        self.communicator = communicator
        # §20 — re-verify the posting is still live before emailing. Injectable
        # for hermetic tests; `None` means "only verify when the safety gate
        # freshness rule is enabled".
        self.verify_url_fn = verify_url_fn

    # ------------------------------------------------------------------ UI
    async def run(self, job, decision, action: str, contact_email: str = "", target_language: str = "en") -> dict:
        """Execute an APPLY / ASK_EMPLOYER action for a job.

        Returns {sent, status, report/error, application_id, email_id,
                 message_id, draft_id}.
        """
        score = getattr(decision, "overall_score", 0)
        mode = self.settings.email_mode

        from app.scheduler.control import is_paused  # lazy: avoid import cycle

        if is_paused():
            return self._record_disabled(
                job, decision, action, score, contact_email,
                reason="global emergency pause is ON — outbound halted")

        # dry_run mode is fully local and safe even with ENABLE_EMAIL=false;
        # any Gmail interaction (draft/live) requires the master switch.
        if mode != "dry_run" and not self.settings.enable_email:
            return self._record_disabled(
                job, decision, action, score, contact_email,
                reason="ENABLE_EMAIL=false (dry-run)")

        attachment = resolve_attachment(Path("candidate") / "cv", target_language)
        if not attachment:
            app = mem.store.add_application(self.session, job.id, decision.id, action,
                                            score, contact_email)
            app.status = "blocked"
            mem.store.record_event(self.session, "action", "no CV available — application blocked",
                                   "error", {"job_id": job.id})
            return {"sent": False, "status": "blocked", "reason": "no cv", "application_id": app.id}

        # §20 — verify the posting still exists before generating an employer email.
        verifier = self.verify_url_fn
        if verifier:
            from app.models import utcnow as _now

            try:
                live = bool(verifier(job.url))
            except Exception as exc:
                live = False
                mem.store.record_event(self.session, "action",
                                       f"verification error for #{job.id}: {exc}", "warn",
                                       {"job_id": job.id})
            if not live:
                job.verification_status = "unverified"
                job.last_verified_at = _now()
                app = mem.store.add_application(self.session, job.id, decision.id, action,
                                                score, contact_email)
                app.status = "blocked"
                mem.store.record_event(self.session, "action",
                                       "posting no longer live — application blocked", "error",
                                       {"job_id": job.id})
                return {"sent": False, "status": "blocked", "reason": "posting not live",
                        "application_id": app.id}
            job.last_verified_at = _now()
            job.verification_status = "verified"

        draft = await self.communicator.generate(job, action, target_language)

        daily_total = mem.store.count_dispatched_today(self.session)
        daily_applications = mem.store.count_dispatched_today(self.session, action="APPLY")
        daily_inquiries = mem.store.count_dispatched_today(self.session, action="ASK_EMPLOYER")

        report = validate(
            to_addr=contact_email,
            subject=draft["subject"],
            body=draft["body"],
            attachments=[attachment],
            job=job,
            profile=self.profile,
            config=self.config,
            session=self.session,
            action=action,
            daily_sent=daily_total,
            daily_total=daily_total,
            daily_applications=daily_applications,
            daily_inquiries=daily_inquiries,
            employer_cooldown_days=self.settings.employer_cooldown_days,
        )

        app = mem.store.add_application(self.session, job.id, decision.id, action,
                                        score, contact_email)
        email = mem.store.add_email(self.session, app.id, contact_email, draft["subject"],
                                    draft["body"], [attachment], status="validated", mode=mode)
        email.validation_log = report.reasons

        if not report.allowed:
            # live mode: the daily outbound budget is the only blocker -> keep
            # the message as a Gmail draft instead of silently blocking it.
            if mode == "live" and _only_cap_blocked(report):
                mem.store.record_event(self.session, "action",
                                       f"daily outbound cap reached - kept as draft for {contact_email}",
                                       "info", {"job_id": job.id, "application_id": app.id})
                return self._finalize_draft(job, app, email, contact_email, draft["subject"],
                                            draft["body"], attachment, score)
            email.status = "blocked"
            app.status = "blocked"
            mem.store.record_event(self.session, "action",
                                   f"email blocked: {report.reasons}", "error",
                                   {"job_id": job.id, "application_id": app.id})
            return {"sent": False, "status": "blocked", "report": report.reasons,
                    "application_id": app.id, "email_id": email.id}

        # ---- outbound boundary -------------------------------------------
        if mode == "dry_run":
            return self._finalize_dry_run(job, app, email, contact_email, draft)
        if mode == "draft":
            return self._finalize_draft(job, app, email, contact_email, draft["subject"],
                                        draft["body"], attachment, score)
        if mode == "live":
            return self._finalize_live(job, app, email, contact_email, draft["subject"],
                                       draft["body"], attachment, score)
        app.status = "blocked"
        email.status = "blocked"
        mem.store.record_event(self.session, "action",
                               f"unknown email mode {mode!r} — blocked", "error",
                               {"job_id": job.id})
        return {"sent": False, "status": "blocked", "reason": f"unknown mode {mode}",
                "application_id": app.id, "email_id": email.id}

    # ------------------------------------------------------- mode handlers
    def _finalize_dry_run(self, job, app, email, contact_email, draft) -> dict:
        """Generate + validate only. Record the outcome; no Gmail interaction."""
        email.status = "dry_run"
        email.sent_at = utcnow()
        app.status = "dry_run"
        app.sent_at = email.sent_at
        mem.store.record_event(self.session, "action",
                               f"dry-run validated (not sent) for {contact_email}",
                               "info", {"job_id": job.id, "email_id": email.id,
                                        "subject": draft["subject"][:80]})
        return {"sent": False, "status": "dry_run", "application_id": app.id,
                "email_id": email.id}

    def _finalize_draft(self, job, app, email, contact_email, subject, body,
                        attachment, score) -> dict:
        """Create a Gmail Draft. Never sends — a human reviews and Sends it."""
        ok, draft_id, error = provider.create_draft(
            self.settings, to=contact_email, subject=subject, body=body,
            attachments=[attachment])
        if ok:
            email.status = "drafted"
            email.draft_id = draft_id or ""
            email.sent_at = utcnow()
            app.status = "drafted"
            app.sent_at = email.sent_at
            days = int(self.config.rules.get("follow_up_days", [7])[0])
            app.follow_up_at = app.sent_at + timedelta(days=days)
            job.status = "acted"
            mem.store.upsert_contact(self.session, contact_email, source="gmail_draft",
                                     company_id=job.company_id)
            mem.store.record_event(self.session, "action",
                                   f"draft created for {contact_email}", "info",
                                   {"job_id": job.id, "email_id": email.id, "draft_id": draft_id})
        else:
            email.status = "failed"
            email.error = error
            app.status = "deferred"
            mem.store.record_event(self.session, "action", f"draft creation failed: {error}",
                                   "error", {"job_id": job.id})
        return {"sent": False, "status": app.status, "error": error if not ok else "",
                "application_id": app.id, "email_id": email.id, "draft_id": email.draft_id}

    def _finalize_live(self, job, app, email, contact_email, subject, body,
                       attachment, score) -> dict:
        """Actually send through Gmail (only after the gate said yes)."""
        ok, message_id, error = provider.send(
            self.settings, to=contact_email, subject=subject, body=body,
            attachments=[attachment])
        if ok:
            email.status = "sent"
            email.sent_at = utcnow()
            email.message_id = message_id or ""
            app.status = "sent"
            app.sent_at = email.sent_at
            # schedule first follow-up
            days = int(self.config.rules.get("follow_up_days", [7])[0])
            app.follow_up_at = app.sent_at + timedelta(days=days)
            job.status = "acted"
            mem.store.upsert_contact(self.session, contact_email, source="application",
                                     company_id=job.company_id)
            mem.store.record_event(self.session, "action", f"email sent to {contact_email}",
                                   "info", {"job_id": job.id, "email_id": email.id,
                                            "message_id": message_id})
        else:
            email.status = "failed"
            email.error = error
            app.status = "deferred"
            mem.store.record_event(self.session, "action", f"email send failed: {error}",
                                   "error", {"job_id": job.id})
        return {"sent": ok, "status": app.status, "error": error if not ok else "",
                "application_id": app.id, "email_id": email.id,
                "message_id": email.message_id, "draft_id": email.draft_id}

    def _record_disabled(self, job, decision, action, score, contact_email, reason: str) -> dict:
        app = mem.store.add_application(self.session, job.id, decision.id, action,
                                        score, contact_email)
        app.status = "blocked"
        mem.store.record_event(self.session, "action", reason, "info", {"job_id": job.id})
        return {"sent": False, "status": "blocked", "reason": reason,
                "application_id": app.id}
