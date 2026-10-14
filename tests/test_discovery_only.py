from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
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
    assert seen["max_search_combinations"] == 12
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


def test_budget_ledger_validation_deduplicates_identical_keys(tmp_path):
    from app.connectors.tavily_resilience import key_fingerprint

    key = "duplicate-key"
    ledger = tmp_path / f"tavily_budget_{key_fingerprint(key)}.json"
    ledger.write_text(json.dumps({
        "date": "2026-10-13", "count": 4, "exhausted": False,
    }), encoding="utf-8")

    assert discovery_only.validate_budget_ledgers(tmp_path, [key, key]) == (True, 1)


def _prepare_cli_inputs(root: Path, database_path: Path) -> None:
    (root / "data").mkdir(parents=True)
    (root / "candidate").mkdir()
    (root / "secrets").mkdir()
    (root / "data" / "discovery_only_inputs_status.json").write_text(
        '{"ready":true}', encoding="utf-8",
    )
    (root / "candidate" / "profile.yaml").write_text(
        "name: Test Candidate\n", encoding="utf-8",
    )
    (root / "candidate" / "preferences.yaml").write_text(
        "target_countries: [Morocco]\n", encoding="utf-8",
    )
    connection = sqlite3.connect(database_path)
    try:
        for table in (
            "jobs", "companies", "sources", "query_stats", "discoveries",
            "notifications",
        ):
            connection.execute(f"CREATE TABLE {table} (id INTEGER)")
    finally:
        connection.close()


def _run_discovery_cli(
    monkeypatch, root, database_path, *, run_discovery=None, parse_candidate=False,
):
    from app import cli

    class SessionStub:
        def scalar(self, _statement):
            return 0

        def flush(self):
            pass

        def scalars(self, _statement):
            return iter(())

    @contextmanager
    def session_scope_stub():
        yield SessionStub()

    monkeypatch.setattr(cli, "ROOT_DIR", root)
    monkeypatch.setattr(
        cli, "get_settings",
        lambda: SimpleNamespace(sqlite_path=database_path),
    )
    monkeypatch.setattr(cli, "get_config", lambda: AgentConfig())
    monkeypatch.setattr(
        cli, "session_scope", session_scope_stub,
    )
    if not parse_candidate:
        monkeypatch.setattr(
            cli, "get_profile", lambda _path: CandidateProfile(name="Fixture"),
        )
        monkeypatch.setattr(
            cli, "get_preferences", lambda _path: Preferences(),
        )
    if run_discovery is not None:
        monkeypatch.setattr(
            discovery_only, "run_bounded_discovery", run_discovery,
        )
    report_path = root / "report.json"
    cli.cmd_discovery_only(SimpleNamespace(report=report_path))
    return json.loads(report_path.read_text(encoding="utf-8"))


def test_discovery_execution_failure_has_sanitized_phase_diagnostics(
    tmp_path, monkeypatch,
):
    root = tmp_path / "repo"
    database_path = root / "data" / "agent.db"
    _prepare_cli_inputs(root, database_path)
    secret_value = "provider-secret candidate@example.test private-candidate"

    async def fail_discovery(*_args, **_kwargs):
        raise RuntimeError(secret_value)

    report = _run_discovery_cli(
        monkeypatch, root, database_path, run_discovery=fail_discovery,
        parse_candidate=False,
    )
    serialized = json.dumps(report)

    assert report["status"] == "BLOCKED"
    assert report["phase"] == "discovery_execution"
    assert report["reason_code"] == "discovery_execution_failed"
    assert report["diagnostic"]["exception_class"] == "RuntimeError"
    assert report["diagnostic"]["message"] == (
        "Bounded discovery failed before a complete report was produced."
    )
    assert secret_value not in serialized
    assert "provider-secret" not in serialized
    assert "candidate@example.test" not in serialized


@pytest.mark.parametrize(
    ("missing", "expected_reason", "expected_phase"),
    [
        ("profile", "candidate_search_preferences_missing", "profile_parse"),
        ("preferences", "candidate_search_preferences_missing", "profile_parse"),
        ("database", "production_database_snapshot_missing", "database_check"),
    ],
)
def test_missing_discovery_inputs_fail_closed(
    tmp_path, monkeypatch, missing, expected_reason, expected_phase,
):
    root = tmp_path / "repo"
    database_path = root / "data" / "agent.db"
    _prepare_cli_inputs(root, database_path)
    if missing == "profile":
        (root / "candidate" / "profile.yaml").unlink()
    elif missing == "preferences":
        (root / "candidate" / "preferences.yaml").unlink()
    else:
        database_path.unlink()

    report = _run_discovery_cli(monkeypatch, root, database_path)

    assert report["status"] == "BLOCKED"
    assert report["reason_code"] == expected_reason
    assert report["phase"] == expected_phase


def test_corrupt_production_database_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    database_path = root / "data" / "agent.db"
    _prepare_cli_inputs(root, database_path)
    database_path.write_bytes(b"not a sqlite database")

    report = _run_discovery_cli(monkeypatch, root, database_path)

    assert report["status"] == "BLOCKED"
    assert report["phase"] == "database_check"
    assert report["reason_code"] == "production_database_unreadable"


