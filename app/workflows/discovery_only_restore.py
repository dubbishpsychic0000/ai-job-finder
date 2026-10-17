"""Fail-closed restoration of bounded discovery inputs from the private vault."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
    _ledger_usage_by_type,
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


def _budget_ledger_diagnostic(path: Path, *, today: date) -> tuple[str, dict | None]:
    """Return a safe outcome code and a parsed ledger only when structurally valid."""
    if not path.is_file():
        return "ledger_missing", None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return "ledger_json_invalid", None
    if not isinstance(payload, dict):
        return "ledger_schema_invalid", None
    if (
        not isinstance(payload.get("date"), str)
        or not isinstance(payload.get("count"), int)
        or isinstance(payload.get("count"), bool)
        or not isinstance(payload.get("exhausted"), bool)
    ):
        return "ledger_schema_invalid", None
    if not 0 <= payload["count"] <= TAVILY_DAILY_REQUEST_LIMIT:
        return "ledger_count_out_of_range", None
    if "usage_by_type" in payload:
        try:
            _ledger_usage_by_type(payload, payload["count"])
        except ValueError:
            return "ledger_provenance_invalid", None
    try:
        ledger_date = date.fromisoformat(payload["date"])
    except ValueError:
        return "ledger_date_invalid", None
    if ledger_date.isoformat() != payload["date"]:
        return "ledger_date_invalid", None
    if ledger_date > today:
        return "ledger_future_dated", None
    if ledger_date < today:
        return "prior_day_rollover_pending", payload
    if payload["exhausted"]:
        return "ledger_exhausted", payload
    return "valid_current_day", payload


def _valid_budget_ledger(path: Path, *, today: date) -> bool:
    outcome, _payload = _budget_ledger_diagnostic(path, today=today)
    return outcome in {
        "valid_current_day",
        "prior_day_rollover_pending",
        "ledger_exhausted",
    }


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
    ledger_diagnostics: list[dict[str, str]] = []
    key_parse_reason = ""
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

        try:
            key_text = required_files["keys"].read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            key_parse_reason = "provider_key_file_invalid"
            raise DiscoveryRestoreError(key_parse_reason) from exc
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
                key_parse_reason = "provider_budget_ledger_collision"
                raise DiscoveryRestoreError("provider_budget_ledger_collision")
            fingerprints[fingerprint] = key
            ledger = repository / "state" / f"tavily_budget_{fingerprint}.json"
            outcome, _payload = _budget_ledger_diagnostic(ledger, today=now_date)
            ledger_diagnostics.append({
                "fingerprint": fingerprint,
                "outcome": outcome,
            })
            ledgers.append(ledger)
        expected_key_count = len(fingerprints)
        if any(
            item["outcome"] not in {
                "valid_current_day",
                "prior_day_rollover_pending",
                "ledger_exhausted",
            }
            for item in ledger_diagnostics
        ):
            raise DiscoveryRestoreError(
                "provider_budget_ledger_missing_or_invalid",
            )

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
            "ledger_diagnostics": ledger_diagnostics,
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
            "ledger_diagnostics": ledger_diagnostics,
            "key_parse_reason": key_parse_reason,
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
            "btp_reserve_status": "unverifiable_ledger",
            "usage_provenance": "unavailable",
            "usage_by_type": None,
            "general_limit_crossed": None,
            "above_general_limit_source": "unknown",
            "eligible_for_general_discovery": False,
            "general_capacity_after_rollover": 0,
            "outcome": "ledger_missing",
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
        outcome, payload = _budget_ledger_diagnostic(
            observed_ledger_path, today=now_date,
        )
        item["outcome"] = outcome
        if payload is None:
            item["reason_code"] = {
                "ledger_missing": "provider_budget_ledger_missing",
                "ledger_future_dated": "provider_budget_ledger_from_future",
                "ledger_provenance_invalid": (
                    "provider_budget_ledger_provenance_invalid"
                ),
            }.get(outcome, "provider_budget_ledger_missing_or_invalid")
            reasons.append(item["reason_code"])
            per_key.append(item)
            continue
        ledger_date = date.fromisoformat(payload["date"])

        item["ledger_valid"] = True
        item["ledger_utc_day_matches"] = ledger_date == now_date
        item["ledger_date_relation"] = (
            "current_utc_day" if ledger_date == now_date else "prior_utc_day"
        )
        item["count"] = payload["count"]
        item["exhausted"] = payload["exhausted"]
        provenance_present = "usage_by_type" in payload
        usage_by_type = _ledger_usage_by_type(payload, payload["count"])
        item["usage_by_type"] = usage_by_type
        item["usage_provenance"] = (
            "verified"
            if provenance_present and usage_by_type["unattributed"] == 0
            else "unknown"
        )
        if outcome == "ledger_future_dated":
            item["ledger_date_relation"] = "future_utc_day"
            item["reason_code"] = "provider_budget_ledger_from_future"
            reasons.append(item["reason_code"])
        elif outcome == "prior_day_rollover_pending":
            item["eligible_for_general_discovery"] = True
            item["general_capacity_after_rollover"] = GENERAL_TAVILY_DAILY_LIMIT
            item["btp_reserve_status"] = "rollover_pending"
            item["general_limit_crossed"] = None
            item["above_general_limit_source"] = "not_applicable_after_rollover"
            item["reason_code"] = "utc_day_rollover_pending_durable_reservation"
        else:
            item["total_remaining"] = max(
                0, TAVILY_DAILY_REQUEST_LIMIT - payload["count"],
            )
            general_usage = usage_by_type["general"]
            btp_usage = usage_by_type["btp"]
            unattributed_usage = usage_by_type["unattributed"]
            effective_general_usage = general_usage + unattributed_usage
            item["general_remaining"] = max(
                0,
                min(
                    GENERAL_TAVILY_DAILY_LIMIT - effective_general_usage,
                    TAVILY_DAILY_REQUEST_LIMIT - payload["count"],
                ),
            )
            if provenance_present and unattributed_usage == 0:
                item["btp_reserve_remaining"] = max(
                    0, BTP_TAVILY_DAILY_RESERVE - btp_usage,
                )
                general_crossed = general_usage > GENERAL_TAVILY_DAILY_LIMIT
                item["general_limit_crossed"] = general_crossed
                if general_crossed:
                    item["btp_reserve_status"] = "general_budget_breach"
                    item["above_general_limit_source"] = (
                        "verified_general_discovery"
                    )
                elif btp_usage > BTP_TAVILY_DAILY_RESERVE:
                    item["btp_reserve_status"] = "btp_reserve_breach"
                    item["above_general_limit_source"] = (
                        "verified_btp"
                        if payload["count"] > GENERAL_TAVILY_DAILY_LIMIT
                        else "not_applicable"
                    )
                elif btp_usage:
                    item["btp_reserve_status"] = "verified_authorized_btp_usage"
                    item["above_general_limit_source"] = (
                        "verified_btp"
                        if payload["count"] > GENERAL_TAVILY_DAILY_LIMIT
                        else "not_applicable"
                    )
                else:
                    item["btp_reserve_status"] = "verified_intact"
                    item["above_general_limit_source"] = (
                        "not_applicable"
                        if payload["count"] <= GENERAL_TAVILY_DAILY_LIMIT
                        else "unknown"
                    )
                item["btp_reserve_intact"] = (
                    not general_crossed
                    and btp_usage <= BTP_TAVILY_DAILY_RESERVE
                )
            else:
                item["general_remaining"] = None
                item["btp_reserve_status"] = "unknown_provenance"
                item["general_limit_crossed"] = (
                    True if general_usage > GENERAL_TAVILY_DAILY_LIMIT
                    else False
                    if payload["count"] <= GENERAL_TAVILY_DAILY_LIMIT
                    else None
                )
                item["above_general_limit_source"] = (
                    "unknown"
                    if payload["count"] > GENERAL_TAVILY_DAILY_LIMIT
                    else "not_applicable"
                )
            item["eligible_for_general_discovery"] = (
                outcome == "valid_current_day"
                and not payload["exhausted"]
                and item["general_remaining"] is not None
                and item["general_remaining"] > 0
                and item["usage_provenance"] == "verified"
                and item["btp_reserve_status"] not in {
                    "general_budget_breach",
                    "btp_reserve_breach",
                }
            )
            if item["eligible_for_general_discovery"]:
                item["reason_code"] = ""
            elif item["btp_reserve_status"] == "general_budget_breach":
                item["reason_code"] = "general_budget_exceeded_protected_limit"
                reasons.append(item["reason_code"])
            elif item["btp_reserve_status"] == "btp_reserve_breach":
                item["reason_code"] = "btp_reserve_exceeded_protected_limit"
                reasons.append(item["reason_code"])
            elif item["usage_provenance"] != "verified":
                item["reason_code"] = "provider_budget_provenance_unavailable"
            elif outcome == "ledger_exhausted" or payload["exhausted"]:
                item["reason_code"] = "provider_key_exhausted"
            else:
                item["reason_code"] = "general_budget_exhausted"
        per_key.append(item)

    input_expected = restoration.get("expected_input_count", 4)
    input_restored = restoration.get("restored_input_count", 0)
    ledger_expected = restoration.get("expected_budget_ledger_count")
    ledger_restored = restoration.get("restored_budget_ledger_count", 0)
    if (
        isinstance(input_expected, int)
        and not isinstance(input_expected, bool)
        and isinstance(input_restored, int)
        and not isinstance(input_restored, bool)
        and input_expected != input_restored
    ):
        reasons.append("restored_input_count_mismatch")
    if (
        isinstance(ledger_expected, int)
        and not isinstance(ledger_expected, bool)
        and isinstance(ledger_restored, int)
        and not isinstance(ledger_restored, bool)
        and (
            ledger_expected != ledger_restored
            or ledger_expected != len(fingerprints)
        )
    ):
        reasons.append("restored_budget_ledger_count_mismatch")
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

    eligible_keys = [
        item for item in per_key
        if item["eligible_for_general_discovery"]
    ]
    current_general_remaining = sum(
        item["general_remaining"] or 0
        for item in eligible_keys
        if item.get("ledger_date_relation") == "current_utc_day"
    )
    rollover_general_capacity = sum(
        item["general_capacity_after_rollover"]
        for item in eligible_keys
        if item.get("ledger_date_relation") == "prior_utc_day"
    )
    current_ledgers = [
        item for item in per_key
        if item.get("ledger_date_relation") == "current_utc_day"
    ]
    current_btp_reserve_is_known = all(item["ledger_valid"] for item in per_key) and all(
        item["btp_reserve_status"] in {
            "verified_intact",
            "verified_authorized_btp_usage",
            "btp_reserve_breach",
            "general_budget_breach",
        }
        for item in current_ledgers
    )
    aggregate = {
        "total_remaining": sum(
            item["total_remaining"] or 0
            for item in per_key
            if item.get("ledger_date_relation") == "current_utc_day"
        ),
        "general_remaining": current_general_remaining,
        "btp_reserve_remaining": (
            sum(item["btp_reserve_remaining"] or 0 for item in current_ledgers)
            if current_ledgers and current_btp_reserve_is_known else None
        ),
    }
    if any(not item["ledger_valid"] for item in per_key):
        reasons.append("provider_budget_ledger_missing_or_invalid")
    if not eligible_keys:
        reasons.append("no_general_budget_available")
    unique_reasons = list(dict.fromkeys(reasons))
    success = (
        not unique_reasons
        and len(per_key) == len(fingerprints)
        and all(
            item["btp_reserve_status"] in {
                "rollover_pending",
                "verified_intact",
                "verified_authorized_btp_usage",
            }
            for item in eligible_keys
        )
    )
    reserve_statuses = {item["btp_reserve_status"] for item in per_key}
    if any(not item["ledger_valid"] for item in per_key) or (
        reserve_statuses & {
            "general_budget_breach",
            "btp_reserve_breach",
        }
    ):
        aggregate_reserve_validation = "blocked"
    elif reserve_statuses & {
        "unknown_provenance",
        "unverifiable_ledger",
    }:
        aggregate_reserve_validation = "unknown"
    elif "rollover_pending" in reserve_statuses:
        aggregate_reserve_validation = "pending_rollover"
    elif "verified_authorized_btp_usage" in reserve_statuses:
        aggregate_reserve_validation = "passed_with_authorized_btp_usage"
    elif per_key and reserve_statuses == {"verified_intact"}:
        aggregate_reserve_validation = "passed"
    else:
        aggregate_reserve_validation = "blocked"
    return {
        "schema_version": 1,
        "mode": "restore_only",
        "status": "COMPLETED" if success else "BLOCKED",
        "reason_code": "general_capacity_available" if success else unique_reasons[0],
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
            "internal_failure_category": (
                restoration.get("internal_failure_category")
                if isinstance(
                    restoration.get("internal_failure_category"), str,
                ) and re.fullmatch(
                    r"[A-Za-z][A-Za-z0-9]{0,63}",
                    restoration["internal_failure_category"],
                )
                else ""
            ),
            "key_parse_reason": (
                restoration.get("key_parse_reason")
                if isinstance(restoration.get("key_parse_reason"), str)
                and restoration.get("key_parse_reason") in {
                    "vault_provider_keys_missing",
                    "provider_key_file_invalid",
                    "provider_budget_ledger_collision",
                }
                else ""
            ),
            "ledger_diagnostics": [
                {
                    "fingerprint": item["fingerprint"],
                    "outcome": item["outcome"],
                }
                for item in restoration.get("ledger_diagnostics", [])
                if isinstance(item, dict)
                and isinstance(item.get("fingerprint"), str)
                and re.fullmatch(r"[a-f0-9]{8}", item["fingerprint"])
                and item.get("outcome") in {
                    "valid_current_day",
                    "prior_day_rollover_pending",
                    "ledger_missing",
                    "ledger_json_invalid",
                    "ledger_schema_invalid",
                    "ledger_date_invalid",
                    "ledger_future_dated",
                    "ledger_count_out_of_range",
                    "ledger_provenance_invalid",
                    "ledger_exhausted",
                }
            ],
        },
        "provider_budget": {
            "requests_dispatched": 0,
            "reservations_created": 0,
            "limit_per_key": TAVILY_DAILY_REQUEST_LIMIT,
            "general_limit_per_key": GENERAL_TAVILY_DAILY_LIMIT,
            "btp_reserve_per_key": BTP_TAVILY_DAILY_RESERVE,
            "utc_day_validation": (
                "passed"
                if success and rollover_general_capacity == 0
                else "passed_with_durable_rollover"
                if success
                else "blocked"
            ),
            "btp_reserve_validation": aggregate_reserve_validation,
            "aggregate_capacity_remaining": aggregate,
            "btp_reserve_ledger_count": len(per_key),
            "btp_reserve_unresolved_ledger_count": sum(
                item["btp_reserve_status"] in {
                    "unknown_provenance",
                    "unverifiable_ledger",
                }
                for item in per_key
            ),
            "eligible_general_key_count": len(eligible_keys),
            "excluded_general_key_count": len(per_key) - len(eligible_keys),
            "general_capacity_current_utc_day": current_general_remaining,
            "general_capacity_after_durable_rollover": rollover_general_capacity,
            "btp_reserve_capacity_after_durable_rollover": (
                len([
                    item for item in eligible_keys
                    if item.get("ledger_date_relation") == "prior_utc_day"
                ]) * BTP_TAVILY_DAILY_RESERVE
            ),
            "per_key": per_key,
        },
        "discovery_gate": {
            "authorized": success,
            "policy": "eligible_keys_only_preserve_per_key_btp_reserve",
            "reason_code": (
                "general_capacity_available" if success else unique_reasons[0]
            ),
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


def _write_blocked_discovery_report(
    path: Path | None,
    *,
    restoration: dict,
    fallback_reason: str,
) -> None:
    if path is None:
        return
    from app.workflows.discovery_only import blocked_report

    reason = restoration.get("reason_code")
    if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason):
        reason = fallback_reason
    report = blocked_report(reason, phase="input_restore")
    key_parse_reason = restoration.get("key_parse_reason")
    report["provider"]["restore_diagnostic_reason"] = (
        key_parse_reason
        if isinstance(key_parse_reason, str) and key_parse_reason in {
            "vault_provider_keys_missing",
            "provider_key_file_invalid",
            "provider_budget_ledger_collision",
        }
        else ""
    )
    for field in (
        "expected_budget_ledger_count",
        "restored_budget_ledger_count",
    ):
        value = restoration.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            report["provider"][field] = value
    allowed_outcomes = {
        "valid_current_day",
        "prior_day_rollover_pending",
        "ledger_missing",
        "ledger_json_invalid",
        "ledger_schema_invalid",
        "ledger_date_invalid",
        "ledger_future_dated",
        "ledger_count_out_of_range",
        "ledger_provenance_invalid",
        "ledger_exhausted",
    }
    ledger_diagnostics = restoration.get("ledger_diagnostics")
    if isinstance(ledger_diagnostics, list):
        report["provider"]["ledger_diagnostics"] = [
            {
                "fingerprint": item["fingerprint"],
                "outcome": item["outcome"],
            }
            for item in ledger_diagnostics
            if isinstance(item, dict)
            and isinstance(item.get("fingerprint"), str)
            and re.fullmatch(r"[a-f0-9]{8}", item["fingerprint"])
            and item.get("outcome") in allowed_outcomes
        ]
    _write_report(path, report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--reservation-path", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--discovery-report", type=Path)
    parser.add_argument(
        "--restore-failure-reason",
        choices=(
            "vault_credentials_missing",
            "vault_clone_failed",
            "vault_sparse_checkout_failed",
            "vault_checkout_failed",
            "vault_required_input_missing",
            "vault_provider_keys_missing",
            "provider_key_file_invalid",
            "provider_budget_ledger_collision",
            "restore_readiness_manifest_invalid",
        ),
    )
    args = parser.parse_args(argv)
    status_path = args.workspace / "data" / "discovery_only_inputs_status.json"
    if args.restore_failure_reason:
        try:
            restoration = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            restoration = {}
        restoration.update({
            "ready": False,
            "reason_code": args.restore_failure_reason,
        })
        _write_status(status_path, restoration)
        report = build_restore_preflight_report(
            args.vault,
            args.workspace,
            restoration=restoration,
            reservation_path=args.reservation_path,
        )
        _write_blocked_discovery_report(
            args.discovery_report,
            restoration=restoration,
            fallback_reason=args.restore_failure_reason,
        )
        if args.report:
            _write_report(args.report, report)
        return 1
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
        _write_blocked_discovery_report(
            args.discovery_report,
            restoration=restoration,
            fallback_reason=exc.reason_code,
        )
        if args.report:
            _write_report(args.report, report)
        print(exc.reason_code, file=sys.stderr)
        return 1
    except Exception as exc:
        try:
            restoration = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            restoration = {}
        exception_class = type(exc).__name__
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", exception_class):
            exception_class = "Exception"
        restoration.update({
            "ready": False,
            "reason_code": "vault_restore_failed",
            "internal_failure_category": exception_class,
        })
        _write_status(status_path, restoration)
        report = build_restore_preflight_report(
            args.vault,
            args.workspace,
            restoration=restoration,
            reservation_path=args.reservation_path,
        )
        _write_blocked_discovery_report(
            args.discovery_report,
            restoration=restoration,
            fallback_reason="vault_restore_failed",
        )
        if args.report:
            _write_report(args.report, report)
        print("vault_restore_failed", file=sys.stderr)
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
        _write_blocked_discovery_report(
            args.discovery_report,
            restoration=failed_status,
            fallback_reason=report["reason_code"],
        )
    if args.report:
        _write_report(args.report, report)
    return 0 if report["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
