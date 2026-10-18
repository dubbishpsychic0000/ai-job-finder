from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.workflows.discovery_only_restore import (
    DiscoveryRestoreError,
    build_restore_preflight_report,
    main,
    restore_discovery_inputs,
    validate_discovery_dispatch_report,
)

ORIGIN = "https://github.com/dubbishpsychic0000/ai-job-finder-vault.git"
KEY = "fixture-provider-key"
FINGERPRINT = hashlib.sha256(KEY.encode()).hexdigest()[:8]


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _vault(
    root: Path,
    *,
    origin: str = ORIGIN,
    branch: str = "main",
    keys: list[str] | None = None,
    ledgers: dict[str, str | None] | None = None,
    ledger: str | None = (
        '{"date":"2026-10-14","count":6,"exhausted":false,'
        '"usage_by_type":{"general":6,"btp":0,"unattributed":0}}'
    ),
) -> Path:
    configured_keys = keys or [KEY]
    repository = root / "vault"
    repository.mkdir(parents=True)
    _git(root, "init", "-b", "main", "vault")
    _git(repository, "config", "user.name", "Offline Fixture")
    _git(repository, "config", "user.email", "offline@example.test")
    _git(repository, "remote", "add", "origin", origin)
    (repository / "state").mkdir()
    (repository / "candidate").mkdir()
    (repository / "secrets").mkdir()
    (repository / "state" / "agent.db").write_bytes(b"offline db fixture")
    (repository / "candidate" / "profile.yaml").write_text(
        "name: Fixture\n", encoding="utf-8",
    )
    (repository / "candidate" / "preferences.yaml").write_text(
        "target_countries: [Morocco]\n", encoding="utf-8",
    )
    (repository / "secrets" / "tavily_keys.txt").write_text(
        ",".join(configured_keys), encoding="utf-8",
    )
    if ledgers is None:
        ledgers = {KEY: ledger}
    for key, payload in ledgers.items():
        if payload is not None:
            fingerprint = hashlib.sha256(key.encode()).hexdigest()[:8]
            (repository / "state" / f"tavily_budget_{fingerprint}.json").write_text(
                payload, encoding="utf-8",
            )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "offline vault fixture")
    _git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    if branch != "main":
        _git(repository, "checkout", "-b", branch)
    return repository


@pytest.mark.parametrize(
    ("origin", "branch", "expected_origin", "expected_branch", "reason"),
    [
        (
            ORIGIN, "main", "", "main",
            "vault_repository_configuration_missing",
        ),
        (
            "https://github.com/another/private-vault.git", "main", ORIGIN, "main",
            "vault_origin_mismatch",
        ),
        (
            ORIGIN, "recovery", ORIGIN, "main",
            "vault_ref_mismatch",
        ),
    ],
)
def test_wrong_or_missing_vault_configuration_blocks_restore(
    tmp_path, origin, branch, expected_origin, expected_branch, reason,
):
    repository = _vault(tmp_path, origin=origin, branch=branch)
    workspace = tmp_path / "workspace"

    with pytest.raises(DiscoveryRestoreError, match=reason):
        restore_discovery_inputs(
            repository,
            workspace,
            expected_origin=expected_origin,
            expected_branch=expected_branch,
            today=date(2026, 10, 14),
        )

    status = json.loads(
        (workspace / "data" / "discovery_only_inputs_status.json").read_text(),
    )
    assert status["ready"] is False
    assert status["reason_code"] == reason


