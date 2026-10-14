"""Crash-safe reservations in the private-vault Tavily budget ledgers."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import date
from pathlib import Path

_LEDGER_NAME = re.compile(r"tavily_budget_[a-f0-9]{8}\.json\Z")
_BRANCH = "refs/heads/main"
_REMOTE_BRANCH = "refs/remotes/origin/main"


class BudgetPersistenceError(RuntimeError):
    """A safe, content-free failure to persist a provider budget reservation."""


class GitVaultBudgetStore:
    """Atomically reserve one daily Tavily attempt with a normal Git push.

    Each reservation is a new commit parented on the latest vault main. The
    commit tree differs by exactly one validated budget-ledger path. A
    non-fast-forward race is retried against the new head; other failures fail
    closed before a provider request is sent.
    """

    def __init__(self, repository: Path, *, max_push_attempts: int = 5):
        self.repository = Path(repository)
        self.max_push_attempts = max(1, int(max_push_attempts))
        self.write_count = 0

    @staticmethod
    def _relative_ledger_path(path: Path) -> str:
        name = Path(path).name
        if not _LEDGER_NAME.fullmatch(name):
            raise BudgetPersistenceError("invalid_budget_ledger_path")
        return f"state/{name}"

    def _git(
        self,
        *args: str,
        input_data: bytes | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> bytes:
        process_env = os.environ.copy()
        process_env["GIT_TERMINAL_PROMPT"] = "0"
        if env:
            process_env.update(env)
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.repository,
                input=input_data,
                capture_output=True,
                env=process_env,
                check=False,
                timeout=30,
            )
        except OSError as exc:
            raise BudgetPersistenceError("vault_git_unavailable") from exc
        except subprocess.TimeoutExpired as exc:
            raise BudgetPersistenceError("vault_git_timeout") from exc
        if check and result.returncode:
            raise BudgetPersistenceError("vault_git_operation_failed")
        return result.stdout

    def _fetch_head(self) -> str:
        self._git(
            "fetch", "--no-tags", "origin",
            f"+{_BRANCH}:{_REMOTE_BRANCH}",
        )
        return self._git("rev-parse", _REMOTE_BRANCH).decode("ascii").strip()

    @staticmethod
    def _decode_ledger(payload: bytes) -> dict:
        try:
            state = json.loads(payload)
            if (
                not isinstance(state, dict)
                or not isinstance(state.get("date"), str)
                or not isinstance(state.get("count"), int)
                or isinstance(state.get("count"), bool)
                or state["count"] < 0
                or not isinstance(state.get("exhausted"), bool)
            ):
                raise ValueError
            date.fromisoformat(state["date"])
            return {
                "date": state["date"],
                "count": state["count"],
                "exhausted": state["exhausted"],
            }
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise BudgetPersistenceError("invalid_remote_budget_ledger") from exc

    def _remote_state(self, head: str, ledger_path: str) -> dict:
        payload = self._git("show", f"{head}:{ledger_path}")
        return self._decode_ledger(payload)

    def synchronize(self, path: Path, *, today: str | None = None) -> dict:
        """Refresh one ephemeral ledger copy from the durable private vault."""
        ledger_path = self._relative_ledger_path(path)
        head = self._fetch_head()
        state = self._remote_state(head, ledger_path)
        if today is not None and state["date"] > today:
            raise BudgetPersistenceError("remote_budget_date_is_in_future")
        local_path = Path(path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(
            json.dumps(state, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return state

    def _commit_ledger(self, head: str, ledger_path: str, state: dict) -> None:
        payload = (
            json.dumps(state, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")
        git_dir = Path(self._git("rev-parse", "--absolute-git-dir").decode().strip())
        with tempfile.NamedTemporaryFile(
            prefix="budget-index-", dir=git_dir, delete=False,
        ) as index_file:
            index_path = Path(index_file.name)
        index_path.unlink(missing_ok=True)
        index_env = {"GIT_INDEX_FILE": str(index_path)}
        try:
            self._git("read-tree", head, env=index_env)
            blob = self._git("hash-object", "-w", "--stdin", input_data=payload).decode().strip()
            self._git(
                "update-index", "--add", "--cacheinfo",
                f"100644,{blob},{ledger_path}", env=index_env,
            )
            tree = self._git("write-tree", env=index_env).decode().strip()
            changed_paths = self._git(
                "diff-tree", "--no-commit-id", "--name-only", "-r",
                head, tree,
            ).decode("utf-8").splitlines()
            if changed_paths != [ledger_path]:
                raise BudgetPersistenceError("vault_budget_write_scope_violation")
            identity = {
                "GIT_AUTHOR_NAME": "WCA Budget Reservations",
                "GIT_AUTHOR_EMAIL": "wca-budget-reservations@users.noreply.github.com",
                "GIT_COMMITTER_NAME": "WCA Budget Reservations",
                "GIT_COMMITTER_EMAIL": "wca-budget-reservations@users.noreply.github.com",
            }
            commit = self._git(
                "commit-tree", tree, "-p", head,
                "-m", "state: reserve Tavily daily budget",
                env=identity,
            ).decode("ascii").strip()
            self._git(
                "push", "--porcelain", "origin", f"{commit}:{_BRANCH}",
            )
        finally:
            index_path.unlink(missing_ok=True)

    def _update(self, path: Path, today: str, limit: int, *, exhaust: bool) -> dict:
        ledger_path = self._relative_ledger_path(path)
        try:
            requested_date = date.fromisoformat(today).isoformat()
            if requested_date != today:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise BudgetPersistenceError("invalid_budget_date") from exc

        for _attempt in range(self.max_push_attempts):
            head = self._fetch_head()
            state = self._remote_state(head, ledger_path)
            if state["date"] > today:
                raise BudgetPersistenceError("remote_budget_date_is_in_future")
            if state["date"] != today:
                state = {"date": today, "count": 0, "exhausted": False}

            if exhaust:
                state["exhausted"] = True
            elif state["exhausted"] or state["count"] >= limit:
                return {
                    **state,
                    "reserved": False,
                    "reason": "daily_request_budget_exhausted",
                }
            else:
                state["count"] += 1

            try:
                self._commit_ledger(head, ledger_path, state)
                self.write_count += 1
                return {**state, "reserved": not exhaust, "reason": ""}
            except BudgetPersistenceError:
                try:
                    current = self._fetch_head()
                except BudgetPersistenceError:
                    raise
                if current == head:
                    raise
        raise BudgetPersistenceError("vault_budget_push_race_limit")

    def reserve(self, path: Path, today: str, limit: int) -> dict:
        """Persist and return a reservation before the provider is dispatched."""
        return self._update(path, today, limit, exhaust=False)

    def exhaust(self, path: Path, today: str, limit: int) -> dict:
        """Durably mark a key exhausted after a provider quota response."""
        return self._update(path, today, limit, exhaust=True)
