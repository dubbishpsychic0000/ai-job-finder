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
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import memory as mem
from app.agents.communication_agent import CommunicationAgent
from app.config import AgentConfig, CandidateProfile, RunnerSettings
from app.discovery.opportunity_details import is_verified_individual_vacancy
from app.email import provider
from app.email.safety_gate import validate
from app.models import Application, Email, LiveSendAttempt, utcnow

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
        # §20 — re-verify supported structured ATS evidence before acting.
        # Injectable for hermetic tests; missing verification fails closed.
        self.verify_url_fn = verify_url_fn

    # ------------------------------------------------------------------ UI
    async def run(
        self,
        job,
        decision,
        action: str,
        contact_email: str = "",
        target_language: str = "en",
        *,
        allow_live_cap_draft_fallback: bool = True,
    ) -> dict:
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
        public_competition = getattr(job, "public_competition", None)
        if (
            getattr(job, "source", "") == "emploi_public_ma"
            or (
                isinstance(public_competition, dict)
                and public_competition.get("source") == "emploi_public_ma"
            )
        ):
            return {
                "sent": False,
                "status": "blocked",
                "reason": "competition_owner_approval_required",
            }
        if not is_spontaneous and not is_verified_individual_vacancy(job):
            return {
                "sent": False,
                "status": "blocked",
                "reason": "vacancy_not_verified",
            }
        if not is_spontaneous and self.verify_url_fn is None:
            return {
                "sent": False,
                "status": "blocked",
                "reason": "vacancy_reverification_unavailable",
            }
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
        if mode == "live" and not is_spontaneous:
            approved_job_id = str(
                getattr(self.settings, "approved_live_job_id", "") or ""
            )
            if (
                not getattr(self.settings, "live_email_manual_dispatch", False)
                or not getattr(self.settings, "authorize_live_job_email", False)
                or not approved_job_id.isdecimal()
                or str(job.id) != approved_job_id
            ):
                return {
                    "sent": False,
                    "status": "blocked",
                    "reason": "single_job_live_email_authorization_required",
                }

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

        if mode == "live":
            recovery = self._recover_pending_live_send(job)
            if recovery is not None:
                return recovery

        if (
            action == "APPLY"
            and not is_spontaneous
            and mode != "dry_run"
            and getattr(job, "application_method", "") != "EMAIL"
        ):
            return {
                "sent": False,
                "status": "blocked",
                "reason": "portal_application_requires_portal_submission",
            }
        attachment = (
            resolve_attachment(Path("candidate") / "cv", target_language)
            if action == "APPLY" else ""
        )
        attachments = [attachment] if attachment else []
        if action == "APPLY" and not attachment:
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

            verification_time = None
            try:
                verification = verifier(job)
                verification_outcome = getattr(
                    verification, "outcome", "UNKNOWN",
                )
                verification_reason = getattr(
                    verification, "reason_code", "send_time_recheck_failed",
                )
                verification_time = getattr(verification, "observed_at", None)
                if (
                    isinstance(verification_time, datetime)
                    and verification_time.tzinfo is None
                ):
                    verification_time = verification_time.replace(tzinfo=timezone.utc)
                live = (
                    verification_outcome == "OPEN"
                    and verification_reason == "matching_public_ats_posting_record"
                    and getattr(verification, "evidence_url", "") == job.url
                    and isinstance(verification_time, datetime)
                    and timedelta(0)
                    <= datetime.now(timezone.utc) - verification_time
                    <= timedelta(minutes=5)
                )
                if live:
                    company_name = (
                        job.company.name if getattr(job, "company", None) else ""
                    )

                    def normalized(value):
                        return " ".join(str(value or "").casefold().split())

                    live = all((
                        normalized(job.title) == normalized(
                            getattr(verification, "title", ""),
                        ),
                        normalized(company_name) == normalized(
                            getattr(verification, "employer", ""),
                        ),
                        normalized(job.location) == normalized(
                            getattr(verification, "location", ""),
                        ),
                        normalized(job.description) == normalized(
                            getattr(verification, "description", ""),
                        ),
                        str(job.application_url or "") == str(
                            getattr(verification, "application_url", ""),
                        ),
                    ))
                    if not live:
                        verification_outcome = "UNKNOWN"
                        verification_reason = "current_ats_record_changed"
                    closing_at = getattr(verification, "closing_at", None)
                    if (
                        live
                        and isinstance(closing_at, datetime)
                        and (
                            closing_at.replace(tzinfo=timezone.utc)
                            if closing_at.tzinfo is None
                            else closing_at.astimezone(timezone.utc)
                        ) <= datetime.now(timezone.utc)
                    ):
                        live = False
                        verification_outcome = "CLOSED"
                        verification_reason = "application_deadline_elapsed"
                elif verification_outcome == "OPEN":
                    verification_outcome = "UNKNOWN"
                    verification_reason = "send_time_evidence_not_current"
            except Exception as exc:
                live = False
                verification_outcome = "UNKNOWN"
                verification_reason = "send_time_recheck_failed"
                mem.store.record_event(self.session, "action",
                                       f"verification error for #{job.id} "
                                       f"({type(exc).__name__})", "warn",
                                       {"job_id": job.id})
            if not live:
                job.verification_status = (
                    "closed" if verification_outcome == "CLOSED" else "unverified"
                )
                job.entity_state = (
                    "VACANCY_CLOSED"
                    if verification_outcome == "CLOSED"
                    else "VACANCY_STATUS_UNKNOWN"
                )
                job.entity_reason_code = verification_reason
                job.last_verified_at = _now()
                app = mem.store.add_application(self.session, job.id, decision.id, action,
                                                score, contact_email)
                app.status = "blocked"
                mem.store.record_event(self.session, "action",
                                       "posting no longer live — application blocked", "error",
                                       {"job_id": job.id})
                return {"sent": False, "status": "blocked", "reason": "posting not live",
                        "application_id": app.id}
            if mode != "dry_run" and action == "APPLY" and (
                getattr(job, "application_method", "") != "EMAIL"
                or getattr(verification, "application_method", "") != "EMAIL"
                or str(contact_email or "").strip().casefold()
                != str(getattr(job, "contact_email", "") or "").strip().casefold()
                or str(getattr(verification, "application_email", "")).casefold()
                != str(contact_email or "").strip().casefold()
            ):
                return {
                    "sent": False,
                    "status": "blocked",
                    "reason": "email_application_route_not_explicitly_verified",
                }
            if action == "ASK_EMPLOYER" and (
                not contact_email
                or str(contact_email).strip().casefold()
                != str(getattr(job, "contact_email", "") or "").strip().casefold()
                or str(contact_email).strip().casefold()
                not in str(getattr(verification, "description", "") or "").casefold()
            ):
                return {
                    "sent": False,
                    "status": "blocked",
                    "reason": "inquiry_recipient_not_verified_in_current_posting",
                }
            job.last_verified_at = verification_time
            job.verification_status = "verified"

        draft = await self.communicator.generate(job, action, target_language)

        daily_total = mem.store.count_dispatched_today(self.session)
        daily_applications = mem.store.count_dispatched_today(self.session, action="APPLY")
        daily_inquiries = mem.store.count_dispatched_today(self.session, action="ASK_EMPLOYER")

        report = validate(
            to_addr=contact_email,
            subject=draft["subject"],
            body=draft["body"],
            attachments=attachments,
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
                                    draft["body"], attachments, status="validated", mode=mode)
        email.validation_log = report.reasons

        if not report.allowed:
            # live mode: the daily outbound budget is the only blocker -> keep
            # the message as a Gmail draft instead of silently blocking it.
            if (
                mode == "live"
                and allow_live_cap_draft_fallback
                and _only_cap_blocked(report)
            ):
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
            attachments=[attachment] if attachment else [],
            attempt_key=email.draft_attempt_key)
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
        """Persist a send intent before crossing the irreversible provider boundary."""
        attempt_key = uuid.uuid4().hex
        fingerprint = _message_fingerprint(
            contact_email, subject, body, [attachment] if attachment else [],
        )
        company = getattr(job, "company", None)
        attempt = LiveSendAttempt(
            attempt_key=attempt_key,
            active_job_id=job.id,
            job_id=job.id,
            application_id=app.id,
            email_id=email.id,
            employer_name=getattr(company, "name", "") or "",
            recipient=contact_email,
            subject=subject,
            message_fingerprint=fingerprint,
            state="prepared",
        )
        email.delivery_state = "prepared"
        email.status = "prepared"
        app.status = "deferred"
        self.session.add(attempt)
        try:
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            logger.warning("Live send for job %s already has an active attempt", job.id)
            return {
                "sent": False, "status": "recovery_required",
                "reason": "another live-send attempt is already active for this application",
                "application_id": app.id, "email_id": email.id,
            }
        except Exception as exc:
            self.session.rollback()
            logger.error("Live-send intent persistence failed; Gmail was not called (%s)",
                         type(exc).__name__)
            raise RuntimeError(
                "Live-send intent could not be persisted; no message was attempted"
            ) from None

        # A persisted dispatching state is deliberately treated as uncertain:
        # a crash can occur either just before or just after the network call.
        attempt.state = "dispatching"
        attempt.updated_at = utcnow()
        email.delivery_state = "dispatching"
        email.status = "dispatching"
        try:
            self.session.commit()
        except Exception as exc:
            self.session.rollback()
            logger.error("Live-send dispatch state persistence failed; Gmail was not called (%s)",
                         type(exc).__name__)
            raise RuntimeError(
                "Live-send dispatch state could not be persisted; no message was attempted"
            ) from None

        try:
            ok, message_id, error = provider.send(
                self.settings, to=contact_email, subject=subject, body=body,
                attachments=[attachment] if attachment else [],
                attempt_key=attempt_key)
        except Exception as exc:
            ok, message_id, error = False, "", provider.GMAIL_SEND_OUTCOME_UNCERTAIN
            logger.warning("Email provider send raised after dispatch began (%s)",
                           type(exc).__name__)
        if ok and not str(message_id or "").strip():
            ok = False
            error = provider.GMAIL_SEND_OUTCOME_UNCERTAIN
        if ok:
            attempt.state = "confirmed_sent"
            attempt.updated_at = utcnow()
            attempt.message_id = message_id
            email.status = "sent"
            email.delivery_state = "confirmed"
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
            if app.action == "ASK_EMPLOYER":
                app.status = "inquiry_sent"
                app.follow_up_at = None
            result_status = app.status
        else:
            email.error = error
            if str(error).startswith(provider.GMAIL_SEND_NOT_ATTEMPTED):
                attempt.state = "definitively_failed"
                attempt.active_job_id = None
                attempt.error = error
                attempt.updated_at = utcnow()
                email.status = "failed"
                email.delivery_state = "definitively_failed"
                app.status = "failed"
                result_status = "failed"
                event = "Gmail send definitively not attempted"
            else:
                attempt.state = "uncertain"
                attempt.updated_at = utcnow()
                attempt.error = error or provider.GMAIL_SEND_OUTCOME_UNCERTAIN
                email.status = "uncertain"
                email.delivery_state = "uncertain"
                app.status = "deferred"
                result_status = "recovery_required"
                event = "Gmail send outcome uncertain; automatic resend blocked"
            mem.store.record_event(self.session, "action", event,
                                   "error", {"job_id": job.id, "attempt_key": attempt_key})
        try:
            self.session.commit()
        except Exception as exc:
            self.session.rollback()
            logger.error("Live-send result persistence failed; durable dispatch intent remains (%s)",
                         type(exc).__name__)
            raise RuntimeError(
                "Live-send result could not be persisted; reconciliation is required"
            ) from None
        return {"sent": ok, "status": result_status, "error": error if not ok else "",
                "application_id": app.id, "email_id": email.id,
                "message_id": email.message_id, "draft_id": email.draft_id}

    def _recover_pending_live_send(self, job) -> dict | None:
        return reconcile_pending_live_send(
            self.session, self.settings, self.config, job,
        )

    def _record_disabled(self, job, decision, action, score, contact_email, reason: str) -> dict:
        app = mem.store.add_application(self.session, job.id, decision.id, action,
                                        score, contact_email)
        app.status = "blocked"
        mem.store.record_event(self.session, "action", reason, "info", {"job_id": job.id})
        return {"sent": False, "status": "blocked", "reason": reason,
                "application_id": app.id}