@pytest.mark.parametrize(
    ("ledger", "expected_outcome"),
    [
        (None, "ledger_missing"),
        ("{invalid json", "ledger_json_invalid"),
        ('{"date":"2026-10-14","count":"six","exhausted":false}', "ledger_schema_invalid"),
        ('{"date":"not-a-date","count":6,"exhausted":false}', "ledger_date_invalid"),
        ('{"date":"2026-10-15","count":6,"exhausted":false}', "ledger_future_dated"),
        ('{"date":"2026-10-14","count":21,"exhausted":false}', "ledger_count_out_of_range"),
        (
            '{"date":"2026-10-14","count":6,"exhausted":false,'
            '"usage_by_type":{"general":5,"btp":0,"unattributed":0}}',
            "ledger_provenance_invalid",
        ),
    ],
)
def test_missing_or_corrupt_durable_budget_ledger_blocks_restore(
    tmp_path, ledger, expected_outcome,
):
    repository = _vault(tmp_path, ledger=ledger)
    workspace = tmp_path / "workspace"

    with pytest.raises(
        DiscoveryRestoreError,
        match="provider_budget_ledger_missing_or_invalid",
    ):
        restore_discovery_inputs(
            repository,
            workspace,
            expected_origin=ORIGIN,
            today=date(2026, 10, 14),
        )

    status = json.loads(
        (workspace / "data" / "discovery_only_inputs_status.json").read_text(),
    )
    assert status["ready"] is False
    assert status["reason_code"] == "provider_budget_ledger_missing_or_invalid"
    assert status["ledger_diagnostics"] == [{
        "fingerprint": FINGERPRINT,
        "outcome": expected_outcome,
    }]
    assert not (workspace / "data" / f"tavily_budget_{FINGERPRINT}.json").exists()


def test_valid_private_vault_fixture_restores_without_changing_ledger_balance(
    tmp_path,
):
    repository = _vault(tmp_path)
    workspace = tmp_path / "workspace"

    status = restore_discovery_inputs(
        repository,
        workspace,
        expected_origin=ORIGIN,
        today=date(2026, 10, 14),
    )

    assert status["ready"] is True
    assert status["vault_repository_validated"] is True
    assert status["vault_ref_validated"] is True
    assert status["provider_key_count"] == 1
    assert status["provider_budget_ledger_count"] == 1
    restored = json.loads(
        (workspace / "data" / f"tavily_budget_{FINGERPRINT}.json").read_text(),
    )
    assert restored == {
        "date": "2026-10-14",
        "count": 6,
        "exhausted": False,
        "usage_by_type": {"general": 6, "btp": 0, "unattributed": 0},
    }
    assert (workspace / "data" / "agent.db").read_bytes() == b"offline db fixture"

    report = build_restore_preflight_report(
        repository,
        workspace,
        restoration=status,
        reservation_path=repository,
        today=date(2026, 10, 14),
    )
    assert report["status"] == "COMPLETED"
    assert report["execution_status"] == "COMPLETED"
    assert report["readiness"] == "READY"
    assert report["discovery_dispatch_permission"] is True
    assert validate_discovery_dispatch_report(report) == ""
    assert report["reason_code"] == "general_capacity_available"
    assert report["provider_budget"]["requests_dispatched"] == 0
    assert report["provider_budget"]["reservations_created"] == 0
    assert report["provider_budget"]["per_key"] == [{
        "fingerprint": FINGERPRINT,
        "ledger_present": True,
        "ledger_valid": True,
        "ledger_utc_day_matches": True,
        "count": 6,
        "exhausted": False,
        "total_remaining": 14,
        "general_remaining": 11,
        "btp_reserve_remaining": 3,
        "btp_reserve_intact": True,
        "btp_reserve_status": "verified_intact",
        "usage_provenance": "verified",
        "usage_by_type": {"general": 6, "btp": 0, "unattributed": 0},
        "general_limit_crossed": False,
        "above_general_limit_source": "not_applicable",
        "eligible_for_general_discovery": True,
        "general_capacity_after_rollover": 0,
        "outcome": "valid_current_day",
        "reason_code": "",
        "source_ledger_present": True,
        "restored_ledger_present": True,
        "ledger_date_relation": "current_utc_day",
    }]
    assert report["restoration"]["expected_input_count"] == 4
    assert report["restoration"]["restored_input_count"] == 4
    assert report["restoration"]["expected_budget_ledger_count"] == 1
    assert report["restoration"]["restored_budget_ledger_count"] == 1
    assert KEY not in json.dumps(report)
    assert str(repository) not in json.dumps(report)


