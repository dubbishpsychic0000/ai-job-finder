from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from app.workflows.history_preflight import run_history_preflight

SCHEMA = """
CREATE TABLE companies (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, normalized_name TEXT NOT NULL,
    website TEXT NOT NULL, official_domain TEXT NOT NULL
);
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY, company_id INTEGER, title TEXT NOT NULL
);
CREATE TABLE applications (
    id INTEGER PRIMARY KEY, job_id INTEGER, status TEXT NOT NULL,
    sent_at TEXT, action TEXT
);
CREATE TABLE emails (
    id INTEGER PRIMARY KEY, application_id INTEGER, status TEXT NOT NULL,
    draft_id TEXT, delivery_state TEXT, body TEXT, to_addr TEXT
);
CREATE TABLE contacts (
    id INTEGER PRIMARY KEY, company_id INTEGER, last_contacted_at TEXT
);
CREATE TABLE live_send_attempts (
    id INTEGER PRIMARY KEY, job_id INTEGER, active_job_id INTEGER,
    state TEXT NOT NULL, employer_name TEXT NOT NULL
);
CREATE TABLE discoveries (
    id INTEGER PRIMARY KEY, canonical_key TEXT, url TEXT,
    company_name TEXT, company_id INTEGER
);
"""


def _prepare_inputs(tmp_path: Path, *, production_schema: bool = True):
    production_db = tmp_path / "vault-state" / "agent.db"
    production_db.parent.mkdir(parents=True)
    if production_schema:
        with closing(sqlite3.connect(production_db)) as connection, connection:
            connection.executescript(SCHEMA)
    screening = tmp_path / "btp_outreach_state.json"
    screening.write_text(json.dumps({"schema_version": 1, "companies": {}}))
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "name: Candidate\n"
        "title: Technicien Spécialisé en Génie Civil\n"
        "phone: '+212600000000'\n"
        "experience_years: 1\n"
        "education: [{degree: Technicien spécialisé, year: 2026}]\n"
        "availability: {notice_period_months: 1}\n",
        encoding="utf-8",
    )
    preferences = tmp_path / "preferences.yaml"
    preferences.write_text(
        "target_countries: [France, Belgium]\n"
        "sponsorship_required: true\n",
        encoding="utf-8",
    )
    restore_status = tmp_path / "restore_status.json"
    restore_status.write_text(json.dumps({
        "vault_snapshot_restored": True,
        "state_database_restored": True,
        "vault_read_performed": True,
    }))
    return production_db, screening, profile, preferences, restore_status


def _run(inputs, **kwargs):
    database, screening, profile, preferences, restore_status = inputs
    return run_history_preflight(
        database_path=database,
        screening_state_path=screening,
        profile_path=profile,
        preferences_path=preferences,
        restore_status_path=restore_status,
        **kwargs,
    )


def _seed_company(connection, *, employer="TGCC"):
    if employer == "TGCC":
        connection.execute(
            "INSERT INTO companies VALUES (?, ?, ?, ?, ?)",
            (1, "Travaux Généraux de Construction", "travaux generaux de construction",
             "https://tgcc.ma/", "tgcc.ma"),
        )
    else:
        connection.execute(
            "INSERT INTO companies VALUES (?, ?, ?, ?, ?)",
            (1, "L'Omnium des Bâtisseurs Marocains", "omnium des batisseurs marocains",
             "https://entrepriseodbm.ma/", "entrepriseodbm.ma"),
        )
    connection.execute("INSERT INTO jobs VALUES (1, 1, 'Spontaneous application')")


def test_preflight_reads_restored_production_database_not_empty_local_db(tmp_path):
    inputs = _prepare_inputs(tmp_path)
    production_db = inputs[0]
    local_empty = tmp_path / "empty-local-agent.db"
    with closing(sqlite3.connect(production_db)) as connection, connection:
        _seed_company(connection)
        connection.execute(
            "INSERT INTO applications VALUES (1, 1, 'sent', '2026-10-12T12:00:00+00:00', 'APPLY')",
        )
    with closing(sqlite3.connect(local_empty)) as connection, connection:
        connection.executescript(SCHEMA)

    before = hashlib.sha256(production_db.read_bytes()).hexdigest()
    report = _run(inputs)
    after = hashlib.sha256(production_db.read_bytes()).hexdigest()

    assert report["database"]["status"] == "READ_ONLY_VERIFIED"
    assert before == after
    assert report["read_only"] is True
    assert report["employers"]["tgcc"]["applications"]["status"] == "BLOCKED"
    assert report["employers"]["tgcc"]["applications"]["reason_code"] == (
        "prior_application_records_found"
    )
    assert report["employers"]["odbm"]["applications"]["status"] == "CLEAR"
    assert report["employers"]["odbm"]["applications"]["reason_code"] == (
        "no_matching_application_row_in_authoritative_database"
    )
    assert local_empty.exists()


