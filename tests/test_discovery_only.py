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
    (root / "candidate" / "profile.yaml").write_text(
        "name: Test Candidate\n", encoding="utf-8",
    )
    (root / "candidate" / "preferences.yaml").write_text(
        "target_countries: [Morocco]\n", encoding="utf-8",
    )
    from app import models

    connection = sqlite3.connect(database_path)
    try:
        for table in models.Base.metadata.tables:
            quoted = table.replace('"', '""')
            connection.execute(f'CREATE TABLE "{quoted}" (id INTEGER)')
    finally:
        connection.close()
    from app.connectors.tavily_resilience import key_fingerprint

    key = "fixture-provider-key"
    (root / "secrets" / "tavily_keys.txt").write_text(key, encoding="utf-8")
    (root / "data" / f"tavily_budget_{key_fingerprint(key)}.json").write_text(
        '{"date":"2026-10-14","count":0,"exhausted":false}',
        encoding="utf-8",
    )
    (root / "data" / "discovery_only_inputs_status.json").write_text(
        json.dumps({
            "schema_version": 1,
            "ready": True,
            "reason_code": "",
            "vault_checkout_path": str((root / "vault").resolve()),
            "vault_commit": "a" * 40,
            "vault_repository_validated": True,
            "vault_ref_validated": True,
            "database_snapshot_restored": True,
            "candidate_search_profile_restored": True,
            "provider_keys_restored": True,
            "provider_budget_ledgers_restored": True,
            "provider_key_count": 1,
            "provider_budget_ledger_count": 1,
            "expected_input_count": 4,
            "restored_input_count": 4,
            "expected_budget_ledger_count": 1,
            "restored_budget_ledger_count": 1,
            "provider_cache_restored": False,
        }),
        encoding="utf-8",
    )


def _run_discovery_cli(
    monkeypatch, root, database_path, *, run_discovery=None, parse_candidate=False,
    database_initializer=None, vault_configured=False,
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
        cli, "init_db", database_initializer or (lambda: None),
    )
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
    if vault_configured:
        from app.workflows import tavily_budget_vault

        class BudgetStoreStub:
            reservation_count = 0
            write_count = 0

            def __init__(self, _repository):
                pass

            def synchronize(self, _path, *, today):
                assert today == datetime.now(timezone.utc).date().isoformat()

        monkeypatch.setenv(
            "TAVILY_BUDGET_VAULT_REPO", str((root / "vault").resolve()),
        )
        monkeypatch.setattr(
            tavily_budget_vault, "GitVaultBudgetStore", BudgetStoreStub,
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
    migrations = []

    async def fail_discovery(*_args, **_kwargs):
        raise RuntimeError(secret_value)

    report = _run_discovery_cli(
        monkeypatch, root, database_path, run_discovery=fail_discovery,
        parse_candidate=False,
        database_initializer=lambda: migrations.append("initialized"),
        vault_configured=True,
    )
    serialized = json.dumps(report)

    assert report["status"] == "BLOCKED"
    assert report["phase"] == "discovery_execution"
    assert report["reason_code"] == "discovery_execution_failed"
    assert report["diagnostic"]["exception_class"] == "RuntimeError"
    assert report["diagnostic"]["message"] == (
        "Bounded discovery failed before a complete report was produced."
    )
    assert migrations == ["initialized"]
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


def test_absent_or_invalid_input_manifest_blocks_before_discovery(
    tmp_path, monkeypatch,
):
    root = tmp_path / "repo"
    database_path = root / "data" / "agent.db"
    _prepare_cli_inputs(root, database_path)
    status_path = root / "data" / "discovery_only_inputs_status.json"
    status_path.unlink()
    dispatched = []

    async def forbidden_discovery(*_args, **_kwargs):
        dispatched.append(True)
        return DiscoveryReport()

    report = _run_discovery_cli(
        monkeypatch, root, database_path, run_discovery=forbidden_discovery,
    )

    assert report["status"] == "BLOCKED"
    assert report["phase"] == "input_restore"
    assert report["reason_code"] == "discovery_input_status_missing"
    assert dispatched == []

    status_path.write_text('{"ready":true}', encoding="utf-8")
    report = _run_discovery_cli(
        monkeypatch, root, database_path, run_discovery=forbidden_discovery,
    )
    assert report["status"] == "BLOCKED"
    assert report["reason_code"] == "discovery_input_status_invalid"
    assert dispatched == []


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("restored_input_count", 3, "restored_input_count_mismatch"),
        (
            "restored_budget_ledger_count",
            0,
            "restored_budget_ledger_count_mismatch",
        ),
    ],
)
def test_incomplete_restore_counts_block_before_discovery(
    tmp_path, monkeypatch, field, value, expected_reason,
):
    root = tmp_path / "repo"
    database_path = root / "data" / "agent.db"
    _prepare_cli_inputs(root, database_path)
    status_path = root / "data" / "discovery_only_inputs_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status[field] = value
    status_path.write_text(json.dumps(status), encoding="utf-8")
    dispatched = []

    async def forbidden_discovery(*_args, **_kwargs):
        dispatched.append(True)
        return DiscoveryReport()

    report = _run_discovery_cli(
        monkeypatch, root, database_path, run_discovery=forbidden_discovery,
    )

    assert report["status"] == "BLOCKED"
    assert report["reason_code"] == expected_reason
    assert dispatched == []


