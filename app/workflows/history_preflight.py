"""Read-only, employer-scoped WCA history inspection."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import yaml

EMPLOYERS = {
    "el_ouatik_freres": {
        "aliases": ("el ouatik freres", "el ouatiki freres", "elouatiki freres"),
        "domains": ("elouatikifreres.com",),
        "portal": "https://elouatikifreres.com/fr/carrieres/",
        "portal_title": "Carrières — El Ouatik Frères",
        "portal_fields": {"required": [], "optional": []},
        "known_profile_fields": [],
        "missing_applicant_inputs": [],
        "route_type": "official_recruitment_email",
        "role_fit": "spontaneous_application_route; applicant role fit requires current CV and route review",
    },
    "cetemco": {
        "aliases": ("cetemco", "centre des techniques et materiaux de construction"),
        "domains": ("cetemco.org",),
        "portal": "https://cetemco.org/offres-demplois/",
        "portal_title": "Offres d'emplois — CETEMCO",
        "portal_fields": {"required": [], "optional": []},
        "known_profile_fields": [],
        "missing_applicant_inputs": [],
        "route_type": "published_vacancy_email",
        "role_fit": "current page fetched: Ingénieur Technico-commercial; Bac+3 and minimum 2 years of technical-sales experience; current CV fit remains unverified",
    },
    "megec": {
        "aliases": ("megec", "m e g e c"),
        "domains": ("megec.com",),
        "portal": "https://www.megec.com/candidature-spontanee/",
        "portal_title": "Candidature spontanée — MEGEC",
        "portal_fields": {"required": [], "optional": []},
        "known_profile_fields": [],
        "missing_applicant_inputs": [],
        "route_type": "official_spontaneous_application_email",
        "role_fit": "current page invites beginner and experienced technicians in civil engineering and related construction specialities",
    },
    "odbm": {
        "aliases": (
            "odbm",
            "omnium des batisseurs marocains",
            "omnium des batisseurs",
        ),
        "domains": ("entrepriseodbm.ma",),
        "portal": "https://entrepriseodbm.ma/carrieres",
        "portal_title": "Carrières — ODBM | L'Omnium des Bâtisseurs Marocains",
        "portal_fields": {
            "required": ["full_name", "email", "cv_pdf_or_word_max_5mb", "consent"],
            "optional": ["phone", "desired_position", "message"],
        },
        "known_profile_fields": ["full_name", "desired_position", "phone"],
        "missing_applicant_inputs": ["email", "cv_file", "consent_review"],
        "role_fit": "spontaneous_route_only; listed methods-technician role asks for 5 years; profile states 1 year",
        "route_type": "official_spontaneous_application_portal",
    },
    "tgcc": {
        "aliases": ("tgcc", "travaux generaux de construction"),
        "domains": ("tgcc.ma", "recrutement.tgcc.ma"),
        "portal": "https://recrutement.tgcc.ma/",
        "portal_title": "Espace recrutement",
        "portal_fields": {
            "required": [
                "gender",
                "first_name",
                "last_name",
                "age",
                "email",
                "phone",
                "occupation",
                "city",
                "mobility",
                "education_details",
                "experience_details",
                "contract_type",
                "availability",
                "pdf_cv",
                "terms_acceptance",
            ],
            "optional": ["salary_expectations", "motivation"],
        },
        "known_profile_fields": [
            "full_name",
            "phone",
            "occupation",
            "education_details",
            "experience_details",
            "availability_notice_period_1_month",
        ],
        "missing_applicant_inputs": [
            "gender",
            "age",
            "email",
            "city",
            "mobility_choice",
            "contract_type",
            "terms_acceptance",
        ],
        "role_fit": "spontaneous_route; technical and methods categories are available; no vacancy match asserted",
        "route_type": "official_spontaneous_application_portal",
    },
}

UNRESOLVED_ATTEMPT_STATES = {
    "prepared", "dispatching", "uncertain", "creating", "pending",
}
CONFIRMED_STATES = {"sent", "drafted", "confirmed", "confirmed_sent"}
SCREENING_TTLS = {
    "no_verified_website": timedelta(days=30),
    "no_explicit_instructions": timedelta(days=30),
    "no_official_employer_email": timedelta(days=30),
    "portal_or_form": timedelta(days=30),
    "in_progress": timedelta(days=1),
    "blocked_by_safety_gate": timedelta(days=1),
    "research_error": timedelta(hours=6),
}
NONBLOCKING_SCREENING_STATES = {
    "discovered",
    "no_recruitment_contact",
    "crawl_failed",
    "invalid_employer_website",
}


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _host(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return (parsed.hostname or "").casefold().removeprefix("www.")


def _matches_employer(row: dict[str, Any], employer: dict[str, Any]) -> bool:
    text_values = (
        row.get("name", ""),
        row.get("normalized_name", ""),
        row.get("company_name", ""),
        row.get("title", ""),
        row.get("canonical_key", ""),
    )
    normalised = [_normalise(str(value)) for value in text_values if value]
    for alias in employer["aliases"]:
        alias_norm = _normalise(alias)
        if any(
            value == alias_norm
            or (len(alias_norm) >= 4 and alias_norm in value)
            for value in normalised
        ):
            return True
    urls = (row.get("website", ""), row.get("official_domain", ""), row.get("url", ""))
    return any(
        _host(str(value)) in employer["domains"]
        for value in urls
        if value
    )


def _status(status: str, reason: str, *, count: int = 0, **details: Any) -> dict:
    return {
        "status": status,
        "reason_code": reason,
        "record_count": count,
        **details,
    }


def _unknown_employer(reason: str) -> dict:
    unknown = _status("UNKNOWN", reason)
    return {
        "identity_matches": unknown,
        "applications": unknown,
        "wca_tracked_drafts": unknown,
        "live_send_attempts": unknown,
        "pending_draft_attempts": unknown,
        "cooldown": unknown,
        "suppression": unknown,
        "duplicate_suppression": unknown,
        "submission_clearance": unknown,
        "gmail_mailbox_history": _status(
            "NOT_CHECKED", "gmail_mailbox_not_queried_by_read_only_preflight",
        ),
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _table_rows(
    connection: sqlite3.Connection,
    table: str,
    required: set[str],
    optional: set[str] | None = None,
) -> list[dict[str, Any]]:
    columns = _columns(connection, table)
    missing = required - columns
    if missing:
        raise ValueError(f"schema_missing_{table}_columns")
    selected = sorted(required | ((optional or set()) & columns))
    statement = (
        f'SELECT {", ".join(f""" "{column}" """ for column in selected)} '
        f'FROM "{table}"'
    )
    return [dict(zip(selected, row, strict=True)) for row in connection.execute(statement)]


def _load_screening_state(path: Path, employer: dict[str, Any], now: datetime) -> dict:
    if not path.is_file():
        return _status("UNKNOWN", "screening_state_file_missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return _status("UNKNOWN", "screening_state_unreadable")
    if not isinstance(data, dict) or not isinstance(data.get("companies"), dict):
        return _status("UNKNOWN", "screening_state_schema_invalid")

    matches = [
        row for row in data["companies"].values()
        if isinstance(row, dict) and _matches_employer(row, employer)
    ]
    active = []
    for record in matches:
        state = str(record.get("status", "")).casefold()
        if state in {"sent", "drafted_for_review"}:
            active.append(record)
            continue
        ttl = SCREENING_TTLS.get(state)
        if ttl is None:
            if state in NONBLOCKING_SCREENING_STATES:
                continue
            return _status(
                "UNKNOWN", "screening_record_state_unrecognized", count=len(matches),
            )
        updated_text = record.get("updated_at")
        try:
            updated = datetime.fromisoformat(str(updated_text))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return _status(
                "UNKNOWN", "screening_record_timestamp_invalid", count=len(matches),
            )
        if now - updated.astimezone(timezone.utc) < ttl:
            active.append(record)
    if active:
        return _status(
            "BLOCKED", "active_btp_suppression_record", count=len(active),
        )
    return _status(
        "CLEAR", "no_active_matching_btp_suppression_record",
        count=len(matches), matching_records=len(matches),
    )


def _load_profile_context(profile_path: Path, preferences_path: Path) -> dict:
    try:
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        preferences = yaml.safe_load(preferences_path.read_text(encoding="utf-8")) or {}
        if not isinstance(profile, dict) or not isinstance(preferences, dict):
            raise ValueError("profile_or_preferences_schema_invalid")
    except (OSError, UnicodeError, yaml.YAMLError, ValueError):
        return {
            "status": "UNKNOWN",
            "reason_code": "candidate_profile_or_preferences_unreadable",
            "morocco_preference_conflict": "UNKNOWN",
            "known_profile_fields": [],
        }
    target_countries = {
        _normalise(str(country))
        for country in preferences.get("target_countries", [])
    }
    has_morocco = "morocco" in target_countries or "maroc" in target_countries
    sponsorship_required = preferences.get("sponsorship_required")
    conflict = not has_morocco
    profile_domestic_status_available = any(
        profile.get(key) not in (None, "", [], {})
        for key in (
            "country_of_residence", "residence_country",
            "citizenship", "nationality", "country_of_citizenship",
        )
    )
    return {
        "status": "READ",
        "reason_code": "profile_and_preferences_read_without_serializing_personal_values",
        "morocco_preference_conflict": (
            "REVIEW_REQUIRED" if conflict else "NO_CONFLICT_RECORDED"
        ),
        "sponsorship_scope": (
            "INTERNATIONAL_JOBS_ONLY"
            if sponsorship_required is True else "NOT_REQUIRED"
        ),
        "candidate_residency_or_citizenship": (
            "PROFILE_FIELD_AVAILABLE"
            if profile_domestic_status_available else "UNKNOWN"
        ),
        "candidate_residency_reason_code": (
            "profile_field_requires_review"
            if profile_domestic_status_available
            else "candidate_residency_or_citizenship_not_recorded_in_profile"
        ),
        "conflict_reasons": [
            reason for reason, applies in (
                ("morocco_not_in_target_countries", not has_morocco),
            ) if applies
        ],
        "known_profile_fields": [
            "full_name", "phone", "title", "education", "experience",
            "availability_notice_period",
        ],
    }


def _employer_history(
    employer_id: str,
    spec: dict[str, Any],
    tables: dict[str, list[dict[str, Any]]],
    screening_path: Path,
    now: datetime,
    cooldown_days: int,
) -> dict:
    companies = [
        row for row in tables["companies"]
        if _matches_employer(row, spec)
    ]
    company_ids = {row.get("id") for row in companies if row.get("id") is not None}
    identity_status = (
        _status(
            "BLOCKED", "ambiguous_company_identity_match", count=len(companies),
        )
        if len(companies) > 1 else _status(
            "CLEAR",
            "matching_company_rows_found" if companies
            else "no_matching_company_or_alias_rows_in_authoritative_database",
            count=len(companies),
        )
    )

    related_jobs = [
        row for row in tables["jobs"]
        if row.get("company_id") in company_ids
    ]
    job_ids = {row.get("id") for row in related_jobs if row.get("id") is not None}
    applications = [
        row for row in tables["applications"]
        if row.get("job_id") in job_ids
    ]
    app_ids = {row.get("id") for row in applications if row.get("id") is not None}
    application_status = (
        _status(
            "BLOCKED", "prior_application_records_found", count=len(applications),
            outcomes=_safe_counts(applications, "status"),
        )
        if applications else _status(
            "CLEAR", "no_matching_application_row_in_authoritative_database",
        )
    )

    related_emails = [
        row for row in tables["emails"]
        if row.get("application_id") in app_ids
    ]
    draft_records = [
        row for row in related_emails
        if row.get("draft_id")
        or str(row.get("status", "")).casefold() in {"drafted", "creating", "uncertain"}
    ]
    drafts_status = (
        _status(
            "BLOCKED", "wca_tracked_gmail_draft_records_found",
            count=len(draft_records),
        )
        if draft_records else _status(
            "CLEAR", "no_matching_wca_tracked_draft_row_in_authoritative_database",
        )
    )
    pending_drafts = [
        row for row in related_emails
        if str(row.get("delivery_state", "")).casefold() in {"creating", "uncertain"}
        or str(row.get("status", "")).casefold() in {"creating", "uncertain"}
    ]
    pending_draft_status = (
        _status(
            "BLOCKED", "unresolved_wca_draft_attempt_found",
            count=len(pending_drafts),
        )
        if pending_drafts else _status(
            "CLEAR", "no_unresolved_matching_wca_draft_attempt",
        )
    )

    relevant_attempts = [
        row for row in tables.get("live_send_attempts", [])
        if row.get("job_id") in job_ids
        or row.get("active_job_id") in job_ids
        or _matches_employer(row, spec)
    ]
    unresolved = [
        row for row in relevant_attempts
        if str(row.get("state", "")).casefold() in UNRESOLVED_ATTEMPT_STATES
        or row.get("active_job_id") is not None
    ]
    if unresolved:
        attempts_status = _status(
            "BLOCKED", "unresolved_or_active_live_send_attempt_found",
            count=len(unresolved),
            states=_safe_counts(unresolved, "state"),
        )
    elif relevant_attempts:
        attempts_status = _status(
            "BLOCKED", "historical_live_send_attempt_records_found",
            count=len(relevant_attempts),
            states=_safe_counts(relevant_attempts, "state"),
        )
    else:
        attempts_status = _status(
            "CLEAR", "no_matching_live_send_attempt_in_authoritative_database",
        )

    contacts = [
        row for row in tables["contacts"]
        if row.get("company_id") in company_ids
    ]
    cutoff = now - timedelta(days=max(0, cooldown_days))
    recent_contacts = []
    unreadable_contact_time = False
    for row in contacts:
        timestamp = _parse_datetime(row.get("last_contacted_at"))
        if timestamp is None:
            if row.get("last_contacted_at") is not None:
                unreadable_contact_time = True
                break
            continue
        if timestamp >= cutoff:
            recent_contacts.append(row)
    if unreadable_contact_time:
        cooldown_status = _status(
            "UNKNOWN", "contact_timestamp_unreadable", count=len(contacts),
        )
    elif recent_contacts:
        cooldown_status = _status(
            "BLOCKED", "employer_cooldown_active",
            count=len(recent_contacts), cooldown_days=cooldown_days,
        )
    else:
        cooldown_status = _status(
            "CLEAR", "no_matching_contact_within_cooldown",
            count=0, cooldown_days=cooldown_days,
        )
    suppression_status = _load_screening_state(screening_path, spec, now)

    checks = (
        identity_status,
        application_status,
        drafts_status,
        attempts_status,
        pending_draft_status,
        cooldown_status,
        suppression_status,
    )
    overall = (
        "BLOCKED" if any(item["status"] == "BLOCKED" for item in checks)
        else "UNKNOWN" if any(item["status"] != "CLEAR" for item in checks)
        else "CLEAR"
    )
    return {
        "identity_matches": identity_status,
        "applications": application_status,
        "wca_tracked_drafts": drafts_status,
        "live_send_attempts": attempts_status,
        "pending_draft_attempts": pending_draft_status,
        "cooldown": cooldown_status,
        "suppression": suppression_status,
        "duplicate_suppression": _status(
            overall,
            "all_wca_history_checks_clear" if overall == "CLEAR"
            else "one_or_more_history_checks_blocked" if overall == "BLOCKED"
            else "history_not_fully_verified",
            count=sum(item["record_count"] for item in checks),
        ),
        "submission_clearance": _status(
            "BLOCKED" if overall == "BLOCKED" else "UNKNOWN",
            "wca_history_contains_a_blocking_record"
            if overall == "BLOCKED"
            else "gmail_mailbox_history_not_checked",
            count=sum(item["record_count"] for item in checks),
        ),
        "gmail_mailbox_history": _status(
            "NOT_CHECKED", "gmail_mailbox_not_queried_by_read_only_preflight",
        ),
        "matched_company_rows": len(companies),
        "matched_job_rows": len(related_jobs),
    }


def _safe_counts(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(field) or "unknown").casefold()
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", value):
            value = "other"
        counts[value] = counts.get(value, 0) + 1
    return counts


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(
        timezone.utc,
    )


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _has_nonempty_sidecar(paths: tuple[Path, ...]) -> bool | None:
    try:
        return any(path.exists() and path.stat().st_size for path in paths)
    except OSError:
        return None


def _portal_readiness(employer_id: str, spec: dict[str, Any], profile_context: dict) -> dict:
    return {
        "status": "PREVIOUSLY_VERIFIED_NOT_RECHECKED",
        "reason_code": "official_route_evidence_not_crawled_during_read_only_preflight",
        "official_route_url": spec["portal"],
        "portal_url": spec["portal"],
        "page_title": spec["portal_title"],
        "route_type": spec["route_type"],
        "submission_instructions": (
            "Current route evidence must be re-fetched before qualification."
            if "email" in spec["route_type"]
            else "Use the portal's spontaneous-application form; do not substitute email."
        ),
        "wca_submission_support": (
            "SUPPORTED_EMAIL_ROUTE_PENDING_CURRENT_EVIDENCE"
            if "email" in spec["route_type"] else "UNSUPPORTED"
        ),
        "reason": (
            "The read-only history task does not crawl or submit; the BTP target task "
            "must re-fetch official email evidence."
            if "email" in spec["route_type"]
            else "WCA records official portal routes but does not submit web forms."
        ),
        "required_fields": spec["portal_fields"]["required"],
        "optional_fields": spec["portal_fields"]["optional"],
        "known_profile_fields": (
            spec["known_profile_fields"]
            if profile_context.get("status") == "READ" else []
        ),
        "missing_applicant_inputs": spec["missing_applicant_inputs"],
        "candidate_preference_review": profile_context.get(
            "morocco_preference_conflict", "UNKNOWN",
        ),
        "role_fit": spec["role_fit"],
    }


def run_history_preflight(
    *,
    database_path: Path,
    screening_state_path: Path,
    profile_path: Path,
    preferences_path: Path,
    restore_status_path: Path | None = None,
    now: datetime | None = None,
    employer_cooldown_days: int = 7,
    daily_max_outbound: int = 10,
    daily_max_applications: int = 5,
    daily_max_inquiries: int = 5,
    employer_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Read persisted state via immutable SQLite and emit only sanitized facts."""
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    profile_context = _load_profile_context(profile_path, preferences_path)
    selected_employers = tuple(employer_ids or EMPLOYERS)
    if not selected_employers or any(
        employer_id not in EMPLOYERS for employer_id in selected_employers
    ):
        raise ValueError("employer_ids must contain supported employer identifiers")
    restore_status = {
        "vault_snapshot_restored": False,
        "state_database_restored": False,
        "vault_read_performed": False,
    }
    if restore_status_path:
        try:
            value = json.loads(restore_status_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                if "vault_read_performed" not in value:
                    value["vault_read_performed"] = bool(
                        value.get("vault_snapshot_restored", False)
                    )
                restore_status.update({
                    key: bool(value.get(key, False))
                    for key in (
                        "vault_snapshot_restored",
                        "state_database_restored",
                        "vault_read_performed",
                    )
                })
        except (OSError, UnicodeError, json.JSONDecodeError):
            restore_status = {
                "vault_snapshot_restored": False,
                "state_database_restored": False,
                "vault_read_performed": False,
            }

    before_hash = _sha256(database_path)
    db_reason = ""
    employers: dict[str, dict[str, Any]]
    tables: dict[str, list[dict[str, Any]]] = {}
    wal_path = Path(f"{database_path}-wal")
    journal_path = Path(f"{database_path}-journal")
    sidecars = (wal_path, journal_path)
    if not restore_status.get("vault_snapshot_restored", False):
        db_reason = "vault_snapshot_restore_not_confirmed"
    elif not restore_status.get("state_database_restored", False):
        db_reason = "vault_state_database_restore_failed"
    elif before_hash is None:
        db_reason = "authoritative_database_missing_or_unreadable"
    elif _has_nonempty_sidecar(sidecars) is None:
        db_reason = "sqlite_sidecar_state_unreadable"
    elif _has_nonempty_sidecar(sidecars):
        db_reason = "sqlite_sidecar_requires_checkpoint_before_immutable_read"
    else:
        uri_path = quote(
            str(database_path.resolve()).replace(chr(92), "/"),
            safe=":/",
        )
        uri = f"file:{uri_path}?mode=ro&immutable=1"
        connection = None
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            table_names = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'",
                )
            }
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                db_reason = "database_foreign_key_integrity_check_failed"
            required_tables = {
                "companies": {"id", "name", "normalized_name", "website", "official_domain"},
                "jobs": {"id", "company_id"},
                "applications": {"id", "job_id", "status", "sent_at", "action"},
                "emails": {"id", "application_id", "status", "draft_id", "delivery_state"},
                "contacts": {"id", "company_id", "last_contacted_at"},
                "live_send_attempts": {
                    "id", "job_id", "active_job_id", "state", "employer_name",
                },
                "discoveries": {
                    "id", "canonical_key", "url", "company_name", "company_id",
                },
            }
            missing_tables = set(required_tables) - table_names
            if not db_reason and missing_tables:
                db_reason = "required_history_tables_missing"
            elif not db_reason:
                for table, required_columns in required_tables.items():
                    tables[table] = _table_rows(
                        connection,
                        table,
                        required_columns,
                    )
        except (sqlite3.Error, OSError, ValueError):
            db_reason = "authoritative_database_read_or_schema_check_failed"
        finally:
            if connection is not None:
                with suppress(sqlite3.Error):
                    connection.close()

    after_hash = _sha256(database_path)
    sidecars_after = _has_nonempty_sidecar(sidecars)
    read_only_verified = bool(
        before_hash and after_hash and before_hash == after_hash
        and sidecars_after is False
    )
    if db_reason:
        employers = {
            employer_id: _unknown_employer(db_reason)
            for employer_id in selected_employers
        }
    else:
        employers = {
            employer_id: _employer_history(
                employer_id,
                spec,
                tables,
                screening_state_path,
                now,
                employer_cooldown_days,
            )
            for employer_id, spec in EMPLOYERS.items()
            if employer_id in selected_employers
        }
        discovery_rows = tables["discoveries"]
        for employer_id, spec in EMPLOYERS.items():
            if employer_id not in selected_employers:
                continue
            unlinked = [
                row for row in discovery_rows
                if _matches_employer(row, spec)
                and row.get("company_id") not in {
                    company.get("id")
                    for company in tables["companies"]
                    if _matches_employer(company, spec)
                }
            ]
            if unlinked:
                current = employers[employer_id]["identity_matches"]
                current.update({
                    "status": "UNKNOWN",
                    "reason_code": "unlinked_alias_discovery_records_found",
                    "record_count": current["record_count"] + len(unlinked),
                    "unlinked_discovery_records": len(unlinked),
                })
                employers[employer_id]["duplicate_suppression"].update({
                    "status": "UNKNOWN",
                    "reason_code": "ambiguous_unlinked_employer_history",
                })

    profile_context["morocco_preference_conflict"] = profile_context.get(
        "morocco_preference_conflict", "UNKNOWN",
    )
    for employer_id in selected_employers:
        spec = EMPLOYERS[employer_id]
        employers[employer_id]["portal"] = _portal_readiness(
            employer_id, spec, profile_context,
        )

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    sent_apps_today = [
        row for row in tables.get("applications", [])
        if str(row.get("status", "")).casefold() in CONFIRMED_STATES
        and (_parse_datetime(row.get("sent_at")) or datetime.min.replace(
            tzinfo=timezone.utc,
        )) >= today_start
    ]
    app_count = sum(
        str(row.get("action", "")).casefold()
        in {"apply", "application", "spontaneous_application"}
        for row in sent_apps_today
    )
    inquiry_count = sum(
        str(row.get("action", "")).casefold() in {"ask_employer", "inquiry"}
        for row in sent_apps_today
    )
    outbound_status = "UNKNOWN" if db_reason else (
        "BLOCKED" if (
            len(sent_apps_today) >= daily_max_outbound
            or app_count >= daily_max_applications
            or inquiry_count >= daily_max_inquiries
        ) else "CLEAR"
    )
    report = {
        "workflow": "history_preflight",
        "report_schema_version": 1,
        "read_only": bool(read_only_verified and not db_reason),
        "database": {
            "status": "READ_ONLY_VERIFIED" if read_only_verified and not db_reason else "UNKNOWN",
            "reason_code": (
                "immutable_sqlite_read_and_hash_unchanged"
                if read_only_verified and not db_reason else db_reason or "database_unchanged_check_failed"
            ),
            "modified": False if read_only_verified else None,
        },
        "side_effects": {
            "database_modified": False if read_only_verified else None,
            "vault_read": bool(
                restore_status.get(
                    "vault_read_performed",
                    restore_status.get("vault_snapshot_restored", False),
                )
            ),
            "vault_modified": False,
            "messaging": False,
            "gmail_provider": False,
            "discovery_or_llm_provider": False,
        },
        "scope": {
            "database_history": "WCA_TRACKED_ONLY",
            "gmail_mailbox_history": "NOT_CHECKED",
            "gmail_mailbox_reason_code": (
                "gmail_readonly_scope_unavailable_or_not_used; mailbox history not checked"
            ),
            "production_snapshot_restored": bool(
                restore_status.get("vault_snapshot_restored", False),
            ),
            "state_database_restored": bool(
                restore_status.get("state_database_restored", False),
            ),
        },
        "employers": employers,
        "global_outbound_restrictions": _status(
            outbound_status,
            "daily_outbound_limit_reached" if outbound_status == "BLOCKED"
            else "daily_outbound_counts_below_configured_limits"
            if outbound_status == "CLEAR" else "daily_outbound_history_unavailable",
            count=len(sent_apps_today),
            sent_applications_today=app_count,
            sent_inquiries_today=inquiry_count,
            limits={
                "outbound": daily_max_outbound,
                "applications": daily_max_applications,
                "inquiries": daily_max_inquiries,
            },
        ),
        "candidate_preference_review": profile_context,
    }
    return report