@pytest.mark.parametrize(
    ("ledger", "reason_code"),
    [
        ('{"date":"2026-10-14","count":17,"exhausted":true}', "no_general_budget_available"),
        ('{"date":"2026-10-14","count":18,"exhausted":false}', "no_general_budget_available"),
    ],
)
def test_restore_preflight_fails_closed_for_stale_or_depleted_budget(
    tmp_path, ledger, reason_code,
):
    repository = _vault(tmp_path, ledger=ledger)
    workspace = tmp_path / "workspace"
    status = restore_discovery_inputs(
        repository,
        workspace,
        expected_origin=ORIGIN,
        today=date(2026, 10, 14),
    )

    report = build_restore_preflight_report(
        repository,
        workspace,
        restoration=status,
        today=date(2026, 10, 14),
    )

    assert report["status"] == "COMPLETED"
    assert report["execution_status"] == "COMPLETED"
    assert report["readiness"] == "BLOCKED"
    assert report["discovery_dispatch_permission"] is False
    assert report["reason_code"] == reason_code
    assert report["provider_budget"]["requests_dispatched"] == 0
    assert report["provider_budget"]["reservations_created"] == 0
    assert report["provider_budget"]["btp_reserve_validation"] == "unknown"
    assert report["discovery_gate"]["authorized"] is False
    assert KEY not in json.dumps(report)


def test_mixed_key_states_authorize_only_general_eligible_keys(tmp_path):
    keys = ["fixture-20", "fixture-17", "fixture-1"]
    counts = [20, 17, 1]
    ledgers = {
        key: json.dumps({
            "date": "2026-10-14",
            "count": count,
            "exhausted": False,
            "usage_by_type": {
                "general": min(count, 17),
                "btp": max(0, count - 17),
                "unattributed": 0,
            },
        })
        for key, count in zip(keys, counts, strict=True)
    }
    repository = _vault(
        tmp_path,
        keys=keys,
        ledgers=ledgers,
    )
    workspace = tmp_path / "workspace"
    status = restore_discovery_inputs(
        repository,
        workspace,
        expected_origin=ORIGIN,
        today=date(2026, 10, 14),
    )

    report = build_restore_preflight_report(
        repository,
        workspace,
        restoration=status,
        reservation_path=repository,
        today=date(2026, 10, 14),
    )

    assert report["status"] == "COMPLETED"
    assert report["execution_status"] == "COMPLETED"
    assert report["readiness"] == "READY"
    assert report["discovery_gate"]["authorized"] is True
    assert report["provider_budget"]["eligible_general_key_count"] == 1
    assert report["provider_budget"]["excluded_general_key_count"] == 2
    assert report["provider_budget"]["general_capacity_current_utc_day"] == 16
    by_fp = {
        item["fingerprint"]: item
        for item in report["provider_budget"]["per_key"]
    }
    assert by_fp[hashlib.sha256(keys[0].encode()).hexdigest()[:8]][
        "eligible_for_general_discovery"
    ] is False
    assert by_fp[hashlib.sha256(keys[0].encode()).hexdigest()[:8]][
        "reason_code"
    ] == "general_budget_exhausted"
    assert by_fp[hashlib.sha256(keys[1].encode()).hexdigest()[:8]][
        "btp_reserve_remaining"
    ] == 3
    assert by_fp[hashlib.sha256(keys[1].encode()).hexdigest()[:8]][
        "eligible_for_general_discovery"
    ] is False
    eligible = by_fp[hashlib.sha256(keys[2].encode()).hexdigest()[:8]]
    assert eligible["eligible_for_general_discovery"] is True
    assert eligible["general_remaining"] == 16
    assert eligible["btp_reserve_remaining"] == 3
    assert report["provider_budget"]["btp_reserve_validation"] == (
        "passed_with_authorized_btp_usage"
    )
    assert all(key not in json.dumps(report) for key in keys)


