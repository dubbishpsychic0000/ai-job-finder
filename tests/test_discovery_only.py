from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

from app.config import AgentConfig, CandidateProfile, Preferences
from app.workflows import discovery_only
from app.workflows.discovery import DiscoveryReport


def test_bounded_discovery_invokes_discovery_only_with_fixed_caps(monkeypatch):
    seen = {}

    async def fake_run_discovery(session, config, preferences, **kwargs):
        seen.update({
            "session": session,
            "config": config,
            "preferences": preferences,
            **kwargs,
        })
        return DiscoveryReport()

    monkeypatch.setattr(discovery_only, "run_discovery", fake_run_discovery)
    config = AgentConfig(discovery={
        "max_queries_per_run": 60,
        "max_api_requests_per_run": 17,
        "website_research": True,
        "company_universe_discovery": True,
        "social_signal_discovery": True,
        "vocab_llm": True,
    })
    preferences = Preferences(target_countries=["Morocco"])
    profile = CandidateProfile(name="Private Candidate", phone="+212 600 000 000")
    session = object()

    result = asyncio.run(discovery_only.run_bounded_discovery(
        session, config, preferences, profile,
    ))

    assert isinstance(result, DiscoveryReport)
    assert seen["session"] is session
    assert seen["preferences"] is preferences
    assert seen["profile"] is profile
    assert seen["llm"] is None
    assert seen["max_queries_override"] == 12
    assert seen["max_api_attempts_override"] == 8
    assert seen["max_total_source_queries"] == 12
    assert all(not seen["config"].discovery[name] for name in (
        "website_research", "company_universe_discovery",
        "social_signal_discovery", "vocab_llm",
    ))
    assert config.discovery["website_research"] is True


def test_budget_ledger_validation_requires_and_preserves_restored_usage(tmp_path):
    key = "opaque-provider-key"
    from app.connectors.tavily_resilience import key_fingerprint

    ledger = tmp_path / f"tavily_budget_{key_fingerprint(key)}.json"
    assert discovery_only.validate_budget_ledgers(tmp_path, [key]) == (False, 0)

    ledger.write_text(json.dumps({
        "date": "2026-10-13",
        "count": 14,
        "exhausted": False,
    }), encoding="utf-8")
    assert discovery_only.validate_budget_ledgers(tmp_path, [key]) == (True, 1)
    assert json.loads(ledger.read_text(encoding="utf-8"))["count"] == 14

    ledger.write_text('{"date":"2026-10-13","count":"unknown","exhausted":false}')
    assert discovery_only.validate_budget_ledgers(tmp_path, [key]) == (False, 0)


def test_discovery_report_is_sanitized_and_distinguishes_cached_new_listing():
    job = SimpleNamespace(
        id=1,
        title="Civil engineering technician internship",
        company=SimpleNamespace(name="Public Employer"),
        url="https://jobs.example.org/listing/42?access_token=secret-value",
        country="France",
        search_country="France",
        location="Lyon",
        posted_at=None,
        closing_at=None,
        opportunity_type="INTERNSHIP",
        source="tavily_web_search",
        source_confidence=65,
        search_language="fr",
        language="",
        application_method="UNKNOWN",
        application_url="",
        description=(
            "Applicants must be currently enrolled. AutoCAD required; "
            "minimum 2 years. Contact hr@employer.example or +33 123 456 789."
        ),
        sponsorship_signal="unknown",
    )
    discovery_report = DiscoveryReport(
        opportunities_fetched=1,
        new_jobs=1,
        source_reports=[{
            "name": "tavily_web_search",
            "kind": "tavily",
            "health": "ok",
            "queries": 1,
            "queries_api": 0,
            "queries_cached": 1,
            "queries_failed": 0,
            "queries_budget_deferred": 0,
            "queries_run_budget_deferred": 0,
            "queries_stale_cache": 0,
            "fetched": 1,
            "fetched_api_results": 0,
            "fetched_cache_results": 1,
            "normalized": 1,
            "duplicates": 0,
            "new": 1,
            "duplicate_reasons": {},
            "query_error_codes": {},
            "queries_by_country": {"France": 1},
            "queries_by_language": {"fr": 1},
        }],
        api_request_budget={
            "limit": 8,
            "attempts": 0,
            "successes": 0,
            "failed_attempts": 0,
            "retry_attempts": 0,
            "remaining": 8,
        },
        new_opportunity_origins={1: "cache"},
    )

    output = discovery_only.build_sanitized_report(
        discovery_report,
        [job],
        AgentConfig(rules={"max_days_since_posted": 45}),
        {"limit_per_key": 17, "remaining_by_key": [17]},
        now=datetime(2026, 10, 13, tzinfo=timezone.utc),
    )
    serialized = json.dumps(output, ensure_ascii=False)
    opportunity = output["opportunities"][0]

    assert output["counts"]["unique_new_opportunities"] == 1
    assert output["counts"]["new_from_cache"] == 1
    assert opportunity["result_origin"] == "CACHE"
    assert opportunity["date_status"] == "UNVERIFIABLE_DATE"
    assert opportunity["eligibility"] == "NOT_EVALUATED"
    assert opportunity["listing_url"] == "https://jobs.example.org/listing/42"
    assert "AutoCAD" in serialized
    assert "+212 600 000 000" not in serialized
    assert "hr@employer.example" not in serialized
    assert "123 456 789" not in serialized
    assert "secret-value" not in serialized
    assert "candidate profile" not in opportunity["discovery_relevance"]
    assert "candidate fit was not evaluated" in opportunity["discovery_relevance"]


