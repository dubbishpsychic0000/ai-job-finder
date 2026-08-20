"""Pipeline orchestrator — bundles discovery -> analysis -> action -> followups
into a "run" that the scheduler can invoke (cron / docker), or the CLI once.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.agents.immigration_agent import ImmigrationAgent
from app.agents.llm import get_llm
from app.config import get_config, get_preferences, get_profile, get_settings
from app.database import init_db, session_scope
from app.workflows.action import run_actions
from app.workflows.analysis import run_analysis
from app.workflows.discovery import run_discovery
from app.workflows.followup import run_follow_ups

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    discovery: dict = field(default_factory=dict)
    analysis: dict = field(default_factory=dict)
    action: dict = field(default_factory=dict)
    followup: dict = field(default_factory=dict)
    notifications: dict = field(default_factory=dict)


def run_pipeline(session: Session | None = None, *, sources_path: Path | None = None,
                 do_followups: bool = True, with_immigration: bool = True) -> RunResult:
    """Synchronous convenience wrapper (async internals run to completion)."""
    import asyncio

    init_db()
    config = get_config()
    prefs = get_preferences()
    profile = get_profile()
    settings = get_settings()
    llm = get_llm(profile, settings)

    def _run() -> RunResult:
        result = RunResult()
        with session_scope() if session is None else _nullctx(session) as s:
            discovery = asyncio.run(run_discovery(s, config, prefs, sources_path, profile=profile, llm=llm))
            result.discovery = {
                "new_jobs": discovery.new_jobs,
                "duplicates": discovery.duplicates,
                "fetched": discovery.opportunities_fetched,
                "errors": discovery.source_errors,
                "employers_discovered": discovery.employers_discovered,
                "immigration_facts": discovery.immigration_facts,
                "opportunity_sources": discovery.opportunity_sources,
                "social_signals": discovery.social_signals,
                "source_reports": discovery.source_reports,
            }
            analysis = asyncio.run(run_analysis(s, config, profile, llm, prefs.countries))
            result.analysis = {
                "analyzed": len(analysis.analyzed),
                "decisions": dict(Counter(d["decision"] for d in analysis.analyzed)),
                "errors": analysis.errors,
            }
            immigration = ImmigrationAgent(llm) if with_immigration else None
            action = asyncio.run(run_actions(s, config, settings, profile, llm, immigration))
            result.action = {
                "applied": len(action.applied),
                "asked": len(action.asked),
                "investigated": len(action.investigated),
                "blocked": action.blocked,
                "errors": action.errors,
            }
            if do_followups and not settings.global_pause and not _is_paused():
                from app.agents.communication_agent import CommunicationAgent

                communicator = CommunicationAgent(llm, profile)
                fu = _followups(s, config, settings, communicator)
                result.followup = {"sent": fu.sent, "blocked": fu.blocked, "errors": fu.errors}
            # Rendering/delivery is intentionally after all actions and never
            # controls discovery, decisions, or email safety.
            from app.notifications.service import NotificationService
            from app.notifications.whatsapp import MetaWhatsAppProvider

            notifications = NotificationService(s)
            whatsapp = MetaWhatsAppProvider()
            if whatsapp.configured:
                # A concise operational heartbeat is sent on *every* pipeline
                # run, including zero-result runs.  Event notifications remain
                # separate so a useful alert is never suppressed by the digest
                # interval.
                run_summary = render_run_summary(result)
                result.notifications = {
                    "immediate": notifications.immediate(sender=whatsapp.send),
                    "digest": notifications.digest(sender=whatsapp.send),
                    "run_summary": run_summary,
                    "run_summary_sent": whatsapp.send(run_summary),
                    "whatsapp_configured": True,
                }
            else:
                # Do not mark a notification delivered merely because no
                # transport has been configured yet.
                result.notifications = {"immediate": [], "digest": None,
                                        "whatsapp_configured": False}
        return result

    return _run()


def render_run_summary(result: RunResult) -> str:
    """Compact, non-sensitive WhatsApp heartbeat for one completed run."""
    discovery = result.discovery
    action = result.action
    drafts = sum(1 for item in (action.get("applied", []) + action.get("asked", []))
                 if item.get("status") == "drafted")
    errors = (len(discovery.get("errors", [])) + len(action.get("errors", [])) +
              len(result.analysis.get("errors", [])) + len(result.followup.get("errors", [])))
    return (
        "Career Agent run complete\n"
        f"Jobs: {discovery.get('new_jobs', 0)} new / {discovery.get('fetched', 0)} fetched\n"
        f"Actions: {len(action.get('applied', []))} apply, {len(action.get('asked', []))} ask, "
        f"{len(action.get('investigated', []))} investigated\n"
        f"Gmail drafts: {drafts} | Errors: {errors}"
    )


def _is_paused() -> bool:
    from app.scheduler.control import is_paused

    return is_paused()


class _nullctx:
    """No-op context manager so callers can pass their own open session."""

    def __init__(self, session: Session):
        self.session = session

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, *exc) -> None:
        return None


def _followups(session, config, settings, communicator):

    return run_follow_ups(session, config, settings, communicator)
