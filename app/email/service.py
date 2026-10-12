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

import hashlib
import logging
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app import memory as mem
from app.agents.communication_agent import CommunicationAgent
from app.config import AgentConfig, CandidateProfile, RunnerSettings
from app.email import provider
from app.email.safety_gate import validate
from app.models import Application, Email, utcnow

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

        is_spontaneous = (
            getattr(job, "opportunity_type", "") == "SPONTANEOUS_APPLICATION"
        )
        if is_spontaneous and mode == "live" and not getattr(
            self.settings, "authorize_live_spontaneous", False
        ):
            return self._record_disabled(
                job, decision, action, score, contact_email,
                reason="live spontaneous email is not explicitly authorized",
            )
        if is_spontaneous and mode not in {"dry_run", "draft", "live"}:
            return self._record_disabled(
                job, decision, action, score, contact_email,
                reason=f"unsupported spontaneous email mode: {mode}",
            )
        if is_paused():
            return self._record_disabled(
                job, decision, action, score, contact_email,
                reason="global emergency pause is ON — outbound halted")

        if mode == "draft" and self.settings.email_provider == "gmail":
            recovery = reconcile_pending_draft(
                self.session, self.settings, job.id,
            )
            if recovery is not None:
                return recovery

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
                result = self._finalize_draft(job, app, email, contact_email, draft["subject"],
                                              draft["body"], attachment, score)
                result["daily_limit_reached"] = True
                return result
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
        email.draft_attempt_key = hashlib.sha256(
            f"wca-gmail-draft-job:{job.id}".encode()
        ).hexdigest()
        email.delivery_state = "creating"
        email.status = "creating"
        app.status = "deferred"
        mem.store.record_event(
            self.session, "action", "Gmail draft intent persisted",
            "info", {"job_id": job.id, "application_id": app.id, "email_id": email.id},
        )
        # Commit the recovery marker before the external API call. If the Gmail
        # result or the subsequent DB commit is lost, the next run can reconcile
        # by the marker and must not issue another create request blindly.
        try:
            self.session.commit()
        except Exception as exc:
            self.session.rollback()
            logger.error(
                "Gmail draft intent persistence failed; Gmail was not called (%s)",
                type(exc).__name__,
            )
            raise RuntimeError(
                "Gmail draft intent could not be persisted; no draft was attempted"
            ) from None
        ok, draft_id, error = provider.create_draft(
            self.settings, to=contact_email, subject=subject, body=body,
            attachments=[attachment], attempt_key=email.draft_attempt_key)
        if ok:
            email.status = "drafted"
            email.delivery_state = "confirmed"
            email.draft_id = draft_id or ""
            email.sent_at = utcnow()
            app.status = "drafted"
            app.sent_at = email.sent_at
            if getattr(job, "opportunity_type", "") != "SPONTANEOUS_APPLICATION":
                days = int(self.config.rules.get("follow_up_days", [7])[0])
                app.follow_up_at = app.sent_at + timedelta(days=days)
            job.status = "acted"
            mem.store.upsert_contact(self.session, contact_email, source="gmail_draft",
                                     company_id=job.company_id)
            mem.store.record_event(self.session, "action",
                                   "Gmail draft created", "info",
                                   {"job_id": job.id, "email_id": email.id, "draft_id": draft_id})
        else:
            uncertain = error == provider.GMAIL_DRAFT_OUTCOME_UNCERTAIN
            not_attempted = (
                error == provider.GMAIL_REAUTH_REQUIRED
                or error.startswith(provider.GMAIL_DRAFT_NOT_ATTEMPTED)
            )
            email.status = "uncertain" if uncertain else "failed"
            email.delivery_state = (
                "uncertain" if uncertain else ("not_attempted" if not_attempted else "uncertain")
            )
            email.error = error
            app.status = "failed" if not_attempted else "deferred"
            event_text = (
                "Gmail draft outcome uncertain; recovery required"
                if uncertain or not not_attempted
                else "Gmail draft not attempted"
            )
            mem.store.record_event(self.session, "action", event_text,
                                   "error", {"job_id": job.id})
        return {"sent": False, "status": app.status, "error": error if not ok else "",
                "application_id": app.id, "email_id": email.id, "draft_id": email.draft_id}

    def _finalize_live(self, job, app, email, contact_email, subject, body,
                       attachment, score) -> dict:
        """Actually send through Gmail (only after the gate said yes)."""
        try:
            ok, message_id, error = provider.send(
                self.settings, to=contact_email, subject=subject, body=body,
                attachments=[attachment])
        except Exception as exc:
            ok, message_id, error = False, "", str(exc)
            logger.exception("Email provider send failed")
        if ok and not str(message_id or "").strip():
            ok = False
            error = "email provider reported success without a message ID"
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


def reconcile_pending_draft(session: Session, settings: RunnerSettings, job_id: int) -> dict | None:
    """Recover or fail closed on a draft create whose result was not persisted."""
    pending = session.query(Application, Email).join(
        Email, Email.application_id == Application.id,
    ).filter(
        Application.job_id == job_id,
        Application.status == "deferred",
        Email.mode == "draft",
        Email.delivery_state.in_(("creating", "uncertain")),
    ).order_by(Email.id).all()
    if not pending:
        return None
    if len(pending) != 1 or not pending[0][1].draft_attempt_key:
        return {
            "sent": False,
            "status": "recovery_required",
            "reason": "multiple or unmarked Gmail draft attempts require manual reconciliation",
        }

    app, email = pending[0]
    result = provider.reconcile_draft(settings, email.draft_attempt_key)
    if result.status != "found":
        logger.warning(
            "Gmail draft recovery requires follow-up (%s); no create retry will run",
            result.status,
        )
        return {
            "sent": False,
            "status": "recovery_required",
            "reason": f"Gmail draft reconciliation: {result.status}",
            "application_id": app.id,
            "email_id": email.id,
        }

    email.status = "drafted"
    email.delivery_state = "confirmed"
    email.draft_id = result.draft_id
    email.sent_at = utcnow()
    app.status = "drafted"
    app.sent_at = email.sent_at
    job = app.job
    if job is not None:
        job.status = "acted"
        mem.store.upsert_contact(
            session, email.to_addr, source="gmail_draft_reconciled",
            company_id=job.company_id,
        )
    mem.store.record_event(
        session, "action", "Gmail draft recovered by attempt marker", "info",
        {"job_id": job_id, "application_id": app.id, "email_id": email.id},
    )
    session.commit()
    return {
        "sent": False,
        "status": "drafted",
        "draft_id": email.draft_id,
        "application_id": app.id,
        "email_id": email.id,
        "recovered": True,
    }