@pytest.mark.parametrize(
    "reason",
    [
        "vault_repository_configuration_missing",
        "vault_origin_mismatch",
        "vault_ref_mismatch",
        "vault_repository_path_mismatch",
        "provider_budget_ledger_missing_or_invalid",
    ],
)
def test_restore_failure_manifest_blocks_before_discovery(
    tmp_path, monkeypatch, reason,
):
    root = tmp_path / "repo"
    database_path = root / "data" / "agent.db"
    _prepare_cli_inputs(root, database_path)
    status_path = root / "data" / "discovery_only_inputs_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status.update({
        "ready": False,
        "reason_code": reason,
        "vault_repository_validated": False,
        "vault_ref_validated": False,
        "provider_budget_ledgers_restored": False,
    })
    status_path.write_text(json.dumps(status), encoding="utf-8")
    dispatched = []

    async def forbidden_discovery(*_args, **_kwargs):
        dispatched.append(True)
        return DiscoveryReport()

    report = _run_discovery_cli(
        monkeypatch, root, database_path, run_discovery=forbidden_discovery,
    )

    assert report["status"] == "BLOCKED"
    assert report["reason_code"] == reason
    assert dispatched == []


def test_blocked_restore_report_exposes_only_safe_ledger_diagnostics(
    tmp_path, monkeypatch,
):
    from app.connectors.tavily_resilience import key_fingerprint

    root = tmp_path / "repo"
    database_path = root / "data" / "agent.db"
    _prepare_cli_inputs(root, database_path)
    key = "fixture-provider-secret"
    fingerprint = key_fingerprint(key)
    status_path = root / "data" / "discovery_only_inputs_status.json"
    status = json.loads(status_path.read_text(encoding="utf-8"))
    status.update({
        "ready": False,
        "reason_code": "provider_budget_ledger_missing_or_invalid",
        "ledger_diagnostics": [{
            "fingerprint": fingerprint,
            "outcome": "ledger_json_invalid",
        }],
    })
    status_path.write_text(json.dumps(status), encoding="utf-8")
    dispatched = []

    async def forbidden_discovery(*_args, **_kwargs):
        dispatched.append(True)
        return DiscoveryReport()

    report = _run_discovery_cli(
        monkeypatch, root, database_path, run_discovery=forbidden_discovery,
    )

    assert report["status"] == "BLOCKED"
    assert report["phase"] == "input_restore"
    assert report["provider"]["ledger_diagnostics"] == [{
        "fingerprint": fingerprint,
        "outcome": "ledger_json_invalid",
    }]
    assert key not in json.dumps(report)
    assert str(root) not in json.dumps(report)
    assert dispatched == []


def test_vault_checkout_path_mismatch_blocks_before_discovery(
    tmp_path, monkeypatch,
):
    root = tmp_path / "repo"
    database_path = root / "data" / "agent.db"
    _prepare_cli_inputs(root, database_path)
    (root / "data" / "discovery_only_inputs_status.json").write_text(
        json.dumps({
            **json.loads(
                (root / "data" / "discovery_only_inputs_status.json")
                .read_text(encoding="utf-8"),
            ),
            "vault_checkout_path": str((root / "different-vault").resolve()),
        }),
        encoding="utf-8",
    )
    dispatched = []

    async def forbidden_discovery(*_args, **_kwargs):
        dispatched.append(True)
        return DiscoveryReport()

    report = _run_discovery_cli(
        monkeypatch,
        root,
        database_path,
        run_discovery=forbidden_discovery,
        vault_configured=True,
    )

    assert report["status"] == "BLOCKED"
    assert report["reason_code"] == "vault_repository_path_mismatch"
    assert dispatched == []