def test_count_18_with_verified_btp_reservation_is_not_a_general_breach(tmp_path):
    ledger = json.dumps({
        "date": "2026-10-14",
        "count": 18,
        "exhausted": False,
        "usage_by_type": {"general": 17, "btp": 1, "unattributed": 0},
    })
    repository = _vault(tmp_path, ledger=ledger)
    workspace = tmp_path / "workspace"
    status = restore_discovery_inputs(
        repository, workspace, expected_origin=ORIGIN,
        today=date(2026, 10, 14),
    )

    report = build_restore_preflight_report(
        repository, workspace, restoration=status, today=date(2026, 10, 14),
    )
    key = report["provider_budget"]["per_key"][0]

    assert report["provider_budget"]["btp_reserve_validation"] == (
        "passed_with_authorized_btp_usage"
    )
    assert key["btp_reserve_remaining"] == 2
    assert key["btp_reserve_intact"] is True
    assert key["general_limit_crossed"] is False
    assert key["above_general_limit_source"] == "verified_btp"
    assert key["eligible_for_general_discovery"] is False


def test_count_18_without_provenance_is_unknown_not_a_breach_or_pass(tmp_path):
    repository = _vault(
        tmp_path,
        ledger='{"date":"2026-10-14","count":18,"exhausted":false}',
    )
    workspace = tmp_path / "workspace"
    status = restore_discovery_inputs(
        repository, workspace, expected_origin=ORIGIN,
        today=date(2026, 10, 14),
    )

    report = build_restore_preflight_report(
        repository, workspace, restoration=status, today=date(2026, 10, 14),
    )
    key = report["provider_budget"]["per_key"][0]

    assert report["provider_budget"]["btp_reserve_validation"] == "unknown"
    assert report["provider_budget"]["btp_reserve_unresolved_ledger_count"] == 1
    assert key["btp_reserve_status"] == "unknown_provenance"
    assert key["btp_reserve_remaining"] is None
    assert key["general_remaining"] is None
    assert key["general_limit_crossed"] is None
    assert key["above_general_limit_source"] == "unknown"
    assert key["eligible_for_general_discovery"] is False


def test_count_18_with_general_usage_is_reported_as_a_breach(tmp_path):
    repository = _vault(
        tmp_path,
        ledger=json.dumps({
            "date": "2026-10-14",
            "count": 18,
            "exhausted": False,
            "usage_by_type": {"general": 18, "btp": 0, "unattributed": 0},
        }),
    )
    workspace = tmp_path / "workspace"
    status = restore_discovery_inputs(
        repository, workspace, expected_origin=ORIGIN,
        today=date(2026, 10, 14),
    )

    report = build_restore_preflight_report(
        repository, workspace, restoration=status, today=date(2026, 10, 14),
    )
    key = report["provider_budget"]["per_key"][0]

    assert report["provider_budget"]["btp_reserve_validation"] == "blocked"
    assert key["btp_reserve_status"] == "general_budget_breach"
    assert key["general_limit_crossed"] is True
    assert key["above_general_limit_source"] == "verified_general_discovery"
    assert key["reason_code"] == "general_budget_exceeded_protected_limit"


def test_btp_usage_above_three_request_reserve_blocks_global_validation(tmp_path):
    repository = _vault(
        tmp_path,
        ledger=json.dumps({
            "date": "2026-10-14",
            "count": 18,
            "exhausted": False,
            "usage_by_type": {"general": 14, "btp": 4, "unattributed": 0},
        }),
    )
    workspace = tmp_path / "workspace"
    status = restore_discovery_inputs(
        repository, workspace, expected_origin=ORIGIN,
        today=date(2026, 10, 14),
    )

    report = build_restore_preflight_report(
        repository, workspace, restoration=status, today=date(2026, 10, 14),
    )
    key = report["provider_budget"]["per_key"][0]

    assert report["provider_budget"]["btp_reserve_validation"] == "blocked"
    assert key["btp_reserve_status"] == "btp_reserve_breach"
    assert key["btp_reserve_remaining"] == 0
    assert key["eligible_for_general_discovery"] is False