@pytest.mark.parametrize(
    ("application_status", "email_status", "draft_id", "expected_draft_status"),
    [
        ("sent", "sent", "", "CLEAR"),
        ("drafted", "drafted", "opaque-draft-id", "BLOCKED"),
    ],
)
def test_prior_application_and_gmail_draft_are_reported(
    tmp_path, application_status, email_status, draft_id, expected_draft_status,
):
    inputs = _prepare_inputs(tmp_path)
    with closing(sqlite3.connect(inputs[0])) as connection, connection:
        _seed_company(connection, employer="ODBM")
        connection.execute(
            "INSERT INTO applications VALUES (1, 1, ?, '2026-10-12T12:00:00+00:00', 'APPLY')",
            (application_status,),
        )
        connection.execute(
            "INSERT INTO emails VALUES (1, 1, ?, ?, 'confirmed', 'private body', 'private@example.com')",
            (email_status, draft_id),
        )

    report = _run(inputs)
    odbm = report["employers"]["odbm"]

    assert odbm["applications"]["status"] == "BLOCKED"
    assert odbm["applications"]["outcomes"] == {application_status: 1}
    assert odbm["wca_tracked_drafts"]["status"] == expected_draft_status
    serialized = json.dumps(report)
    assert "private body" not in serialized
    assert "private@example.com" not in serialized
    if draft_id:
        assert draft_id not in serialized


def test_unresolved_live_send_and_draft_attempts_block_clearance(tmp_path):
    inputs = _prepare_inputs(tmp_path)
    with closing(sqlite3.connect(inputs[0])) as connection, connection:
        _seed_company(connection)
        connection.execute(
            "INSERT INTO applications VALUES (1, 1, 'deferred', NULL, 'APPLY')",
        )
        connection.execute(
            "INSERT INTO emails VALUES (1, 1, 'uncertain', '', 'uncertain', 'private body', 'private@example.com')",
        )
        connection.execute(
            "INSERT INTO live_send_attempts VALUES (1, 1, 1, 'dispatching', 'TGCC')",
        )

    report = _run(inputs)
    tgcc = report["employers"]["tgcc"]

    assert tgcc["pending_draft_attempts"]["status"] == "BLOCKED"
    assert tgcc["pending_draft_attempts"]["reason_code"] == (
        "unresolved_wca_draft_attempt_found"
    )
    assert tgcc["live_send_attempts"]["status"] == "BLOCKED"
    assert tgcc["live_send_attempts"]["reason_code"] == (
        "unresolved_or_active_live_send_attempt_found"
    )
    assert tgcc["duplicate_suppression"]["status"] == "BLOCKED"


def test_alias_normalization_and_unlinked_alias_rows_fail_closed(tmp_path):
    inputs = _prepare_inputs(tmp_path)
    with sqlite3.connect(inputs[0]) as connection:
        _seed_company(connection, employer="ODBM")
        connection.execute(
            "INSERT INTO discoveries VALUES (1, 'odbm-alias', '', 'ODBM', NULL)",
        )

    report = _run(inputs)
    odbm = report["employers"]["odbm"]

    assert odbm["matched_company_rows"] == 1
    assert odbm["identity_matches"]["status"] == "UNKNOWN"
    assert odbm["identity_matches"]["reason_code"] == (
        "unlinked_alias_discovery_records_found"
    )
    assert odbm["duplicate_suppression"]["status"] == "UNKNOWN"