def test_corrupt_production_database_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    database_path = root / "data" / "agent.db"
    _prepare_cli_inputs(root, database_path)
    database_path.write_bytes(b"not a sqlite database")

    report = _run_discovery_cli(
        monkeypatch, root, database_path, vault_configured=True,
    )

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
    dispatched = []

    async def forbidden_discovery(*_args, **_kwargs):
        dispatched.append(True)
        return DiscoveryReport()

    report = _run_discovery_cli(
        monkeypatch,
        root,
        database_path,
        parse_candidate=False,
        run_discovery=forbidden_discovery,
    )

    assert report["status"] == "BLOCKED"
    assert report["phase"] == "budget_validation"
    assert report["reason_code"] == (
        "provider_budget_persistence_unavailable"
        if expected_valid else "provider_budget_ledger_missing_or_invalid"
    )
    assert key not in json.dumps(report)
    assert dispatched == []


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
        posted_at_source="workday:postedOn",
        posted_at_evidence_url=(
            "https://jobs.example.org/listing/42?access_token=secret-value"
        ),
        closing_at=None,
        discovered_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
        opportunity_type="INTERNSHIP",
        entity_type="INDIVIDUAL_VACANCY",
        entity_state="VACANCY_UNVERIFIED",
        entity_reason_code="specific_listing_url_unverified",
        entity_evidence_url=(
            "https://jobs.example.org/listing/42?access_token=secret-value"
        ),
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

    assert output["schema_version"] == 4
    assert output["counts"]["unique_new_opportunities"] == 0
    assert output["counts"]["unique_new_candidate_vacancies"] == 1
    assert output["counts"]["unique_new_discovery_records"] == 1
    assert output["counts"]["new_from_cache"] == 1
    assert opportunity["result_origin"] == "CACHE"
    assert opportunity["date_status"] == "UNVERIFIABLE_DATE"
    assert opportunity["posting_date_evidence"]["status"] == "UNVERIFIABLE"
    assert opportunity["entity_state"] == "VACANCY_UNVERIFIED"
    assert opportunity["application_route"]["verified"] is False
    assert opportunity["posting_date_evidence"]["status"] == "UNVERIFIABLE"
    assert opportunity["posting_date_evidence"]["evidence_url"] == (
        "https://jobs.example.org/listing/42"
    )
    assert opportunity["entity_evidence_url"] == "https://jobs.example.org/listing/42"
    assert opportunity["eligibility"] == "NOT_EVALUATED"
    assert opportunity["listing_url"] == "https://jobs.example.org/listing/42"
    assert "AutoCAD" not in serialized
    assert "+212 600 000 000" not in serialized
    assert "hr@employer.example" not in serialized
    assert "123 456 789" not in serialized
    assert "secret-value" not in serialized
    assert "candidate profile" not in opportunity["discovery_relevance"]
    assert "candidate fit was not evaluated" in opportunity["discovery_relevance"]


def test_discovery_freshness_requires_source_date_evidence():
    from types import SimpleNamespace

    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    job = SimpleNamespace(
        posted_at=datetime(2026, 10, 12, tzinfo=timezone.utc),
        posted_at_source="",
        posted_at_evidence_url="",
        closing_at=None,
    )
    assert discovery_only._freshness(job, 45, now) == "UNVERIFIABLE_DATE"

    job.posted_at_source = "workday:postedOn"
    job.posted_at_evidence_url = "https://employer.example/jobs/123"
    assert discovery_only._freshness(job, 45, now) == "DATED_WITHIN_WINDOW"

    job.closing_at = datetime(2026, 10, 12, tzinfo=timezone.utc)
    assert discovery_only._freshness(job, 45, now) == "EXPIRED_BY_CLOSING_DATE"