def test_workflow_discovery_task_skips_outbound_vault_and_full_pipeline_steps():
    repo_root = Path(__file__).resolve().parents[1]
    workflow = yaml.load(
        (repo_root / ".github" / "workflows" / "agent.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    task_input = workflow["on"]["workflow_dispatch"]["inputs"]["task"]
    assert "discovery_only" in task_input["options"]

    steps = {
        step.get("name"): step for step in workflow["jobs"]["pipeline"]["steps"]
    }
    for name in (
        "Restore candidate data, OAuth secrets, and state (private vault)",
        "Build .env from vault and secrets",
        "Reset ephemeral runtime state",
        "Run selected workflow",
        "Send WhatsApp run status",
        "Send WhatsApp connection test",
        "Persist state back to vault",
        "Upload run artifacts",
        "Upload run logs",
    ):
        assert "discovery_only" in steps[name]["if"]

    run_step = steps["Run bounded discovery-only evaluation"]
    assert "inputs.task == 'discovery_only'" in run_step["if"]
    assert "run-once" not in run_step["run"]
    assert run_step["env"]["TAVILY_DEBUG"] == "0"
    assert run_step["env"]["GEMINI_API_KEYS"] == ""
    assert run_step["env"]["WHATSAPP_ACCESS_TOKEN"] == ""
    assert run_step["env"]["ENABLE_EMAIL"] == "false"
    assert run_step["env"]["EMAIL_PROVIDER"] == "log"
    assert run_step["env"]["AUTHORIZE_LIVE_SPONTANEOUS"] == "false"
    assert "discovery_only_report.json" in steps[
        "Upload sanitized discovery-only report"
    ]["with"]["path"]
    assert "discovery_only" not in steps["Run BTP spontaneous-application workflow"]["if"]
    assert "inputs.task == 'pipeline'" in steps["Run selected workflow"]["if"]
    job_env = workflow["jobs"]["pipeline"]["env"]
    assert "inputs.task == 'discovery_only') && 'false'" in job_env[
        "AUTHORIZE_LIVE_SPONTANEOUS"
    ]

    restore = steps["Restore bounded discovery inputs only"]
    assert "state/agent.db" in restore["run"]
    assert "candidate/profile.yaml" in restore["run"]
    assert "candidate/preferences.yaml" in restore["run"]
    assert "secrets/tavily_keys.txt" in restore["run"]
    assert "secrets/." not in restore["run"]
    assert "candidate/cv" not in restore["run"]

    persist_condition = steps["Persist state back to vault"]["if"]
    assert "inputs.task != 'discovery_only'" in persist_condition
    assert "upload sanitized discovery-only report" in {
        name.casefold() for name in steps
    }