def test_global_reserve_summary_includes_all_seven_keys(tmp_path):
    keys = [f"fixture-{number}" for number in range(7)]
    ledgers = {}
    for key in keys[:6]:
        ledgers[key] = json.dumps({
            "date": "2026-10-13",
            "count": 20,
            "exhausted": True,
        })
    ledgers[keys[6]] = (
        '{"date":"2026-10-14","count":18,"exhausted":false}'
    )
    repository = _vault(tmp_path, keys=keys, ledgers=ledgers)
    workspace = tmp_path / "workspace"
    status = restore_discovery_inputs(
        repository, workspace, expected_origin=ORIGIN,
        today=date(2026, 10, 14),
    )

    report = build_restore_preflight_report(
        repository, workspace, restoration=status, today=date(2026, 10, 14),
    )

    assert report["provider_budget"]["btp_reserve_ledger_count"] == 7
    assert len(report["provider_budget"]["per_key"]) == 7
    assert report["provider_budget"]["eligible_general_key_count"] == 6
    assert report["provider_budget"]["btp_reserve_validation"] == "unknown"
    assert report["provider_budget"]["btp_reserve_unresolved_ledger_count"] == 1


def test_yesterday_ledger_is_reported_rollover_eligible_without_mutation(tmp_path):
    yesterday = '{"date":"2026-10-13","count":20,"exhausted":true}'
    repository = _vault(tmp_path, ledger=yesterday)
    workspace = tmp_path / "workspace"
    status = restore_discovery_inputs(
        repository,
        workspace,
        expected_origin=ORIGIN,
        today=date(2026, 10, 14),
    )
    source_path = repository / "state" / f"tavily_budget_{FINGERPRINT}.json"
    restored_path = workspace / "data" / source_path.name
    before = source_path.read_bytes()

    report = build_restore_preflight_report(
        repository,
        workspace,
        restoration=status,
        reservation_path=repository,
        today=date(2026, 10, 14),
    )

    assert report["status"] == "COMPLETED"
    assert report["execution_status"] == "COMPLETED"
    assert report["readiness"] == "CONDITIONAL"
    assert report["discovery_dispatch_permission"] is False
    assert report["discovery_gate"]["authorized"] is False
    assert report["readiness_reason_code"] == (
        "utc_day_rollover_pending_durable_reservation"
    )
    assert report["provider_budget"]["utc_day_validation"] == (
        "passed_with_durable_rollover"
    )
    assert report["provider_budget"]["general_capacity_after_durable_rollover"] == 17
    assert report["provider_budget"]["btp_reserve_validation"] == "pending_rollover"
    assert report["provider_budget"]["aggregate_capacity_remaining"][
        "btp_reserve_remaining"
    ] is None
    assert report["provider_budget"]["per_key"][0][
        "eligible_for_general_discovery"
    ] is True
    assert report["provider_budget"]["per_key"][0]["general_remaining"] is None
    assert json.loads(source_path.read_text())["date"] == "2026-10-13"
    assert restored_path.read_bytes() == before
    assert source_path.read_bytes() == before


def test_current_capacity_is_ready_while_other_key_is_rollover_pending(tmp_path):
    yesterday = '{"date":"2026-10-13","count":20,"exhausted":true}'
    repository = _vault(
        tmp_path,
        keys=["fixture-current", "fixture-prior"],
        ledgers={
            "fixture-current": (
                '{"date":"2026-10-14","count":0,"exhausted":false,'
                '"usage_by_type":{"general":0,"btp":0,"unattributed":0}}'
            ),
            "fixture-prior": yesterday,
        },
    )
    workspace = tmp_path / "workspace"
    restoration = restore_discovery_inputs(
        repository, workspace, expected_origin=ORIGIN,
        today=date(2026, 10, 14),
    )

    report = build_restore_preflight_report(
        repository, workspace, restoration=restoration, today=date(2026, 10, 14),
    )

    assert report["provider_budget"]["btp_reserve_validation"] == "pending_rollover"
    assert report["provider_budget"]["general_capacity_current_utc_day"] == 17
    assert report["provider_budget"]["aggregate_capacity_remaining"][
        "btp_reserve_remaining"
    ] == 3
    assert report["readiness"] == "READY"
    assert report["discovery_dispatch_permission"] is True
    assert validate_discovery_dispatch_report(report) == ""


