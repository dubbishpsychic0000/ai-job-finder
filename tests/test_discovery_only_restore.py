from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from app.workflows.discovery_only_restore import (
    DiscoveryRestoreError,
    build_restore_preflight_report,
    restore_discovery_inputs,
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
    ledger: str | None = '{"date":"2026-10-14","count":6,"exhausted":false}',
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
    "ledger",
    [
        None,
        '{"date":"2026-10-14","count":"six","exhausted":false}',
        '{"date":"2026-10-15","count":6,"exhausted":false}',
        '{"date":"2026-10-14","count":21,"exhausted":false}',
    ],
)
def test_missing_or_corrupt_durable_budget_ledger_blocks_restore(
    tmp_path, ledger,
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
        "eligible_for_general_discovery": True,
        "general_capacity_after_rollover": 0,
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

    assert report["status"] == "BLOCKED"
    assert report["reason_code"] == reason_code
    assert report["provider_budget"]["requests_dispatched"] == 0
    assert report["provider_budget"]["reservations_created"] == 0
    assert report["provider_budget"]["btp_reserve_validation"] == "blocked"
    assert KEY not in json.dumps(report)


def test_mixed_key_states_authorize_only_general_eligible_keys(tmp_path):
    keys = ["fixture-20", "fixture-17", "fixture-1"]
    counts = [20, 17, 1]
    ledgers = {
        key: json.dumps({
            "date": "2026-10-14",
            "count": count,
            "exhausted": False,
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
    assert report["provider_budget"]["btp_reserve_validation"] == "passed"
    assert all(key not in json.dumps(report) for key in keys)


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
    assert report["discovery_gate"]["authorized"] is True
    assert report["provider_budget"]["utc_day_validation"] == (
        "passed_with_durable_rollover"
    )
    assert report["provider_budget"]["general_capacity_after_durable_rollover"] == 17
    assert report["provider_budget"]["per_key"][0][
        "eligible_for_general_discovery"
    ] is True
    assert report["provider_budget"]["per_key"][0]["general_remaining"] is None
    assert json.loads(source_path.read_text())["date"] == "2026-10-13"
    assert restored_path.read_bytes() == before
    assert source_path.read_bytes() == before


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
