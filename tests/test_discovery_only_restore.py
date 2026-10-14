from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from app.workflows.discovery_only_restore import (
    DiscoveryRestoreError,
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
    ledger: str | None = '{"date":"2026-10-14","count":6,"exhausted":false}',
) -> Path:
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
        KEY, encoding="utf-8",
    )
    if ledger is not None:
        (repository / "state" / f"tavily_budget_{FINGERPRINT}.json").write_text(
            ledger, encoding="utf-8",
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