def test_phase_28q_unknown_current_provenance_cannot_authorize_dispatch(tmp_path):
    keys = [f"fixture-{number}" for number in range(7)]
    ledgers = {
        keys[0]: (
            '{"date":"2026-10-14","count":18,"exhausted":false}'
        ),
        keys[1]: (
            '{"date":"2026-10-14","count":17,"exhausted":false}'
        ),
    }
    for key in keys[2:]:
        ledgers[key] = (
            '{"date":"2026-10-13","count":17,"exhausted":false}'
        )
    repository = _vault(tmp_path, keys=keys, ledgers=ledgers)
    workspace = tmp_path / "workspace"
    restoration = restore_discovery_inputs(
        repository, workspace, expected_origin=ORIGIN,
        today=date(2026, 10, 14),
    )

    report = build_restore_preflight_report(
        repository, workspace, restoration=restoration, today=date(2026, 10, 14),
    )

    assert report["execution_status"] == "COMPLETED"
    assert report["status"] == "COMPLETED"
    assert report["readiness"] == "BLOCKED"
    assert report["discovery_dispatch_permission"] is False
    assert report["discovery_gate"]["authorized"] is False
    assert report["provider_budget"]["general_capacity_current_utc_day"] == 0
    assert report["provider_budget"]["general_capacity_after_durable_rollover"] == 85
    assert report["provider_budget"]["btp_reserve_validation"] == "unknown"
    assert report["readiness_reason_codes"] == [
        "no_general_budget_available",
        "provider_budget_reserve_unverified",
        "provider_budget_provenance_unavailable",
        "utc_day_rollover_pending_durable_reservation",
    ]
    assert validate_discovery_dispatch_report(report) == (
        "discovery_readiness_not_ready"
    )
    assert validate_discovery_dispatch_report(
        report,
        allow_conditional_rollover=True,
    ) == "discovery_readiness_not_ready"


def _conditional_rollover_report(tmp_path):
    today = datetime.now(timezone.utc).date()
    yesterday = date.fromordinal(today.toordinal() - 1)
    repository = _vault(
        tmp_path,
        ledger=json.dumps({
            "date": yesterday.isoformat(),
            "count": 18,
            "exhausted": True,
        }),
    )
    workspace = tmp_path / "workspace"
    restoration = restore_discovery_inputs(
        repository,
        workspace,
        expected_origin=ORIGIN,
        today=today,
    )
    return repository, workspace, build_restore_preflight_report(
        repository,
        workspace,
        restoration=restoration,
        reservation_path=repository,
        today=today,
    )


def test_conditional_rollover_report_requires_separate_explicit_authorization(
    tmp_path,
):
    _repository, _workspace, report = _conditional_rollover_report(tmp_path)

    assert report["execution_status"] == "COMPLETED"
    assert report["readiness"] == "CONDITIONAL"
    assert report["discovery_dispatch_permission"] is False
    assert report["discovery_gate"]["authorized"] is False
    assert report["provider_budget"]["general_capacity_current_utc_day"] == 0
    assert report["provider_budget"]["general_capacity_after_durable_rollover"] == 17
    assert report["provider_budget"]["requests_dispatched"] == 0
    assert report["provider_budget"]["reservations_created"] == 0
    assert validate_discovery_dispatch_report(report) == (
        "conditional_rollover_authorization_required"
    )
    assert validate_discovery_dispatch_report(
        report,
        allow_conditional_rollover=True,
    ) == ""


