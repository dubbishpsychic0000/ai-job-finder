"""Command-line interface — `wca <command>`.

  wca init                 create DB + demo schema
  wca run-once [--json]    run the full pipeline once (discovery->action->followup)
  wca discover             discovery only
  wca analyze              analysis only
  wca act                  actions only
  wca followups            follow-ups only
  wca search-plan          print the current search plan
  wca stats                print dashboard-style stats
  wca pause / resume       toggle the agent
  wca scheduler            run the infinite scheduler loop
  wca dashboard            start the web dashboard (uvicorn app.api.main:app)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from app import memory as mem
from app.agents.immigration_agent import ImmigrationAgent
from app.agents.llm import get_llm
from app.config import ROOT_DIR, get_config, get_preferences, get_profile, get_settings
from app.database import init_db, session_scope

console = Console()


def _load_new_discovery_jobs(session, previous_max_id: int) -> list:
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload

    from app import models

    return list(session.scalars(
        select(models.Job)
        .options(joinedload(models.Job.company))
        .where(models.Job.id > previous_max_id)
        .order_by(models.Job.id),
    ))


def _print_json(data: dict) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def cmd_init(_args) -> None:
    init_db()
    console.print("[green]Database initialised.[/green]")


def cmd_run_once(args) -> None:
    from app.workflows.pipeline import run_pipeline

    result = run_pipeline()
    data = {
        "discovery": result.discovery,
        "analysis": result.analysis,
        "action": {
            "applied": result.action.get("applied"),
            "asked": result.action.get("asked"),
            "investigated": result.action.get("investigated", 0),
            "blocked": len(result.action.get("blocked", [])),
            "drafts": result.action.get("drafts", 0)
                      + result.company_outreach.get("action", {}).get("btp", {}).get("drafts", 0),
        },
        "company_outreach": result.company_outreach,
        "followup": result.followup,
    }
    if args.json:
        _print_json(data)
        return
    _render_run(data)


def _render_run(data: dict) -> None:
    t = Table(title="Agent run")
    t.add_column("Stage")
    t.add_column("Result")
    t.add_row("Discovery", f"{data['discovery']['new_jobs']} new / {data['discovery']['fetched']} fetched")
    t.add_row("Analysis", str(data["analysis"].get("decisions")))
    a = data["action"]
    t.add_row("Actions", f"applied {a['applied']}, asked {a['asked']}, investigated {a['investigated']}")
    company_outreach = data.get("company_outreach", {})
    btp = company_outreach.get("action", {}).get("btp", {})
    if btp:
        t.add_row("BTP companies", f"{btp.get('candidates', 0)} candidates, "
                     f"{btp.get('drafts', 0)} drafts")
    elif company_outreach.get("status") == "skipped":
        t.add_row("BTP companies", f"skipped ({company_outreach.get('reason', '')})")
    t.add_row("Follow-ups", f"sent {data['followup'].get('sent', 0)}, blocked {data['followup'].get('blocked', 0)}")
    console.print(t)


def cmd_discover(args) -> None:
    from app.workflows.discovery import DEFAULT_SOURCES_PATH, DEMO_SOURCES_PATH, run_discovery

    config, prefs = get_config(), get_preferences()
    sources_path = DEMO_SOURCES_PATH if args.demo else DEFAULT_SOURCES_PATH
    with session_scope() as s:
        report = asyncio.run(run_discovery(s, config, prefs, sources_path=sources_path,
                                           profile=get_profile()))
    console.print(f"found {report.new_jobs} new jobs, {report.duplicates} dupes, errors={report.source_errors}")
    table = Table(title=f"Discovery source diagnostics ({'demo' if args.demo else 'real'})")
    for col in ("Source", "Kind", "Queries", "Fetched", "Normalized", "Dupes", "New", "Status"):
        table.add_column(col)
    for item in report.source_reports:
        table.add_row(item["name"], item["kind"], str(item["queries"]), str(item["fetched"]),
                      str(item["normalized"]), str(item["duplicates"]), str(item["new"]),
                      item["error"] or item["rate_status"])
    console.print(table)


def cmd_discovery_only(args) -> None:
    """Run bounded discovery against the ephemeral copy of production state."""
    import sqlite3

    from sqlalchemy import func, select

    from app import models
    from app.config import load_yaml
    from app.workflows.discovery import DEFAULT_SOURCES_PATH
    from app.workflows.discovery_only import (
        blocked_report,
        build_sanitized_report,
        daily_budget_summary,
        run_bounded_discovery,
        validate_budget_ledgers,
    )
    from app.workflows.tavily_budget_vault import (
        BudgetPersistenceError,
        GitVaultBudgetStore,
    )

    report_path = Path(args.report)
    report = None
    budget_store = None
    root = ROOT_DIR
    data_dir = root / "data"
    phase = "input_restore"
    exception_class = ""

    def set_phase(value: str) -> None:
        nonlocal phase
        phase = value

    try:
        database_path = get_settings().sqlite_path
        status_path = data_dir / "discovery_only_inputs_status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("ready") is not True:
            report = blocked_report(
                "production_inputs_not_restored",
                phase=phase,
            )
        else:
            profile_path = root / "candidate" / "profile.yaml"
            preferences_path = root / "candidate" / "preferences.yaml"
            keys_path = root / "secrets" / "tavily_keys.txt"
            if not profile_path.is_file() or not preferences_path.is_file():
                report = blocked_report(
                    "candidate_search_preferences_missing", phase="profile_parse",
                )
            elif not database_path.is_file():
                report = blocked_report(
                    "production_database_snapshot_missing", phase="database_check",
                )
            else:
                phase = "config_load"
                config = get_config()
                phase = "profile_parse"
                profile = get_profile(profile_path)
                preferences = get_preferences(preferences_path)
                phase = "source_initialization"
                from app.config import load_yaml
                from app.workflows.discovery import DEFAULT_SOURCES_PATH

                sources_config = load_yaml(DEFAULT_SOURCES_PATH)
                enabled_sources = [
                    source for source in sources_config.get("connectors", [])
                    if source.get("enabled", True)
                ]
                if any(source.get("kind") != "tavily" for source in enabled_sources):
                    report = blocked_report(
                        "unsupported_connector_enabled_for_bounded_discovery",
                        phase=phase,
                    )

                phase = "budget_validation"
                key_text = (
                    keys_path.read_text(encoding="utf-8")
                    if keys_path.is_file() else ""
                )
                keys = [
                    key.strip()
                    for key in key_text.replace("\r", "").replace("\n", ",").split(",")
                    if key.strip()
                ]
                fingerprints = {}
                from app.connectors.tavily_resilience import key_fingerprint

                for key in keys:
                    fingerprint = key_fingerprint(key)
                    if fingerprint in fingerprints and fingerprints[fingerprint] != key:
                        report = blocked_report(
                            "provider_budget_ledger_collision",
                            phase=phase,
                        )
                    fingerprints[fingerprint] = key
                ledgers_valid, restored_ledger_count = validate_budget_ledgers(
                    data_dir, keys,
                )
                vault_repo = os.environ.get("TAVILY_BUDGET_VAULT_REPO", "")
                if report is None and keys and not ledgers_valid:
                    report = blocked_report(
                        "provider_budget_ledger_missing_or_invalid", phase=phase,
                    )
                elif report is None and keys and not vault_repo:
                    report = blocked_report(
                        "provider_budget_persistence_unavailable", phase=phase,
                        diagnostic_message=(
                            "The private-vault reservation store is not configured."
                        ),
                    )
                elif report is None:
                    budget_store = (
                        GitVaultBudgetStore(Path(vault_repo)) if keys else None
                    )
                    if budget_store is not None:
                        for fingerprint in dict.fromkeys(
                            key_fingerprint(key) for key in keys
                        ):
                            budget_store.synchronize(
                                data_dir / f"tavily_budget_{fingerprint}.json",
                                today=datetime.now(timezone.utc).date().isoformat(),
                            )
                    phase = "database_check"
                    uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
                    try:
                        with sqlite3.connect(uri, uri=True) as connection:
                            integrity = connection.execute("PRAGMA quick_check").fetchone()
                            tables = {
                                row[0] for row in connection.execute(
                                    "SELECT name FROM sqlite_master WHERE type='table'",
                                )
                            }
                        required_tables = set(models.Base.metadata.tables)
                        if not integrity or integrity[0] != "ok":
                            report = blocked_report(
                                "production_database_integrity_check_failed",
                                phase=phase,
                            )
                        elif not required_tables <= tables:
                            report = blocked_report(
                                "production_database_schema_incomplete",
                                phase=phase,
                            )
                        if report is None:
                            init_db()
                    except sqlite3.Error:
                        report = blocked_report(
                            "production_database_unreadable", phase=phase,
                        )
                    if report is None:
                        phase = "discovery_execution"
                        with session_scope() as session:
                            previous_max_id = session.scalar(
                                select(func.max(models.Job.id)),
                            ) or 0
                            discovery_report = asyncio.run(
                                run_bounded_discovery(
                                    session, config, preferences, profile,
                                    budget_store=budget_store,
                                    phase_callback=set_phase,
                                ),
                            )
                            session.flush()
                            new_jobs = _load_new_discovery_jobs(
                                session, previous_max_id,
                            )
                        budget_persistence_failed = any(
                            {
                                "provider_budget_persistence_unavailable",
                                "budget_ledger_unavailable",
                            } & set(item.get("query_error_codes", {}))
                            for item in discovery_report.source_reports
                        )
                        if budget_persistence_failed:
                            report = blocked_report(
                                "provider_budget_persistence_unavailable",
                                phase="budget_validation",
                                diagnostic_message=(
                                    "A durable budget update failed; discovery "
                                    "stopped without an unreserved retry."
                                ),
                            )
                            actual_budget = (
                                discovery_report.api_request_budget or {}
                            )
                            report["provider"].update({
                                "api_attempts": int(actual_budget.get("attempts", 0)),
                                "durably_reserved_attempts": (
                                    budget_store.reservation_count
                                    if budget_store else 0
                                ),
                                "successes": int(actual_budget.get("successes", 0)),
                                "failed_attempts": int(
                                    actual_budget.get("failed_attempts", 0),
                                ),
                                "retry_attempts": int(
                                    actual_budget.get("retry_attempts", 0),
                                ),
                                "remaining_run_attempts": int(
                                    actual_budget.get("remaining", 8),
                                ),
                            })
                            report["side_effects"]["private_vault_writes"] = (
                                budget_store.write_count if budget_store else 0
                            )
                            report["side_effects"][
                                "private_vault_budget_ledger_writes"
                            ] = budget_store.write_count if budget_store else 0
                        else:
                            phase = "report_serialization"
                            report = build_sanitized_report(
                                discovery_report,
                                new_jobs,
                                config,
                                daily_budget_summary(data_dir, keys),
                                sources_config=sources_config,
                            )
                            report["provider"]["restored_budget_ledger_count"] = (
                                restored_ledger_count
                            )
                            report["provider"]["durably_reserved_attempts"] = (
                                budget_store.reservation_count if budget_store else 0
                            )
                            if budget_store is not None:
                                report["provider"]["durable_ledger_writes"] = (
                                    budget_store.write_count
                                )
                                report["side_effects"]["private_vault_writes"] = (
                                    budget_store.write_count
                                )
                                report["side_effects"][
                                    "private_vault_budget_ledger_writes"
                                ] = budget_store.write_count
                            report["phase"] = "completed"
    except Exception as exc:
        exception_class = type(exc).__name__
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", exception_class):
            exception_class = "Exception"
        reason_code = {
            "input_restore": "production_inputs_invalid_or_incomplete",
            "config_load": "discovery_config_invalid",
            "profile_parse": "candidate_profile_or_preferences_invalid",
            "source_initialization": "discovery_source_initialization_failed",
            "budget_validation": "provider_budget_persistence_unavailable",
            "database_check": "production_database_check_failed",
            "discovery_execution": (
                "provider_budget_persistence_unavailable"
                if isinstance(exc, BudgetPersistenceError)
                else "discovery_execution_failed"
            ),
            "report_serialization": "discovery_report_serialization_failed",
        }.get(phase, "discovery_execution_failed")
        message = {
            "input_restore": "Required restored inputs could not be read.",
            "config_load": "The public discovery configuration could not be loaded.",
            "profile_parse": "Candidate search preferences could not be validated.",
            "source_initialization": "The bounded public source configuration is invalid.",
            "budget_validation": "The durable provider budget could not be verified.",
            "database_check": "The restored database failed its read-only checks.",
            "discovery_execution": "Bounded discovery failed before a complete report was produced.",
            "report_serialization": "The sanitized discovery report could not be serialized.",
        }.get(phase, "The bounded discovery task failed.")
        report = blocked_report(
            reason_code,
            phase=phase,
            exception_class=exception_class,
            diagnostic_message=message,
        )
        if budget_store is not None:
            report["provider"]["durably_reserved_attempts"] = (
                budget_store.reservation_count
            )
            report["side_effects"]["private_vault_writes"] = (
                budget_store.write_count
            )
            report["side_effects"][
                "private_vault_budget_ledger_writes"
            ] = budget_store.write_count

    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report or blocked_report(
                "discovery_report_unavailable", phase="report_serialization",
            ), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logging.getLogger(__name__).error(
            "Could not write sanitized discovery report (phase=report_serialization)",
        )
        raise SystemExit(1) from None


def cmd_btp_outreach(args) -> None:
    """Prepare bounded, safety-gated French spontaneous-application drafts."""
    from app.workflows.btp_outreach import BtpOutreachSafetyError, run_btp_outreach

    settings = get_settings()
    allowed_modes = {"dry_run", "draft", "live"}
    if settings.email_mode not in allowed_modes:
        raise SystemExit("BTP outreach requires EMAIL_MODE=dry_run, draft, or live")
    if settings.email_mode != "dry_run" and settings.enable_email is not True:
        raise SystemExit("BTP outreach requires ENABLE_EMAIL=true; no Gmail interaction occurred")
    if settings.email_mode != "dry_run" and settings.email_provider != "gmail":
        raise SystemExit("BTP outreach requires EMAIL_PROVIDER=gmail; no email-provider interaction occurred")
    if settings.email_mode == "live" and settings.authorize_live_spontaneous is not True:
        raise SystemExit(
            "BTP live outreach requires AUTHORIZE_LIVE_SPONTANEOUS=true; "
            "no Gmail interaction occurred"
        )
    preflight_only = bool(getattr(args, "preflight_only", False))
    target_employer = getattr(args, "target_employer", None)
    if preflight_only and (settings.email_mode != "dry_run" or not target_employer):
        raise SystemExit(
            "--preflight-only requires --target-employer and EMAIL_MODE=dry_run; "
            "no provider interaction occurred"
        )
    if preflight_only and not settings.sqlite_path.is_file():
        raise SystemExit(
            "targeted preflight requires an existing restored database; "
            "refusing to create or initialize a local database"
        )
    if not preflight_only:
        init_db()
    config = get_config()
    profile = get_profile()
    company_universe = None
    try:
        with session_scope() as s:
            discovery_cfg = config.discovery or {}
            if discovery_cfg.get("company_universe_discovery", False) and not preflight_only:
                from app.workflows.company_universe import run_company_universe_discovery

                researcher = None
                if discovery_cfg.get("website_research", False):
                    from app.discovery.website_researcher import WebsiteResearcher

                    researcher = WebsiteResearcher(
                        max_pages=int(discovery_cfg.get("website_research_max_pages", 12)),
                        per_host_delay=float(
                            discovery_cfg.get("website_research_delay_seconds", 0.25)
                        ),
                        state_path=ROOT_DIR / "data" / "website_research_state.json",
                        session=s,
                        refresh_after_days=int(
                            discovery_cfg.get("website_research_refresh_days", 7)
                        ),
                    )
                company_universe = asyncio.run(run_company_universe_discovery(
                    s, config, researcher=researcher,
                ))
            route_researcher = None
            if preflight_only:
                from app.discovery.website_researcher import WebsiteResearcher

                route_researcher = WebsiteResearcher(
                    max_pages=int(config.discovery.get("website_research_max_pages", 12)),
                    per_host_delay=float(
                        config.discovery.get("website_research_delay_seconds", 0.25)
                    ),
                    state_path=None,
                    session=s,
                    allow_redirects=False,
                    allow_safe_same_site_redirects=True,
                )
            report = run_btp_outreach(
                s,
                config,
                settings,
                profile,
                origin_city=args.origin_city,
                max_companies=args.max_companies,
                target_employer=target_employer,
                preflight_only=preflight_only,
                researcher=route_researcher,
            )
            if preflight_only:
                s.rollback()
    except BtpOutreachSafetyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(2) from exc
    run_report = report.as_run_report()
    if company_universe is not None:
        run_report["company_universe"] = company_universe.as_dict()
    _print_json(run_report)


def cmd_history_preflight(args) -> None:
    """Inspect employer history without initializing or mutating the database."""
    from app.config import RunnerSettings
    from app.workflows.history_preflight import run_history_preflight

    settings = RunnerSettings(_env_file=None)
    report = run_history_preflight(
        database_path=args.database,
        screening_state_path=args.screening_state,
        profile_path=args.profile,
        preferences_path=args.preferences,
        restore_status_path=args.restore_status,
        employer_cooldown_days=settings.employer_cooldown_days,
        daily_max_outbound=settings.daily_max_outbound,
        daily_max_applications=settings.daily_max_applications,
        daily_max_inquiries=settings.daily_max_inquiries,
        employer_ids=tuple(args.employer) if args.employer else None,
    )
    _print_json(report)


def cmd_analyze(_args) -> None:
    from app.workflows.analysis import run_analysis

    config, prefs, profile = get_config(), get_preferences(), get_profile()
    llm = get_llm(profile)
    with session_scope() as s:
        report = asyncio.run(
            run_analysis(s, config, profile, llm, prefs.countries, preferences=prefs)
        )
    console.print(report.analyzed)


def cmd_act(_args) -> None:
    from app.workflows.action import run_actions

    config, settings, profile = get_config(), get_settings(), get_profile()
    llm = get_llm(profile)
    with session_scope() as s:
        report = asyncio.run(run_actions(s, config, settings, profile, llm, ImmigrationAgent(llm)))
    console.print(f"applied={len(report.applied)} asked={len(report.asked)} "
                  f"blocked={len(report.blocked)} errors={report.errors}")


def cmd_followups(_args) -> None:
    from app.agents.communication_agent import CommunicationAgent
    from app.workflows.followup import run_follow_ups

    config, settings, profile = get_config(), get_settings(), get_profile()
    llm = get_llm(profile)
    with session_scope() as s:
        report = run_follow_ups(s, config, settings, CommunicationAgent(llm, profile))
    console.print(f"sent={report.sent} blocked={report.blocked} errors={report.errors}")


def cmd_search_plan(_args) -> None:
    from app.workflows.search_plan import SearchPlan

    plan = SearchPlan(get_preferences()).build()
    t = Table(title="Search plan")
    t.add_column("Country")
    t.add_column("Query")
    t.add_column("Lang")
    for combo in plan:
        t.add_row(combo["country"], combo["query"], combo["lang"])
    console.print(t)


def cmd_stats(_args) -> None:
    with session_scope() as s:
        st = mem.store.stats(s)
    t = Table(title="Worldwide Career Agent")
    t.add_column("Metric")
    t.add_column("Value")
    for k, v in st.items():
        if k == "last_events":
            continue
        t.add_row(k.replace("_", " ").title(), str(v))
    console.print(t)


def cmd_job(args) -> None:
    """Print complete on-demand details without sending any notification."""
    from app.notifications.service import NotificationService
    with session_scope() as s:
        detail = NotificationService(s).details(args.opportunity_id)
    if not detail:
        console.print("[yellow]Opportunity not found.[/yellow]")
        raise SystemExit(1)
    console.print_json(json.dumps(detail, ensure_ascii=False))


def cmd_pause(_args) -> None:
    from app.scheduler.control import set_paused

    set_paused(True)
    console.print("[yellow]Agent paused.[/yellow]")


def cmd_resume(_args) -> None:
    from app.scheduler.control import set_paused

    set_paused(False)
    console.print("[green]Agent resumed.[/green]")


def cmd_scheduler(_args) -> None:
    from app.scheduler.runner import serve_forever

    serve_forever()


def cmd_dashboard(args) -> None:
    import uvicorn

    uvicorn.run("app.api.main:app", host=args.host, port=args.port, reload=False)


def cmd_gmail_auth(_args) -> None:
    """One-time interactive OAuth authorization for the active email mode
    (draft -> gmail.compose; live -> send, compose fallback, and read-only
    Sent-mail reconciliation). Prints only WHERE the token was stored.
    """
    from app.config import get_settings
    from app.email import gmail_oauth

    try:
        path = gmail_oauth.authorize(get_settings())
    except gmail_oauth.OAuthError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc
    console.print(f"[green]Gmail authorized for mode {get_settings().email_mode}. "
                  f"Token stored at {path}[/green]")
    console.print("[yellow]Gmail drafts/sending still disabled until ENABLE_EMAIL=true in .env.[/yellow]")


def cmd_gmail_status(_args) -> None:
    """Report OAuth configuration WITHOUT exposing credentials."""
    from app.config import get_settings
    from app.email import gmail_oauth

    settings = get_settings()
    secret = gmail_oauth.client_secret_path(settings)
    token = gmail_oauth.token_path(settings)
    console.print(f"client secret: {'present' if secret.exists() else 'MISSING'} ({secret})")
    console.print(f"token:         {'present' if token.exists() else 'MISSING'} ({token})")
    console.print(f"scopes required to send: {' '.join(gmail_oauth.required_scopes(settings))}")
    optional = gmail_oauth.optional_scopes(settings)
    if optional:
        granted = gmail_oauth.granted_scopes(settings)
        for scope in optional:
            state = "granted" if scope in granted else "missing"
            console.print(f"optional automatic-reconciliation scope ({state}): {scope}")


def cmd_email_policy(_args) -> None:
    """Show the current outbound communication policy (no secrets)."""
    from app.config import get_settings
    from app.scheduler.control import is_paused

    s = get_settings()
    console.print("Outbound communication policy")
    console.print(f"  mode:                 {s.email_mode}      (dry_run | draft | live)")
    console.print(f"  ENABLE_EMAIL:         {'true' if s.enable_email else 'false'}")
    console.print(f"  paused (kill switch): {'YES' if is_paused() else 'no'}")
    console.print(f"  daily applications:   {s.daily_max_applications}")
    console.print(f"  daily inquiries:      {s.daily_max_inquiries}")
    console.print(f"  daily total outbound: {s.daily_max_outbound}")
    console.print(f"  employer cooldown:    {s.employer_cooldown_days} days")
    console.print(f"  min apply score:      {s.min_application_score}/100")
    console.print(f"  min apply confidence: {s.min_application_confidence}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="wca", description="Worldwide AI Job & Relocation Agent")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")
    run_once = sub.add_parser("run-once")
    run_once.add_argument("--json", action="store_true", help="machine-readable output")
    discover = sub.add_parser("discover", help="run real public-source discovery (no outbound email)")
    discover_mode = discover.add_mutually_exclusive_group()
    discover_mode.add_argument("--real", action="store_true", help="explicitly select production public sources")
    discover_mode.add_argument("--demo", action="store_true", help="use offline demo fixtures only")
    discovery_only = sub.add_parser(
        "discovery-only",
        help="run bounded discovery and write a sanitized report without outreach",
    )
    discovery_only.add_argument(
        "--report", type=Path,
        default=ROOT_DIR / "data" / "discovery_only_report.json",
    )
    btp = sub.add_parser(
        "btp-outreach",
        help="prepare draft-only spontaneous applications to nearby Moroccan BTP companies",
    )
    btp.add_argument("--origin-city", choices=("Casablanca",), default="Casablanca")
    btp.add_argument("--max-companies", type=int, default=20)
    from app.workflows.btp_outreach import TARGET_EMPLOYERS
    btp.add_argument("--target-employer", choices=tuple(TARGET_EMPLOYERS))
    btp.add_argument(
        "--preflight-only",
        action="store_true",
        help="targeted dry-run that rolls back database changes and does not persist screening state",
    )
    history_preflight = sub.add_parser(
        "history-preflight",
        help="read-only employer application and outreach history preflight",
    )
    history_preflight.add_argument("--database", type=Path,
                                   default=ROOT_DIR / "data" / "agent.db")
    history_preflight.add_argument("--screening-state", type=Path,
                                   default=ROOT_DIR / "data" / "btp_outreach_state.json")
    history_preflight.add_argument("--profile", type=Path,
                                   default=ROOT_DIR / "candidate" / "profile.yaml")
    history_preflight.add_argument("--preferences", type=Path,
                                   default=ROOT_DIR / "candidate" / "preferences.yaml")
    history_preflight.add_argument("--restore-status", type=Path,
                                   default=ROOT_DIR / "data" / "preflight_restore_status.json")
    from app.workflows.history_preflight import EMPLOYERS
    history_preflight.add_argument(
        "--employer",
        action="append",
        choices=tuple(EMPLOYERS),
        help="inspect only this supported employer; repeat to select multiple",
    )
    for name in ("analyze", "act", "followups", "search-plan",
                 "stats", "pause", "resume", "scheduler"):
        sub.add_parser(name)
    job = sub.add_parser("job", help="show complete details for an opportunity ID")
    job.add_argument("opportunity_id", help="e.g. JOB-2026-0817-0042")
    dash = sub.add_parser("dashboard")
    dash.add_argument("--host", default="127.0.0.1")
    dash.add_argument("--port", default=8000, type=int)

    sub.add_parser("gmail-auth", help="one-time Gmail OAuth authorization for the current email mode")
    sub.add_parser("gmail-status", help="show Gmail OAuth status without exposing secrets")
    sub.add_parser("email-policy", help="show current outbound communication policy")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    handlers = {
        "init": cmd_init,
        "run-once": cmd_run_once,
        "discover": cmd_discover,
        "discovery-only": cmd_discovery_only,
        "btp-outreach": cmd_btp_outreach,
        "history-preflight": cmd_history_preflight,
        "analyze": cmd_analyze,
        "act": cmd_act,
        "followups": cmd_followups,
        "search-plan": cmd_search_plan,
        "stats": cmd_stats,
        "job": cmd_job,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "scheduler": cmd_scheduler,
        "dashboard": cmd_dashboard,
        "gmail-auth": cmd_gmail_auth,
        "gmail-status": cmd_gmail_status,
        "email-policy": cmd_email_policy,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
