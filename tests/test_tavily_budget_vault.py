from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

import pytest

from app.connectors.tavily_resilience import (
    BTP_TAVILY_DAILY_RESERVE,
    GENERAL_TAVILY_DAILY_LIMIT,
    TAVILY_DAILY_REQUEST_LIMIT,
    DailyBudget,
    key_fingerprint,
)
from app.workflows.tavily_budget_vault import (
    BudgetPersistenceError,
    GitVaultBudgetStore,
)


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _vault_repositories(
    tmp_path: Path, *, initial_count: int = 0, legacy_initial_ledger: bool = False,
):
    bare = tmp_path / "vault.git"
    seed = tmp_path / "seed"
    _git("init", "--bare", "--initial-branch=main", str(bare))
    seed.mkdir()
    _git("init", "--initial-branch=main", cwd=seed)
    _git("config", "user.name", "Test", cwd=seed)
    _git("config", "user.email", "test@example.invalid", cwd=seed)
    (seed / "state").mkdir()
    ledger = seed / "state" / "tavily_budget_0123abcd.json"
    initial_ledger = {
        "date": datetime.now(timezone.utc).date().isoformat(),
        "count": initial_count,
        "exhausted": False,
    }
    if not legacy_initial_ledger:
        initial_ledger["usage_by_type"] = {
            "general": min(initial_count, GENERAL_TAVILY_DAILY_LIMIT),
            "btp": max(0, initial_count - GENERAL_TAVILY_DAILY_LIMIT),
            "unattributed": 0,
        }
    ledger.write_text(json.dumps(initial_ledger), encoding="utf-8")
    (seed / "state" / "agent.db").write_bytes(b"private-db-placeholder")
    (seed / "candidate.yaml").write_text("private candidate placeholder", encoding="utf-8")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "initial state", cwd=seed)
    _git("remote", "add", "origin", str(bare), cwd=seed)
    _git("push", "origin", "main", cwd=seed)

    clones = []
    for number in (1, 2):
        clone = tmp_path / f"clone-{number}"
        _git("clone", "--quiet", str(bare), str(clone))
        clones.append(clone)
    return bare, ledger.relative_to(seed).as_posix(), clones


def _remote_ledger(bare: Path, relative_path: str) -> dict:
    content = _git(
        "--git-dir", str(bare), "show", f"refs/heads/main:{relative_path}",
    )
    return json.loads(content)


def test_durable_reservation_is_visible_before_provider_request(tmp_path, monkeypatch):
    bare, relative_path, (clone, _) = _vault_repositories(tmp_path)
    store = GitVaultBudgetStore(clone)
    budget_path = tmp_path / "runtime" / Path(relative_path).name
    budget_path.parent.mkdir()
    budget_path.write_text(json.dumps({
        "date": datetime.now(timezone.utc).date().isoformat(),
        "count": 0,
        "exhausted": False,
        "usage_by_type": {"general": 0, "btp": 0, "unattributed": 0},
    }), encoding="utf-8")
    budget = DailyBudget(
        budget_path,
        limit=GENERAL_TAVILY_DAILY_LIMIT,
        durable_reserver=store.reserve,
        durable_exhauster=store.exhaust,
    )

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        @staticmethod
        def json():
            return {"results": []}

    observed_counts = []

    def provider_request(*_args, **_kwargs):
        observed_counts.append(_remote_ledger(bare, relative_path)["count"])
        return Response()

    monkeypatch.setattr(
        "app.connectors.tavily_resilience.requests.post", provider_request,
    )
    from app.connectors.tavily_resilience import ResilientTavily, TavilyCache, TavilyKey

    source = ResilientTavily(
        keys=[TavilyKey(
            "test-api-key", budget, key_fingerprint("test-api-key"),
        )],
        cache=TavilyCache(tmp_path / "cache.json"),
    )

    import asyncio

    assert asyncio.run(source.search("civil", "Morocco")) == []
    assert observed_counts == [1]
    assert _remote_ledger(bare, relative_path)["count"] == 1
    assert store.write_count == 1
    assert store.reservation_count == 1