def test_active_cooldown_and_suppression_are_reported_as_blocked(tmp_path):
    inputs = _prepare_inputs(tmp_path)
    with sqlite3.connect(inputs[0]) as connection:
        _seed_company(connection)
        connection.execute(
            "INSERT INTO contacts VALUES (1, 1, '2026-10-13T08:00:00+00:00')",
        )
    inputs[1].write_text(json.dumps({
        "schema_version": 1,
        "companies": {
            "target": {
                "name": "TGCC",
                "city": "Casablanca",
                "status": "portal_or_form",
                "updated_at": "2026-10-13T08:00:00+00:00",
            },
        },
    }))

    report = _run(
        inputs,
        now=datetime.fromisoformat("2026-10-13T09:00:00+00:00"),
    )
    tgcc = report["employers"]["tgcc"]

    assert tgcc["cooldown"]["status"] == "BLOCKED"
    assert tgcc["cooldown"]["reason_code"] == "employer_cooldown_active"
    assert tgcc["suppression"]["status"] == "BLOCKED"
    assert tgcc["duplicate_suppression"]["status"] == "BLOCKED"
    assert tgcc["submission_clearance"]["status"] == "BLOCKED"


def test_daily_outbound_limit_is_reported_from_persisted_application_state(tmp_path):
    inputs = _prepare_inputs(tmp_path)
    with sqlite3.connect(inputs[0]) as connection:
        _seed_company(connection)
        connection.execute(
            "INSERT INTO applications VALUES (1, 1, 'drafted', '2026-10-13T08:00:00+00:00', 'SPONTANEOUS_APPLICATION')",
        )

    report = _run(
        inputs,
        now=datetime.fromisoformat("2026-10-13T09:00:00+00:00"),
        daily_max_applications=1,
    )

    assert report["global_outbound_restrictions"]["status"] == "BLOCKED"
    assert report["global_outbound_restrictions"]["reason_code"] == (
        "daily_outbound_limit_reached"
    )
    assert report["global_outbound_restrictions"]["sent_applications_today"] == 1


@pytest.mark.parametrize("database_kind", ["missing", "corrupt", "missing_tables"])
def test_missing_corrupt_or_unusable_database_is_unknown_never_clear(tmp_path, database_kind):
    inputs = _prepare_inputs(tmp_path, production_schema=database_kind != "missing_tables")
    database = inputs[0]
    if database_kind == "missing":
        database.unlink()
    elif database_kind == "corrupt":
        database.write_bytes(b"not a sqlite database")

    report = _run(inputs)

    assert report["read_only"] is False
    assert report["database"]["status"] == "UNKNOWN"
    assert all(
        employer["duplicate_suppression"]["status"] == "UNKNOWN"
        for employer in report["employers"].values()
    )


def test_no_screening_file_and_no_gmail_read_scope_are_explicit_limitations(tmp_path):
    inputs = _prepare_inputs(tmp_path)
    inputs[1].unlink()

    report = _run(inputs)

    assert report["employers"]["odbm"]["suppression"]["status"] == "UNKNOWN"
    assert report["employers"]["odbm"]["duplicate_suppression"]["status"] == "UNKNOWN"
    assert report["employers"]["tgcc"]["gmail_mailbox_history"]["status"] == "NOT_CHECKED"
    assert "gmail_readonly" in report["scope"]["gmail_mailbox_reason_code"]
    assert report["side_effects"]["gmail_provider"] is False


def test_history_preflight_can_be_scoped_to_allowlisted_employer(tmp_path):
    inputs = _prepare_inputs(tmp_path)

    report = _run(inputs, employer_ids=("megec",))

    assert set(report["employers"]) == {"megec"}
    route = report["employers"]["megec"]["portal"]
    assert route["official_route_url"] == (
        "https://www.megec.com/candidature-spontanee/"
    )
    assert route["wca_submission_support"] == (
        "SUPPORTED_EMAIL_ROUTE_PENDING_CURRENT_EVIDENCE"
    )
    assert "contact@megec.com" not in json.dumps(report).casefold()