def test_sanitized_report_can_serialize_new_jobs_after_session_closes(db):
    from app import models
    from app.cli import _load_new_discovery_jobs

    company = models.Company(name="Example Employer", normalized_name="example-employer")
    db.add(company)
    db.flush()
    job = models.Job(
        source="fixture",
        external_id="detached-job",
        dedup_key="detached-job",
        title="Civil Engineering Technician",
        company_id=company.id,
        country="Morocco",
        url="https://example.com/jobs/civil-technician",
    )
    db.add(job)
    db.commit()
    job_id = job.id

    loaded_jobs = _load_new_discovery_jobs(db, 0)
    db.expunge_all()

    output = discovery_only.build_sanitized_report(
        DiscoveryReport(
            opportunities_fetched=1,
            new_jobs=1,
            new_opportunity_origins={job_id: "api"},
        ),
        loaded_jobs,
        AgentConfig(),
        {"limit_per_key": 17, "remaining_by_key": [17]},
    )

    assert output["counts"]["unique_new_opportunities"] == 0
    assert output["counts"]["unique_new_candidate_vacancies"] == 0
    assert output["counts"]["unique_new_discovery_records"] == 1
    assert output["opportunities"][0]["employer"] == "Example Employer"


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
        "provider_results_by_country": {},
        "provider_results_by_language": {},
        "filter_reason_counts": {},
        "normalization_rejection_reasons": {},
        "deferred_requests_by_reason": {},
    }
    assert output["counts"]["search_combinations_attempted"] == 1
    assert output["counts"]["source_requests_attempted"] == 1
    assert output["sources"][0]["queried_by_country"] == {"Morocco": 1}
    assert output["sources"][0]["provider_attempts_by_country"] == {"Morocco": 1}
    assert output["sources"][1]["inapplicable_country_queries"] == {"France": 4}


def test_sanitized_report_exposes_filter_and_budget_reasons_without_raw_urls():
    source = {
        "name": "official",
        "kind": "tavily",
        "health": "partial_budget",
        "request_scope": {
            "applies_to_countries": ["*"],
            "domains": [],
            "official_only": True,
            "request_scope_id": "scope",
        },
        "queries": 1,
        "queries_api": 1,
        "provider_results_returned": 2,
        "provider_results_filtered": 1,
        "last_filter_reason_counts": {},
        "filter_reason_counts": {"unrecognized_official_ats_host": 1},
        "filtered_url_examples": [{
            "reason_code": "unrecognized_official_ats_host",
            "host": "careers.example.test",
            "path_pattern": "/jobs/{item}",
        }],
        "provider_results_by_country": {"Morocco": 2},
        "provider_results_by_language": {"fr": 2},
        "accepted_results_by_country": {"Morocco": 1},
        "accepted_results_by_language": {"fr": 1},
        "filtered_results_by_country": {"Morocco": 1},
        "filtered_results_by_language": {"fr": 1},
        "key_budget_events": {
            "a1b2c3d4": {"reserved": 1},
            "not-a-fingerprint": {"reserved": 99},
        },
        "queries_deferred_by_reason": {
            "run_attempt_cap": 4,
            "aggregate_daily_budget": 2,
        },
        "queries_by_country": {"Morocco": 1},
        "queries_by_language": {"fr": 1},
        "provider_attempts_by_country": {"Morocco": 1},
        "provider_attempts_by_language": {"fr": 1},
        "inapplicable_country_queries": {},
    }
    report = DiscoveryReport(
        source_requests_attempted=1,
        api_request_budget={
            "limit": 8,
            "attempts": 1,
            "successes": 1,
            "failed_attempts": 0,
            "retry_attempts": 0,
            "remaining": 7,
        },
        source_reports=[source],
    )

    output = discovery_only.build_sanitized_report(
        report, [], AgentConfig(), {"limit_per_key": 17, "remaining_by_key": [16]},
    )

    safe_source = output["sources"][0]
    assert safe_source["filter_reason_counts"] == {
        "unrecognized_official_ats_host": 1,
    }
    assert safe_source["filtered_url_examples"] == [{
        "reason_code": "unrecognized_official_ats_host",
        "host": "careers.example.test",
        "path_pattern": "/jobs/{item}",
    }]
    assert safe_source["provider_results_by_country"] == {"Morocco": 2}
    assert safe_source["filtered_results_by_language"] == {"fr": 1}
    assert safe_source["key_budget_events"] == {"a1b2c3d4": {"reserved": 1}}
    assert safe_source["queries_deferred_by_reason"] == {
        "run_attempt_cap": 4,
        "aggregate_daily_budget": 2,
    }