def test_failed_durable_reservation_sends_no_provider_request(tmp_path, monkeypatch):
    budget = DailyBudget(
        tmp_path / "budget.json",
        limit=GENERAL_TAVILY_DAILY_LIMIT,
        durable_reserver=lambda *_args: (_ for _ in ()).throw(
            BudgetPersistenceError("safe internal error"),
        ),
    )
    requests = []
    monkeypatch.setattr(
        "app.connectors.tavily_resilience.requests.post",
        lambda *_args, **_kwargs: requests.append(1),
    )
    from app.connectors.tavily_resilience import (
        ApiAttemptBudget,
        ResilientTavily,
        TavilyCache,
        TavilyKey,
    )

    run_budget = ApiAttemptBudget(
        8,
        country_limits={"Morocco": 6, "France": 1, "Belgium": 1},
    )
    source = ResilientTavily(
        keys=[TavilyKey("key", budget, key_fingerprint("key"))],
        cache=TavilyCache(tmp_path / "cache.json"),
        attempt_budget=run_budget,
    )

    import asyncio

    assert asyncio.run(source.search(
        "civil", "Morocco", country="Morocco",
    )) == []
    assert requests == []
    assert source.last_error_code == "provider_budget_persistence_unavailable"
    assert run_budget.as_dict()["attempts"] == 0
    assert (
        run_budget.as_dict()["country_attempt_allocation"]["morocco"]["attempts"]
        == 0
    )


def test_malformed_durable_reservation_response_fails_closed(tmp_path, monkeypatch):
    today = datetime.now(timezone.utc).date().isoformat()
    budget = DailyBudget(
        tmp_path / "budget.json",
        limit=GENERAL_TAVILY_DAILY_LIMIT,
        durable_reserver=lambda *_args: {
            "reserved": True,
            "date": today,
            "count": 0,
            "exhausted": False,
        },
    )
    requests = []
    monkeypatch.setattr(
        "app.connectors.tavily_resilience.requests.post",
        lambda *_args, **_kwargs: requests.append(1),
    )
    from app.connectors.tavily_resilience import (
        ApiAttemptBudget,
        ResilientTavily,
        TavilyCache,
        TavilyKey,
    )

    run_budget = ApiAttemptBudget(8)
    source = ResilientTavily(
        keys=[TavilyKey("key", budget, key_fingerprint("key"))],
        cache=TavilyCache(tmp_path / "cache.json"),
        attempt_budget=run_budget,
    )

    import asyncio

    assert asyncio.run(source.search("civil", "Morocco")) == []
    assert requests == []
    assert source.last_error_code == "provider_budget_persistence_unavailable"
    assert run_budget.as_dict()["attempts"] == 0


def test_crash_after_durable_reservation_preserves_allowance_for_next_run(tmp_path):
    bare, relative_path, (first_clone, second_clone) = _vault_repositories(tmp_path)
    today = datetime.now(timezone.utc).date().isoformat()
    first_store = GitVaultBudgetStore(first_clone)
    local_first = tmp_path / "first" / Path(relative_path).name
    local_first.parent.mkdir()
    local_first.write_text(json.dumps({
        "date": today, "count": 0, "exhausted": False,
        "usage_by_type": {"general": 0, "btp": 0, "unattributed": 0},
    }), encoding="utf-8")

    reservation = first_store.reserve(local_first, today, 2)

    assert reservation["reserved"] is True
    assert _remote_ledger(bare, relative_path)["count"] == 1

    second_store = GitVaultBudgetStore(second_clone)
    local_second = tmp_path / "second" / Path(relative_path).name
    second_store.synchronize(local_second, today=today)
    second_budget = DailyBudget(local_second, limit=2)

    assert second_budget.remaining() == 1
    assert json.loads(local_second.read_text(encoding="utf-8"))["count"] == 1


def test_concurrent_reservations_retry_stale_push_without_losing_counter(tmp_path):
    bare, relative_path, clones = _vault_repositories(tmp_path)
    barrier = Barrier(2)

    class BarrierStore(GitVaultBudgetStore):
        def __init__(self, repository):
            super().__init__(repository)
            self.first_fetch = True

        def _fetch_head(self):
            head = super()._fetch_head()
            if self.first_fetch:
                self.first_fetch = False
                barrier.wait(timeout=10)
            return head

    today = datetime.now(timezone.utc).date().isoformat()
    stores = [BarrierStore(clone) for clone in clones]
    local_paths = [
        tmp_path / f"runtime-{index}" / Path(relative_path).name
        for index in range(2)
    ]
    for local_path in local_paths:
        local_path.parent.mkdir()

    with ThreadPoolExecutor(max_workers=2) as executor:
        reservations = list(executor.map(
            lambda pair: pair[0].reserve(pair[1], today, 2),
            zip(stores, local_paths, strict=True),
        ))

    assert all(result["reserved"] for result in reservations)
    assert _remote_ledger(bare, relative_path)["count"] == 2