@pytest.mark.parametrize(
    ("mutate", "expected_reason"),
    [
        (
            lambda report: report.update(discovery_dispatch_permission=True),
            "discovery_readiness_not_ready",
        ),
        (
            lambda report: report["provider_budget"].update(
                btp_reserve_validation="blocked",
            ),
            "provider_budget_reserve_unverified",
        ),
        (
            lambda report: report["provider_budget"]["per_key"][0].update(
                outcome="ledger_json_invalid",
            ),
            "discovery_readiness_report_invalid",
        ),
        (
            lambda report: report["provider_budget"].update(
                general_capacity_after_durable_rollover=102,
            ),
            "discovery_readiness_report_invalid",
        ),
        (
            lambda report: report.update(
                readiness_utc_day="2020-01-01",
            ),
            "discovery_readiness_not_ready",
        ),
        (
            lambda report: report["restoration"].update(
                vault_origin_verified=False,
            ),
            "discovery_readiness_report_invalid",
        ),
        (
            lambda report: report["restoration"].update(
                restored_input_count=3,
            ),
            "discovery_readiness_report_invalid",
        ),
        (
            lambda report: report["provider_budget"].update(
                btp_reserve_unresolved_ledger_count=1,
            ),
            "provider_budget_reserve_unverified",
        ),
        (
            lambda report: report["provider_budget"]["per_key"][0].update(
                btp_reserve_status="btp_reserve_breach",
            ),
            "provider_budget_reserve_unverified",
        ),
        (
            lambda report: report.update(
                readiness_reason_code="general_capacity_available",
            ),
            "discovery_readiness_not_ready",
        ),
    ],
)
def test_conditional_rollover_validator_rejects_stale_or_contradictory_report(
    tmp_path, mutate, expected_reason,
):
    _repository, _workspace, report = _conditional_rollover_report(tmp_path)
    mutate(report)

    assert validate_discovery_dispatch_report(
        report,
        allow_conditional_rollover=True,
    ) == expected_reason


@pytest.mark.parametrize(
    ("mutate", "expected_reason"),
    [
        (lambda report: report.pop("readiness_evaluated_at"), "discovery_readiness_not_ready"),
        (
            lambda report: report.update(
                readiness_evaluated_at="2020-01-01T00:00:00+00:00",
            ),
            "discovery_readiness_not_ready",
        ),
        (
            lambda report: report.update(discovery_dispatch_permission=False),
            "discovery_readiness_not_ready",
        ),
        (
            lambda report: report["provider_budget"].update(
                aggregate_capacity_remaining={"general_remaining": 0},
            ),
            "discovery_readiness_report_invalid",
        ),
    ],
)
def test_dispatch_report_validation_rejects_missing_stale_or_inconsistent_data(
    tmp_path, mutate, expected_reason,
):
    repository = _vault(tmp_path)
    workspace = tmp_path / "workspace"
    restoration = restore_discovery_inputs(
        repository, workspace, expected_origin=ORIGIN,
        today=date(2026, 10, 14),
    )
    report = build_restore_preflight_report(
        repository, workspace, restoration=restoration, today=date(2026, 10, 14),
    )
    mutate(report)
    assert validate_discovery_dispatch_report(report) == expected_reason


def test_dispatch_report_validator_rejects_missing_readiness_report():
    assert validate_discovery_dispatch_report(None) == (
        "discovery_readiness_report_invalid"
    )


def test_restore_preflight_reports_missing_expected_ledger_by_fingerprint(tmp_path):
    repository = _vault(tmp_path, ledger=None)
    workspace = tmp_path / "workspace"
    with pytest.raises(DiscoveryRestoreError):
        restore_discovery_inputs(
            repository,
            workspace,
            expected_origin=ORIGIN,
            today=date(2026, 10, 14),
        )
    restoration = json.loads(
        (workspace / "data" / "discovery_only_inputs_status.json")
        .read_text(encoding="utf-8"),
    )

    report = build_restore_preflight_report(
        repository,
        workspace,
        restoration=restoration,
        today=date(2026, 10, 14),
    )

    assert report["status"] == "BLOCKED"
    assert report["reason_code"] == "provider_budget_ledger_missing_or_invalid"
    assert report["restoration"]["expected_budget_ledger_count"] == 1
    assert report["restoration"]["restored_budget_ledger_count"] == 0
    assert report["provider_budget"]["per_key"][0]["fingerprint"] == FINGERPRINT
    assert report["provider_budget"]["per_key"][0]["source_ledger_present"] is False
    assert report["provider_budget"]["per_key"][0]["reason_code"] == (
        "provider_budget_ledger_missing"
    )
    assert report["provider_budget"]["per_key"][0]["outcome"] == "ledger_missing"
    assert report["restoration"]["ledger_diagnostics"] == [{
        "fingerprint": FINGERPRINT,
        "outcome": "ledger_missing",
    }]