def test_workflow_discovery_task_skips_outbound_vault_and_full_pipeline_steps():
    repo_root = Path(__file__).resolve().parents[1]
    workflow = yaml.load(
        (repo_root / ".github" / "workflows" / "agent.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    task_input = workflow["on"]["workflow_dispatch"]["inputs"]["task"]
    assert "discovery_only" in task_input["options"]
    assert "discovery_restore_preflight" in task_input["options"]

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
    assert restore["id"] == "discovery_restore"
    assert "set -euo pipefail" in restore["run"]
    assert "set +e" not in restore["run"]
    assert "exit 0" not in restore["run"]
    for reason in (
        "vault_credentials_missing",
        "vault_clone_failed",
        "vault_sparse_checkout_failed",
        "vault_checkout_failed",
        "provider_key_file_invalid",
        "vault_provider_keys_missing",
    ):
        assert reason in restore["run"]
    assert 'echo "ready=false" >> "$GITHUB_OUTPUT"' in restore["run"]
    assert "steps.discovery_restore.outcome == 'success'" in run_step["if"]
    assert "steps.discovery_restore.outputs.ready == 'true'" in run_step["if"]
    assert "state/agent.db" in restore["run"]
    assert "candidate/profile.yaml" in restore["run"]
    assert "candidate/preferences.yaml" in restore["run"]
    assert "secrets/tavily_keys.txt" in restore["run"]
    assert "--branch main --single-branch" in restore["run"]
    assert "app.workflows.discovery_only_restore" in restore["run"]
    assert '"ready":true' not in restore["run"]
    assert "secrets/." not in restore["run"]
    assert "candidate/cv" not in restore["run"]
    assert "wca-history-vault" not in restore["run"]
    assert steps["Upload sanitized discovery-only report"]["if"].startswith(
        "${{ always()"
    )
    assert steps["Upload sanitized discovery-only report"]["with"][
        "if-no-files-found"
    ] == "error"
    assert "Remove private discovery restore material" in steps
    assert "always()" in steps["Remove private discovery restore material"]["if"]

    persist_condition = steps["Persist state back to vault"]["if"]
    assert "inputs.task != 'discovery_only'" in persist_condition
    assert "TAVILY_BUDGET_VAULT_REPO" in run_step["env"]
    assert 'get("status") == "COMPLETED"' in run_step["run"]
    assert "upload sanitized discovery-only report" in {
        name.casefold() for name in steps
    }


def test_restore_only_workflow_job_cannot_dispatch_discovery_or_outbound_actions():
    repo_root = Path(__file__).resolve().parents[1]
    workflow = yaml.load(
        (repo_root / ".github" / "workflows" / "agent.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    jobs = workflow["jobs"]
    restore = jobs["discovery_restore_preflight"]
    restore_steps = {step.get("name"): step for step in restore["steps"]}
    assert "inputs.task == 'discovery_restore_preflight'" in restore["if"]
    assert "TAVILY_BUDGET_VAULT_REPO" not in json.dumps(restore)
    restore_step = restore_steps[
        "Restore and validate private-vault budget (read-only)"
    ]
    assert restore_step["id"] == "restore_only_preflight"
    assert "set -euo pipefail" in restore_step["run"]
    assert "exit 0" not in restore_step["run"]
    assert "vault_clone_failed" in restore_step["run"]
    assert "vault_sparse_checkout_failed" in restore_step["run"]
    assert "vault_checkout_failed" in restore_step["run"]
    assert restore_steps["Upload sanitized restore-only report"]["if"].startswith(
        "${{ always()"
    )
    assert "Remove private restore material" in restore_steps
    assert "VAULT_TOKEN" in restore_steps[
        "Restore and validate private-vault budget (read-only)"
    ]["env"]
    assert "discovery_only_restore" in restore_steps[
        "Restore and validate private-vault budget (read-only)"
    ]["run"]
    assert "app.cli" not in json.dumps(restore)
    assert "tavily" not in json.dumps(restore_steps["Upload sanitized restore-only report"]).casefold()
    assert "discovery_restore_preflight" in jobs["pipeline"]["if"]
    assert "Upload sanitized restore-only report" in restore_steps