def test_preflight_does_not_call_any_external_provider(tmp_path, monkeypatch):
    inputs = _prepare_inputs(tmp_path)

    def forbidden(*_args, **_kwargs):
        pytest.fail("history preflight must not call a provider")

    from app.agents import llm
    from app.connectors import tavily_resilience
    from app.email import provider
    from app.notifications import whatsapp

    for name in ("send", "create_draft", "reconcile_draft", "reconcile_live_send"):
        monkeypatch.setattr(provider, name, forbidden)
    monkeypatch.setattr(tavily_resilience.ResilientTavily, "search", forbidden, raising=False)
    monkeypatch.setattr(llm, "get_llm", forbidden)
    monkeypatch.setattr(whatsapp.MetaWhatsAppProvider, "send", forbidden, raising=False)

    report = _run(inputs)

    assert report["read_only"] is True
    assert all(value is False for key, value in report["side_effects"].items()
               if key in {"database_modified", "vault_modified", "messaging",
                          "gmail_provider", "discovery_or_llm_provider"})


def test_portal_readiness_lists_manual_fields_without_personal_values(tmp_path):
    inputs = _prepare_inputs(tmp_path)

    report = _run(inputs)

    odbm = report["employers"]["odbm"]["portal"]
    tgcc = report["employers"]["tgcc"]["portal"]
    assert odbm["status"] == "PREVIOUSLY_VERIFIED_NOT_RECHECKED"
    assert odbm["wca_submission_support"] == "UNSUPPORTED"
    assert "consent" in odbm["required_fields"]
    assert "email" in odbm["missing_applicant_inputs"]
    assert tgcc["candidate_preference_review"] == "REVIEW_REQUIRED"
    assert "mobility_choice" in tgcc["missing_applicant_inputs"]
    assert report["candidate_preference_review"]["conflict_reasons"] == [
        "morocco_not_in_target_countries",
        "sponsorship_required",
    ]
    serialized = json.dumps(report)
    assert "Candidate" not in serialized
    assert "+212600000000" not in serialized


def test_cli_history_preflight_does_not_initialize_database(tmp_path, monkeypatch, capsys):
    inputs = _prepare_inputs(tmp_path)
    args = [
        "history-preflight",
        "--database", str(inputs[0]),
        "--screening-state", str(inputs[1]),
        "--profile", str(inputs[2]),
        "--preferences", str(inputs[3]),
        "--restore-status", str(inputs[4]),
    ]
    from app import cli

    monkeypatch.setattr(cli, "init_db", lambda: pytest.fail("must not call init_db"))
    cli.main(args)
    report = json.loads(capsys.readouterr().out)
    assert report["workflow"] == "history_preflight"
    assert report["read_only"] is True


def test_github_workflow_skips_providers_notifications_and_vault_persistence():
    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "agent.yml")
        .read_text(encoding="utf-8-sig")
    )
    steps = workflow["jobs"]["pipeline"]["steps"]
    by_name = {step.get("name"): step for step in steps}

    assert "history_preflight" in str(workflow)
    assert "secrets/" not in by_name[
        "Restore only read-only history-preflight inputs"
    ]["run"]
    for name in (
        "Restore candidate data, OAuth secrets, and state (private vault)",
        "Build .env from vault and secrets",
        "Reset ephemeral runtime state",
        "Send WhatsApp run status",
        "Send WhatsApp connection test",
        "Persist state back to vault",
        "Upload run logs",
    ):
        assert "history_preflight" in by_name[name]["if"]
    targeted_restore = by_name["Restore targeted BTP preflight inputs only"]
    assert "secrets/" not in targeted_restore["run"]
    assert "candidate/cv/" in targeted_restore["run"]
    assert "cp -a /tmp/wca-target-vault/candidate/cv/. candidate/cv/" in (
        targeted_restore["run"]
    )
    for name in (
        "Restore candidate data, OAuth secrets, and state (private vault)",
        "Build .env from vault and secrets",
        "Reset ephemeral runtime state",
        "Send WhatsApp run status",
        "Send WhatsApp connection test",
        "Persist state back to vault",
    ):
        assert "target_employer != 'none'" in by_name[name]["if"]
    assert by_name["Run selected workflow"]["if"].find("inputs.task == 'pipeline'") >= 0
    assert by_name["Run BTP spontaneous-application workflow"]["if"].find(
        "inputs.task == 'btp_outreach'",
    ) >= 0
    assert by_name["Upload sanitized history-preflight report"]["with"]["path"] == (
        "/tmp/history_preflight_report.json"
    )
    assert "--preflight-only" in by_name["Run BTP spontaneous-application workflow"]["run"]
