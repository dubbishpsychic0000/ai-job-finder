"""Fail-closed restoration of bounded discovery inputs from the private vault."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from app.connectors.tavily_resilience import TAVILY_DAILY_REQUEST_LIMIT


class DiscoveryRestoreError(RuntimeError):
    """Sanitized error code for an incomplete or untrusted vault restore."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _git(repository: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repository,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiscoveryRestoreError("vault_repository_unavailable") from exc
    if result.returncode:
        raise DiscoveryRestoreError("vault_repository_unavailable")
    return result.stdout.decode("utf-8", errors="replace").strip()


def _remote_identity(remote: str) -> tuple[str, str]:
    try:
        parts = urlsplit(remote)
        if parts.scheme:
            if not parts.hostname:
                raise ValueError
            return parts.hostname.casefold(), parts.path.rstrip("/").removesuffix(".git")
        return "", str(Path(remote).resolve())
    except (OSError, ValueError) as exc:
        raise DiscoveryRestoreError("vault_origin_mismatch") from exc


def _validate_checkout(
    repository: Path,
    *,
    expected_origin: str,
    expected_branch: str,
) -> str:
    if not expected_origin or not expected_branch:
        raise DiscoveryRestoreError("vault_repository_configuration_missing")
    if not repository.is_dir():
        raise DiscoveryRestoreError("vault_repository_unavailable")
    origin = _git(repository, "remote", "get-url", "origin")
    if _remote_identity(origin) != _remote_identity(expected_origin):
        raise DiscoveryRestoreError("vault_origin_mismatch")
    branch = _git(repository, "symbolic-ref", "--quiet", "--short", "HEAD")
    if branch != expected_branch:
        raise DiscoveryRestoreError("vault_ref_mismatch")
    head = _git(repository, "rev-parse", "HEAD")
    remote_head = _git(repository, "rev-parse", f"refs/remotes/origin/{expected_branch}")
    if head != remote_head:
        raise DiscoveryRestoreError("vault_ref_mismatch")
    return head


def _valid_budget_ledger(path: Path, *, today: date) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("date"), str)
        or not isinstance(payload.get("count"), int)
        or isinstance(payload.get("count"), bool)
        or not 0 <= payload["count"] <= TAVILY_DAILY_REQUEST_LIMIT
        or not isinstance(payload.get("exhausted"), bool)
    ):
        return False
    try:
        ledger_date = date.fromisoformat(payload["date"])
    except ValueError:
        return False
    return ledger_date.isoformat() == payload["date"] and ledger_date <= today


def _write_status(path: Path, status: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(status, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def restore_discovery_inputs(
    repository: Path,
    workspace: Path,
    *,
    expected_origin: str,
    expected_branch: str = "main",
    today: date | None = None,
) -> dict:
    """Validate the selected vault snapshot before copying any discovery input."""
    data_dir = workspace / "data"
    status_path = data_dir / "discovery_only_inputs_status.json"
    now_date = today or datetime.now(timezone.utc).date()
    try:
        commit = _validate_checkout(
            repository,
            expected_origin=expected_origin,
            expected_branch=expected_branch,
        )
        required_files = {
            "database": repository / "state" / "agent.db",
            "profile": repository / "candidate" / "profile.yaml",
            "preferences": repository / "candidate" / "preferences.yaml",
            "keys": repository / "secrets" / "tavily_keys.txt",
        }
        if any(not path.is_file() or path.stat().st_size == 0 for path in required_files.values()):
            raise DiscoveryRestoreError("vault_required_input_missing")

        key_text = required_files["keys"].read_text(encoding="utf-8")
        keys = [
            key.strip()
            for key in key_text.replace("\r", "").replace("\n", ",").split(",")
            if key.strip()
        ]
        if not keys:
            raise DiscoveryRestoreError("vault_provider_keys_missing")
        fingerprints: dict[str, str] = {}
        ledgers: list[Path] = []
        for key in keys:
            fingerprint = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
            if fingerprint in fingerprints and fingerprints[fingerprint] != key:
                raise DiscoveryRestoreError("provider_budget_ledger_collision")
            fingerprints[fingerprint] = key
            ledger = repository / "state" / f"tavily_budget_{fingerprint}.json"
            if not _valid_budget_ledger(ledger, today=now_date):
                raise DiscoveryRestoreError("provider_budget_ledger_missing_or_invalid")
            ledgers.append(ledger)

        copies = [
            (required_files["database"], data_dir / "agent.db"),
            (required_files["profile"], workspace / "candidate" / "profile.yaml"),
            (required_files["preferences"], workspace / "candidate" / "preferences.yaml"),
            (required_files["keys"], workspace / "secrets" / "tavily_keys.txt"),
            *(
                (ledger, data_dir / ledger.name)
                for ledger in dict.fromkeys(ledgers)
            ),
        ]
        cache = repository / "state" / "tavily_cache.json"
        if cache.is_file():
            copies.append((cache, data_dir / "tavily_cache.json"))
        for source, destination in copies:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)

        status = {
            "schema_version": 1,
            "ready": True,
            "reason_code": "",
            "vault_checkout_path": str(repository.resolve()),
            "vault_repository_validated": True,
            "vault_ref_validated": True,
            "vault_commit": commit,
            "database_snapshot_restored": True,
            "candidate_search_profile_restored": True,
            "provider_keys_restored": True,
            "provider_budget_ledgers_restored": True,
            "provider_key_count": len(fingerprints),
            "provider_budget_ledger_count": len(set(ledgers)),
            "provider_cache_restored": cache.is_file(),
        }
        _write_status(status_path, status)
        return status
    except DiscoveryRestoreError as exc:
        _write_status(status_path, {
            "schema_version": 1,
            "ready": False,
            "reason_code": exc.reason_code,
            "vault_checkout_path": "",
            "vault_repository_validated": False,
            "vault_ref_validated": False,
            "database_snapshot_restored": False,
            "candidate_search_profile_restored": False,
            "provider_keys_restored": False,
            "provider_budget_ledgers_restored": False,
            "provider_key_count": 0,
            "provider_budget_ledger_count": 0,
            "provider_cache_restored": False,
        })
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--branch", default="main")
    args = parser.parse_args(argv)
    try:
        restore_discovery_inputs(
            args.vault,
            args.workspace,
            expected_origin=args.origin,
            expected_branch=args.branch,
        )
    except DiscoveryRestoreError as exc:
        print(exc.reason_code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