@pytest.mark.parametrize(
    "reason",
    [
        "vault_credentials_missing",
        "vault_clone_failed",
        "vault_sparse_checkout_failed",
        "vault_checkout_failed",
        "restore_execution_report_invalid",
    ],
)
def test_restore_operation_failures_write_blocked_reports(tmp_path, reason):
    workspace = tmp_path / "workspace"
    repository = tmp_path / "unavailable-vault"
    repository.mkdir()
    restore_report = workspace / "restore-report.json"
    discovery_report = workspace / "discovery-report.json"

    status = main([
        "--vault", str(repository),
        "--workspace", str(workspace),
        "--origin", ORIGIN,
        "--branch", "main",
        "--reservation-path", str(repository),
        "--report", str(restore_report),
        "--discovery-report", str(discovery_report),
        "--restore-failure-reason", reason,
    ])

    restore_payload = json.loads(restore_report.read_text(encoding="utf-8"))
    discovery_payload = json.loads(discovery_report.read_text(encoding="utf-8"))
    manifest = json.loads(
        (workspace / "data" / "discovery_only_inputs_status.json")
        .read_text(encoding="utf-8"),
    )
    assert status == 1
    assert restore_payload["status"] == "BLOCKED"
    assert restore_payload["reason_code"] == reason
    assert discovery_payload["status"] == "BLOCKED"
    assert discovery_payload["phase"] == "input_restore"
    assert discovery_payload["reason_code"] == reason
    assert discovery_payload["provider"]["api_attempts"] == 0
    assert manifest["ready"] is False


def test_restore_preflight_rejects_mismatched_reservation_checkout(tmp_path):
    repository = _vault(tmp_path)
    workspace = tmp_path / "workspace"
    status = restore_discovery_inputs(
        repository,
        workspace,
        expected_origin=ORIGIN,
        today=date(2026, 10, 14),
    )

    report = build_restore_preflight_report(
        repository,
        workspace,
        restoration=status,
        reservation_path=tmp_path / "other-vault",
        today=date(2026, 10, 14),
    )

    assert report["status"] == "BLOCKED"
    assert "vault_repository_path_mismatch" in report["reason_codes"]
    assert report["restoration"]["vault_path_verified"] is False


def test_exhausted_ledger_is_diagnosed_without_rewriting_it(tmp_path):
    exhausted = '{"date":"2026-10-14","count":20,"exhausted":true}'
    repository = _vault(tmp_path, ledger=exhausted)
    workspace = tmp_path / "workspace"
    source = repository / "state" / f"tavily_budget_{FINGERPRINT}.json"
    before = source.read_bytes()

    status = restore_discovery_inputs(
        repository,
        workspace,
        expected_origin=ORIGIN,
        today=date(2026, 10, 14),
    )
    report = build_restore_preflight_report(
        repository,
        workspace,
        restoration=status,
        reservation_path=repository,
        today=date(2026, 10, 14),
    )

    assert status["ledger_diagnostics"] == [{
        "fingerprint": FINGERPRINT,
        "outcome": "ledger_exhausted",
    }]
    assert report["status"] == "COMPLETED"
    assert report["execution_status"] == "COMPLETED"
    assert report["readiness"] == "BLOCKED"
    assert report["discovery_dispatch_permission"] is False
    assert report["provider_budget"]["per_key"][0]["outcome"] == "ledger_exhausted"
    assert source.read_bytes() == before
