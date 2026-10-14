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

from app.connectors.tavily_resilience import (
    BTP_TAVILY_DAILY_RESERVE,
    GENERAL_TAVILY_DAILY_LIMIT,
    TAVILY_DAILY_REQUEST_LIMIT,
)


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
    expected_key_count: int | None = None
    restored_input_count = 0
    restored_ledger_count = 0
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
        expected_key_count = len(set(keys))
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
            if source in required_files.values():
                restored_input_count += 1
            elif source in ledgers:
                restored_ledger_count += 1

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
            "provider_budget_ledger_count": restored_ledger_count,
            "expected_input_count": len(required_files),
            "restored_input_count": restored_input_count,
            "expected_budget_ledger_count": len(fingerprints),
            "restored_budget_ledger_count": restored_ledger_count,
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
            "expected_input_count": len(required_files) if "required_files" in locals() else 4,
            "restored_input_count": restored_input_count,
            "expected_budget_ledger_count": expected_key_count,
            "restored_budget_ledger_count": restored_ledger_count,
            "provider_cache_restored": False,
        })
        raise


def build_restore_preflight_report(
    repository: Path,
    workspace: Path,
    *,
    restoration: dict,
    reservation_path: Path | None = None,
    today: date | None = None,
) -> dict:
    """Summarize restore and budget health without disclosing vault data."""
    now_date = today or datetime.now(timezone.utc).date()
    keys_path = workspace / "secrets" / "tavily_keys.txt"
    if not keys_path.is_file():
        keys_path = repository / "secrets" / "tavily_keys.txt"
    data_dir = workspace / "data"
    keys: list[str] = []
    if keys_path.is_file():
        try:
            keys = [
                key.strip()
                for key in keys_path.read_text(encoding="utf-8")
                .replace("\r", "").replace("\n", ",").split(",")
                if key.strip()
            ]
        except (OSError, UnicodeError):
            keys = []
    fingerprints = list(dict.fromkeys(
        hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
        for key in keys
    ))
    per_key: list[dict] = []
    reasons: list[str] = []
    if restoration.get("ready") is not True:
        reasons.append(str(restoration.get("reason_code") or "vault_restore_failed"))
    if not fingerprints:
        reasons.append("vault_provider_keys_missing")

    for fingerprint in fingerprints:
        ledger_path = data_dir / f"tavily_budget_{fingerprint}.json"
        item = {
            "fingerprint": fingerprint,
            "ledger_present": False,
            "ledger_valid": False,
            "ledger_utc_day_matches": False,
            "count": None,
            "exhausted": None,
            "total_remaining": None,
            "general_remaining": None,
            "btp_reserve_remaining": None,
            "btp_reserve_intact": False,
            "reason_code": "",
        }
        source_ledger_path = repository / "state" / ledger_path.name
        observed_ledger_path = (
            ledger_path if ledger_path.is_file() else source_ledger_path
        )
        item["source_ledger_present"] = source_ledger_path.is_file()
        item["restored_ledger_present"] = ledger_path.is_file()
        item["ledger_present"] = item["source_ledger_present"]
        if not observed_ledger_path.is_file():
            item["reason_code"] = "provider_budget_ledger_missing"
            reasons.append(item["reason_code"])
            per_key.append(item)
            continue
        try:
            payload = json.loads(observed_ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("date"), str)
            or not isinstance(payload.get("count"), int)
            or isinstance(payload.get("count"), bool)
            or not 0 <= payload["count"] <= TAVILY_DAILY_REQUEST_LIMIT
            or not isinstance(payload.get("exhausted"), bool)
        ):
            item["reason_code"] = "provider_budget_ledger_missing_or_invalid"
            reasons.append(item["reason_code"])
            per_key.append(item)
            continue
        try:
            ledger_date = date.fromisoformat(payload["date"])
        except ValueError:
            ledger_date = None
        if ledger_date is None or ledger_date.isoformat() != payload["date"]:
            item["reason_code"] = "provider_budget_ledger_missing_or_invalid"
            reasons.append(item["reason_code"])
            per_key.append(item)
            continue

        item["ledger_valid"] = True
        item["ledger_utc_day_matches"] = ledger_date == now_date
        item["count"] = payload["count"]
        item["exhausted"] = payload["exhausted"]
        if ledger_date != now_date:
            item["reason_code"] = "provider_budget_ledger_stale"
            reasons.append(item["reason_code"])
        else:
            item["total_remaining"] = max(
                0, TAVILY_DAILY_REQUEST_LIMIT - payload["count"],
            )
            item["general_remaining"] = max(
                0, GENERAL_TAVILY_DAILY_LIMIT - payload["count"],
            )
            item["btp_reserve_remaining"] = max(
                0,
                TAVILY_DAILY_REQUEST_LIMIT
                - max(GENERAL_TAVILY_DAILY_LIMIT, payload["count"]),
            )
            item["btp_reserve_intact"] = (
                payload["count"] <= GENERAL_TAVILY_DAILY_LIMIT
                and item["btp_reserve_remaining"] >= BTP_TAVILY_DAILY_RESERVE
            )
            if payload["exhausted"] or payload["count"] >= TAVILY_DAILY_REQUEST_LIMIT:
                item["reason_code"] = "provider_budget_exhausted"
                reasons.append(item["reason_code"])
            elif not item["btp_reserve_intact"]:
                item["reason_code"] = "btp_reserve_not_intact"
                reasons.append(item["reason_code"])
        per_key.append(item)

    input_expected = restoration.get("expected_input_count", 4)
    input_restored = restoration.get("restored_input_count", 0)
    ledger_expected = restoration.get("expected_budget_ledger_count")
    ledger_restored = restoration.get("restored_budget_ledger_count", 0)
    if restoration.get("ready") is True and (
        input_expected != input_restored
        or ledger_expected != ledger_restored
        or ledger_expected != len(fingerprints)
    ):
        reasons.append("restored_input_count_mismatch")
    for flag in (
        "vault_repository_validated",
        "vault_ref_validated",
    ):
        if restoration.get(flag) is not True:
            reasons.append(
                "vault_origin_mismatch" if flag == "vault_repository_validated"
                else "vault_ref_mismatch",
            )
    expected_reservation_path = (reservation_path or repository).resolve()
    checkout_path_verified = (
        repository.resolve() == expected_reservation_path
        and restoration.get("vault_checkout_path")
        == str(expected_reservation_path)
    )
    if not checkout_path_verified:
        reasons.append("vault_repository_path_mismatch")

    aggregate = {
        key: (
            sum(item[key] for item in per_key if isinstance(item[key], int))
            if per_key and all(isinstance(item[key], int) for item in per_key)
            else None
        )
        for key in (
            "total_remaining",
            "general_remaining",
            "btp_reserve_remaining",
        )
    }
    unique_reasons = list(dict.fromkeys(reasons))
    success = not unique_reasons and len(per_key) == len(fingerprints)
    return {
        "schema_version": 1,
        "mode": "restore_only",
        "status": "COMPLETED" if success else "BLOCKED",
        "reason_code": "restoration_validated" if success else unique_reasons[0],
        "utc_day": now_date.isoformat(),
        "restoration": {
            "expected_input_count": input_expected,
            "restored_input_count": input_restored,
            "expected_budget_ledger_count": ledger_expected,
            "restored_budget_ledger_count": ledger_restored,
            "vault_origin_verified": restoration.get(
                "vault_repository_validated",
            ) is True,
            "vault_branch_verified": restoration.get("vault_ref_validated") is True,
            "vault_path_verified": checkout_path_verified,
            "vault_branch": "main" if restoration.get("vault_ref_validated") else "",
        },
        "provider_budget": {
            "requests_dispatched": 0,
            "reservations_created": 0,
            "limit_per_key": TAVILY_DAILY_REQUEST_LIMIT,
            "general_limit_per_key": GENERAL_TAVILY_DAILY_LIMIT,
            "btp_reserve_per_key": BTP_TAVILY_DAILY_RESERVE,
            "utc_day_validation": "passed" if success else "blocked",
            "btp_reserve_validation": (
                "passed"
                if success and all(item["btp_reserve_intact"] for item in per_key)
                else "blocked"
            ),
            "aggregate_capacity_remaining": aggregate,
            "per_key": per_key,
        },
        "reason_codes": unique_reasons,
        "side_effects": {
            "discovery_dispatched": False,
            "tavily_requests": 0,
            "budget_reservations": 0,
            "application_actions": 0,
            "drafts_created": 0,
            "emails_sent": 0,
            "whatsapp_messages_sent": 0,
            "btp_actions": 0,
            "production_candidate_writes": 0,
        },
    }


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--reservation-path", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    status_path = args.workspace / "data" / "discovery_only_inputs_status.json"
    try:
        restoration = restore_discovery_inputs(
            args.vault,
            args.workspace,
            expected_origin=args.origin,
            expected_branch=args.branch,
        )
    except DiscoveryRestoreError as exc:
        try:
            restoration = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            restoration = {"ready": False, "reason_code": exc.reason_code}
        report = build_restore_preflight_report(
            args.vault,
            args.workspace,
            restoration=restoration,
            reservation_path=args.reservation_path,
        )
        if args.report:
            _write_report(args.report, report)
        print(exc.reason_code, file=sys.stderr)
        return 1
    report = build_restore_preflight_report(
        args.vault,
        args.workspace,
        restoration=restoration,
        reservation_path=args.reservation_path,
    )
    if report["status"] != "COMPLETED":
        failed_status = dict(restoration)
        failed_status.update({
            "ready": False,
            "reason_code": report["reason_code"],
        })
        _write_status(status_path, failed_status)
    if args.report:
        _write_report(args.report, report)
    return 0 if report["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
