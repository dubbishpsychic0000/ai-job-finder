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
import sys

from rich.console import Console
from rich.table import Table

from app import memory as mem
from app.agents.immigration_agent import ImmigrationAgent
from app.agents.llm import get_llm
from app.config import get_config, get_preferences, get_profile, get_settings
from app.database import init_db, session_scope

console = Console()


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
        },
        "followup": result.followup,
    }
    if args.json:
        console.print(json.dumps(data, indent=2))
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
    t.add_row("Follow-ups", f"sent {data['followup'].get('sent', 0)}, blocked {data['followup'].get('blocked', 0)}")
    console.print(t)


def cmd_discover(_args) -> None:
    from app.workflows.discovery import run_discovery

    config, prefs = get_config(), get_preferences()
    with session_scope() as s:
        report = asyncio.run(run_discovery(s, config, prefs))
    console.print(f"found {report.new_jobs} new jobs, {report.duplicates} dupes, errors={report.source_errors}")


def cmd_analyze(_args) -> None:
    from app.workflows.analysis import run_analysis

    config, prefs, profile = get_config(), get_preferences(), get_profile()
    llm = get_llm(profile)
    with session_scope() as s:
        report = asyncio.run(run_analysis(s, config, profile, llm, prefs.countries))
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
    (draft -> gmail.compose; live -> gmail.send). Prints only WHERE the token
    was stored — never the token or the client secret.
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
    console.print(f"scopes needed: {' '.join(gmail_oauth.required_scopes(settings))}")


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
    for name in ("discover", "analyze", "act", "followups", "search-plan",
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