def reconcile_pending_live_send(session: Session, settings: RunnerSettings,
                                config: AgentConfig, job) -> dict | None:
    """Reconcile a persisted live attempt without initiating any new send."""
    attempts = session.query(LiveSendAttempt).filter(
        LiveSendAttempt.job_id == job.id,
        LiveSendAttempt.state.in_(("prepared", "dispatching", "uncertain", "confirmed_sent")),
    ).order_by(LiveSendAttempt.id).all()
    if not attempts:
        return None
    if len(attempts) != 1:
        return {
            "sent": False, "status": "recovery_required",
            "reason": "multiple live-send records exist; operator reconciliation is required",
        }
    attempt = attempts[0]
    app = session.get(Application, attempt.application_id)
    email = session.get(Email, attempt.email_id)
    if app is None or email is None:
        return {
            "sent": False, "status": "recovery_required",
            "reason": "live-send record has missing application data",
        }
    if attempt.state == "confirmed_sent":
        return {
            "sent": False, "status": "sent",
            "reason": "application was already confirmed sent; duplicate suppressed",
            "application_id": app.id, "email_id": email.id,
            "message_id": attempt.message_id, "recovered": True,
        }
    if attempt.state == "prepared":
        attempt.state = "definitively_failed"
        attempt.active_job_id = None
        attempt.error = "process stopped before dispatch state was persisted"
        attempt.updated_at = utcnow()
        app.status = "failed"
        email.status = "failed"
        email.delivery_state = "definitively_failed"
        email.error = attempt.error
        try:
            session.commit()
        except Exception:
            session.rollback()
            raise
        return None

    result = provider.reconcile_live_send(
        settings,
        attempt_key=attempt.attempt_key,
        recipient=attempt.recipient,
        subject=attempt.subject,
        started_at=attempt.started_at,
    )
    if result.status == "found":
        attempt.state = "confirmed_sent"
        attempt.active_job_id = attempt.job_id
        attempt.message_id = result.message_id
        attempt.updated_at = utcnow()
        email.status = "sent"
        email.delivery_state = "confirmed"
        email.sent_at = utcnow()
        email.message_id = result.message_id
        app.status = "sent"
        app.sent_at = email.sent_at
        app.follow_up_at = app.sent_at + timedelta(
            days=int(config.rules.get("follow_up_days", [7])[0]),
        )
        job.status = "acted"
        mem.store.upsert_contact(
            session, attempt.recipient, source="application_reconciled",
            company_id=job.company_id,
        )
        mem.store.record_event(
            session, "action", "Gmail live send recovered from Sent mailbox",
            "info", {"job_id": job.id, "attempt_key": attempt.attempt_key,
                     "message_id": result.message_id},
        )
        session.commit()
        return {
            "sent": True, "status": "sent", "message_id": result.message_id,
            "application_id": app.id, "email_id": email.id, "recovered": True,
        }

    attempt.state = "uncertain"
    attempt.updated_at = utcnow()
    attempt.error = f"Sent mailbox reconciliation: {result.status}"
    email.status = "uncertain"
    email.delivery_state = "uncertain"
    email.error = attempt.error
    app.status = "deferred"
    session.commit()
    return {
        "sent": False, "status": "recovery_required",
        "reason": f"live-send reconciliation is {result.status}; automatic resend blocked",
        "application_id": app.id, "email_id": email.id,
    }


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


def _message_fingerprint(recipient: str, subject: str, body: str,
                         attachments: list[str]) -> str:
    attachment_digests = []
    for attachment in attachments:
        digest = hashlib.sha256()
        with Path(attachment).open("rb") as stream:
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                digest.update(chunk)
        attachment_digests.append(digest.hexdigest())
    content = "\0".join((
        recipient.strip().casefold(), subject, body, *attachment_digests,
    ))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