def test_durable_reservation_writes_only_target_ledger_and_preserves_btp_reserve(
    tmp_path,
):
    bare, relative_path, (clone, _) = _vault_repositories(
        tmp_path, initial_count=GENERAL_TAVILY_DAILY_LIMIT - 1,
    )
    today = datetime.now(timezone.utc).date().isoformat()
    local_path = tmp_path / "runtime" / Path(relative_path).name
    local_path.parent.mkdir()
    store = GitVaultBudgetStore(clone)

    result = store.reserve(local_path, today, GENERAL_TAVILY_DAILY_LIMIT)
    changed_files = _git(
        "--git-dir", str(bare), "diff-tree", "--no-commit-id",
        "--name-only", "-r", "refs/heads/main^", "refs/heads/main",
    ).splitlines()

    assert result["reserved"] is True
    assert changed_files == [relative_path]
    assert _remote_ledger(bare, relative_path)["count"] == GENERAL_TAVILY_DAILY_LIMIT
    assert (
        GENERAL_TAVILY_DAILY_LIMIT + BTP_TAVILY_DAILY_RESERVE
        == 20
    )


def test_durable_general_reservation_cannot_spend_btp_reserve(tmp_path):
    bare, relative_path, (clone, _) = _vault_repositories(
        tmp_path, initial_count=GENERAL_TAVILY_DAILY_LIMIT - 1,
    )
    today = datetime.now(timezone.utc).date().isoformat()
    local_path = tmp_path / "runtime" / Path(relative_path).name
    local_path.parent.mkdir()
    store = GitVaultBudgetStore(clone)

    first = store.reserve_general(local_path, today, TAVILY_DAILY_REQUEST_LIMIT)
    second = store.reserve_general(local_path, today, TAVILY_DAILY_REQUEST_LIMIT)

    assert first["reserved"] is True
    assert first["count"] == GENERAL_TAVILY_DAILY_LIMIT
    assert second["reserved"] is False
    assert second["reason"] == "daily_request_budget_exhausted"
    assert _remote_ledger(bare, relative_path)["count"] == (
        GENERAL_TAVILY_DAILY_LIMIT
    )


def test_legitimate_btp_reservation_is_attributed_and_preserves_general_cap(
    tmp_path,
):
    bare, relative_path, (clone, _) = _vault_repositories(
        tmp_path, initial_count=GENERAL_TAVILY_DAILY_LIMIT,
    )
    today = datetime.now(timezone.utc).date().isoformat()
    local_path = tmp_path / "runtime" / Path(relative_path).name
    local_path.parent.mkdir()
    store = GitVaultBudgetStore(clone)

    reservation = store.reserve_btp(local_path, today, TAVILY_DAILY_REQUEST_LIMIT)
    blocked_general = store.reserve_general(
        local_path, today, TAVILY_DAILY_REQUEST_LIMIT,
    )
    ledger = _remote_ledger(bare, relative_path)

    assert reservation["reserved"] is True
    assert ledger["count"] == GENERAL_TAVILY_DAILY_LIMIT + 1
    assert ledger["usage_by_type"] == {
        "general": GENERAL_TAVILY_DAILY_LIMIT,
        "btp": 1,
        "unattributed": 0,
    }
    assert blocked_general["reserved"] is False
    assert ledger["count"] == GENERAL_TAVILY_DAILY_LIMIT + 1


def test_unknown_legacy_provenance_blocks_without_persisting_reservation(tmp_path):
    bare, relative_path, (clone, _) = _vault_repositories(
        tmp_path, initial_count=18, legacy_initial_ledger=True,
    )
    today = datetime.now(timezone.utc).date().isoformat()
    local_path = tmp_path / "runtime" / Path(relative_path).name
    local_path.parent.mkdir()
    store = GitVaultBudgetStore(clone)
    before = _remote_ledger(bare, relative_path)

    result = store.reserve_general(local_path, today, TAVILY_DAILY_REQUEST_LIMIT)

    assert result["reserved"] is False
    assert result["reason"] == "provider_budget_provenance_unavailable"
    assert _remote_ledger(bare, relative_path) == before
    assert store.write_count == 0


def test_unknown_provenance_does_not_dispatch_provider_request(tmp_path, monkeypatch):
    bare, relative_path, (clone, _) = _vault_repositories(
        tmp_path, initial_count=18, legacy_initial_ledger=True,
    )
    today = datetime.now(timezone.utc).date().isoformat()
    local_path = tmp_path / "runtime" / Path(relative_path).name
    local_path.parent.mkdir()
    local_path.write_text(json.dumps({
        "date": today,
        "count": 18,
        "exhausted": False,
    }), encoding="utf-8")
    store = GitVaultBudgetStore(clone)
    budget = DailyBudget(
        local_path,
        limit=TAVILY_DAILY_REQUEST_LIMIT,
        durable_reserver=store.reserve_general,
        reservation_type="general",
    )
    requests = []
    monkeypatch.setattr(
        "app.connectors.tavily_resilience.requests.post",
        lambda *_args, **_kwargs: requests.append(True),
    )
    from app.connectors.tavily_resilience import (
        ResilientTavily,
        TavilyCache,
        TavilyKey,
    )

    source = ResilientTavily(
        keys=[TavilyKey(
            "test-api-key", budget, key_fingerprint("test-api-key"),
        )],
        cache=TavilyCache(tmp_path / "cache.json"),
    )

    import asyncio

    assert asyncio.run(source.search("civil engineering", "Morocco")) == []
    assert requests == []
    assert source.last_error_code == "provider_budget_provenance_unavailable"
    assert _remote_ledger(bare, relative_path)["count"] == 18
    assert store.write_count == 0