@pytest.mark.parametrize(
    ("ledger_payload", "expected_valid"),
    [
        (None, False),
        ('{"date":"2026-10-13","count":"bad","exhausted":false}', False),
        ('{"date":"2026-10-13","count":1,"exhausted":false}', True),
    ],
)
def test_invalid_or_missing_ledger_inputs_fail_closed(
    tmp_path, monkeypatch, ledger_payload, expected_valid,
):
    from app.connectors.tavily_resilience import key_fingerprint

    root = tmp_path / "repo"
    database_path = root / "data" / "agent.db"
    _prepare_cli_inputs(root, database_path)
    key = "fixture-provider-secret"
    (root / "secrets" / "tavily_keys.txt").write_text(key, encoding="utf-8")
    ledger = root / "data" / f"tavily_budget_{key_fingerprint(key)}.json"
    if ledger_payload is not None:
        ledger.write_text(ledger_payload, encoding="utf-8")
    monkeypatch.setenv("TAVILY_BUDGET_VAULT_REPO", "")

    report = _run_discovery_cli(
        monkeypatch, root, database_path, parse_candidate=False,
    )

    assert report["status"] == "BLOCKED"
    assert report["phase"] == "budget_validation"
    assert report["reason_code"] == (
        "provider_budget_persistence_unavailable"
        if expected_valid else "provider_budget_ledger_missing_or_invalid"
    )
    assert key not in json.dumps(report)


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("profile.yaml", "name: [invalid\n"),
        ("preferences.yaml", "target_countries: not-a-list\n"),
    ],
)
def test_corrupt_candidate_inputs_fail_closed(
    tmp_path, monkeypatch, filename, content,
):
    root = tmp_path / "repo"
    database_path = root / "data" / "agent.db"
    _prepare_cli_inputs(root, database_path)
    (root / "candidate" / filename).write_text(content, encoding="utf-8")

    report = _run_discovery_cli(
        monkeypatch, root, database_path, parse_candidate=True,
    )

    assert report["status"] == "BLOCKED"
    assert report["phase"] == "profile_parse"
    assert report["reason_code"] == "candidate_profile_or_preferences_invalid"


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


def test_zero_result_report_distinguishes_empty_from_deferred_queries():
    report = DiscoveryReport(
        combinations_attempted=1,
        source_requests_attempted=1,
        source_reports=[
            {
                "name": "broad",
                "kind": "tavily",
                "health": "partial_budget",
                "result_state": "successful_empty_response",
                "request_scope": {
                    "applies_to_countries": ["*"],
                    "domains": [],
                    "official_only": False,
                    "request_scope_id": "scope-id",
                },
                "queries": 1,
                "queries_api": 1,
                "queries_empty": 1,
                "live_empty_responses": 1,
                "cached_empty_results": 0,
                "queries_filtered": 0,
                "queries_failed": 0,
                "queries_budget_deferred": 2,
                "queries_run_budget_deferred": 0,
                "queries_by_country": {"Morocco": 1},
                "queries_by_language": {"fr": 1},
                "provider_attempts_by_country": {"Morocco": 1},
                "provider_attempts_by_language": {"fr": 1},
                "cache_hits_by_country": {},
                "cache_hits_by_language": {},
                "inapplicable_country_queries": {},
                "provider_results_returned": 0,
                "provider_results_filtered": 0,
                "cached_provider_results_filtered": 0,
            },
            {
                "name": "morocco-board",
                "kind": "tavily",
                "health": "not_run",
                "result_state": "not_attempted",
                "request_scope": {
                    "applies_to_countries": ["Morocco"],
                    "domains": ["rekrute.com"],
                    "official_only": False,
                    "request_scope_id": "another-scope",
                },
                "queries": 0,
                "queries_empty": 0,
                "queries_failed": 0,
                "queries_budget_deferred": 0,
                "queries_run_budget_deferred": 0,
                "queries_by_country": {},
                "queries_by_language": {},
                "inapplicable_country_queries": {"France": 4},
            },
        ],
        api_request_budget={
            "limit": 8,
            "attempts": 1,
            "successes": 1,
            "failed_attempts": 0,
            "retry_attempts": 0,
            "remaining": 7,
        },
    )

    output = discovery_only.build_sanitized_report(
        report,
        [],
        AgentConfig(),
        {"limit_per_key": 17, "remaining_by_key": [16]},
    )

    assert output["query_outcome"] == {
        "result_state": "all_live_provider_responses_successfully_empty",
        "source_requests_attempted": 1,
        "successful_empty_responses": 1,
        "cached_empty_results": 0,
        "failed_requests": 0,
        "requests_not_attempted_due_to_budget": 2,
        "source_country_combinations_inapplicable": 4,
        "provider_results_returned": 0,
        "provider_results_filtered": 0,
        "cached_provider_results_filtered": 0,
    }
    assert output["counts"]["search_combinations_attempted"] == 1
    assert output["counts"]["source_requests_attempted"] == 1
    assert output["sources"][0]["queried_by_country"] == {"Morocco": 1}
    assert output["sources"][0]["provider_attempts_by_country"] == {"Morocco": 1}
    assert output["sources"][1]["inapplicable_country_queries"] == {"France": 4}


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
    assert "wca-history-vault" not in restore["run"]

    persist_condition = steps["Persist state back to vault"]["if"]
    assert "inputs.task != 'discovery_only'" in persist_condition
    assert "TAVILY_BUDGET_VAULT_REPO" in run_step["env"]
    assert 'get("status") == "COMPLETED"' in run_step["run"]
    assert "upload sanitized discovery-only report" in {
        name.casefold() for name in steps
    }