def test_known_general_overrun_is_never_reclassified_as_btp_usage(tmp_path):
    bare, relative_path, (clone, _) = _vault_repositories(tmp_path)
    today = datetime.now(timezone.utc).date().isoformat()
    ledger = clone / relative_path
    ledger.write_text(json.dumps({
        "date": today,
        "count": 18,
        "exhausted": False,
        "usage_by_type": {"general": 18, "btp": 0, "unattributed": 0},
    }), encoding="utf-8")
    _git("add", relative_path, cwd=clone)
    _git("commit", "-m", "fixture general overrun", cwd=clone)
    _git("push", "origin", "main", cwd=clone)
    store = GitVaultBudgetStore(clone)
    before = _remote_ledger(bare, relative_path)

    result = store.reserve_general(
        ledger, today, TAVILY_DAILY_REQUEST_LIMIT,
    )

    assert result["reserved"] is False
    assert result["reason"] == "daily_request_budget_exhausted"
    assert _remote_ledger(bare, relative_path) == before


@pytest.mark.parametrize("initial_count", [17, 20])
def test_durable_general_reservation_skips_keys_without_general_capacity(
    tmp_path, initial_count,
):
    bare, relative_path, (clone, _) = _vault_repositories(
        tmp_path, initial_count=initial_count,
    )
    today = datetime.now(timezone.utc).date().isoformat()
    local_path = tmp_path / "runtime" / Path(relative_path).name
    local_path.parent.mkdir()
    store = GitVaultBudgetStore(clone)

    result = store.reserve_general(local_path, today, TAVILY_DAILY_REQUEST_LIMIT)

    assert result["reserved"] is False
    assert _remote_ledger(bare, relative_path)["count"] == initial_count


def test_durable_exhaustion_preserves_count_and_blocks_later_reservations(tmp_path):
    bare, relative_path, (clone, _) = _vault_repositories(tmp_path, initial_count=4)
    today = datetime.now(timezone.utc).date().isoformat()
    local_path = tmp_path / "runtime" / Path(relative_path).name
    local_path.parent.mkdir()
    store = GitVaultBudgetStore(clone)

    exhausted = store.exhaust(local_path, today, 17)
    blocked = store.reserve(local_path, today, 17)
    persisted = _remote_ledger(bare, relative_path)

    assert exhausted["exhausted"] is True
    assert blocked["reserved"] is False
    assert persisted == {
        "date": today,
        "count": 4,
        "exhausted": True,
        "usage_by_type": {"general": 4, "btp": 0, "unattributed": 0},
    }


def test_stale_date_rolls_only_at_utc_day_boundary(tmp_path):
    bare, relative_path, (clone, _) = _vault_repositories(tmp_path, initial_count=8)
    store = GitVaultBudgetStore(clone)
    local_path = tmp_path / "runtime" / Path(relative_path).name
    local_path.parent.mkdir()
    tomorrow = (
        datetime.now(timezone.utc).date().fromordinal(
            datetime.now(timezone.utc).date().toordinal() + 1,
        ).isoformat()
    )

    result = store.reserve(local_path, tomorrow, 17)

    assert result["date"] == tomorrow
    assert result["count"] == 1
    assert _remote_ledger(bare, relative_path) == {
        "date": tomorrow,
        "count": 1,
        "exhausted": False,
        "usage_by_type": {"general": 0, "btp": 0, "unattributed": 1},
    }


def test_discovery_rollover_resets_stale_ledger_only_at_reservation_boundary(
    tmp_path,
):
    bare, relative_path, (clone, _) = _vault_repositories(tmp_path, initial_count=20)
    store = GitVaultBudgetStore(clone)
    local_path = tmp_path / "runtime" / Path(relative_path).name
    local_path.parent.mkdir()
    today = datetime.now(timezone.utc).date().isoformat()
    next_day = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    store.synchronize(local_path, today=today)
    before = _remote_ledger(bare, relative_path)

    reservation = store.reserve_general(local_path, next_day, 20)

    assert before["date"] == today
    assert before["count"] == 20
    assert reservation["reserved"] is True
    assert reservation["date"] == next_day
    assert reservation["count"] == 1
    assert _remote_ledger(bare, relative_path) == {
        "date": next_day,
        "count": 1,
        "exhausted": False,
        "usage_by_type": {"general": 1, "btp": 0, "unattributed": 0},
    }
