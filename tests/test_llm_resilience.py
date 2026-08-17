"""Tests for the LLM resilience layer (cache, daily budget, fallback).

All tests use fake providers, so they never hit the network or quota.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agents.llm_resilience import DailyBudget, LLMCache, _cache_key

SYSTEM = "JOB_ANALYST"
USER = "some posting text"


class _Stub:
    name = "stub"

    def __init__(self, *, fail: Exception | None = None, answer: dict | None = None):
        self.calls = 0
        self._fail = fail
        self._answer = answer or {"key": "primary_value"}

    async def complete_json(self, system: str, user: str) -> dict:
        self.calls += 1
        if self._fail:
            raise self._fail
        return self._answer


def _run(coro):
    return asyncio.run(coro)


def _make(cache_path, budget_path, budget_limit=20, primary=None, fallback=None):
    from app.agents.llm_resilience import DailyBudget, LLMCache, ResilientLLM

    cache = LLMCache(cache_path)
    budget = DailyBudget(budget_path, budget_limit)
    resume = ResilientLLM(
        fallback=fallback or _Stub(answer={"key": "fallback_value"}),
        cache=cache,
        primary=primary or _Stub(),
        budget=budget,
    )
    return resume, resume.primaries[0].llm, resume.fallback


def test_cache_primary(result_llm, tmp_path):
    """A cached response is returned without calling the primary again."""
    key = _cache_key("stub", SYSTEM, USER)
    result_llm.cache.put(key, {"key": "cached_value"})
    out = _run(result_llm.complete_json(SYSTEM, USER))
    assert out == {"key": "cached_value"}
    assert result_llm.primaries[0].llm.calls == 0


def test_primary_success_populates_cache_and_budget(tmp_path):
    resume, primary, _ = _make(tmp_path / "c.json", tmp_path / "b.json")
    out = _run(resume.complete_json(SYSTEM, USER))
    assert out == {"key": "primary_value"}
    assert primary.calls == 1
    assert resume.primaries[0].budget.remaining() == 19
    key = _cache_key("stub", SYSTEM, USER)
    assert resume.cache.get(key) == {"key": "primary_value"}


def test_primary_failure_falls_back(tmp_path):
    err = RuntimeError("connection reset")
    resume, primary, fallback = _make(tmp_path / "c.json", tmp_path / "b.json", primary=_Stub(fail=err))
    out = _run(resume.complete_json(SYSTEM, USER))
    assert out == {"key": "fallback_value"}
    assert primary.calls == 1
    assert fallback.calls == 1
    # budget was NOT charged for a fallback (no successful primary call)
    assert resume.primaries[0].budget.remaining() == 20


def test_budget_exhaustion_skips_primary(tmp_path):
    resume, primary, fallback = _make(tmp_path / "c.json", tmp_path / "b.json", budget_limit=1)
    resume.primaries[0].budget.record()  # exhaust today's single call
    out = _run(resume.complete_json(SYSTEM, USER))
    assert out == {"key": "fallback_value"}
    assert primary.calls == 0
    assert fallback.calls == 1


def test_quota_error_exhausts_budget_fast(tmp_path):
    """A 429 makes the rest of today fall back without retrying the network."""
    err = RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")
    resume, primary, fallback = _make(tmp_path / "c.json", tmp_path / "b.json",
                                      budget_limit=20, primary=_Stub(fail=err))
    out = _run(resume.complete_json(SYSTEM, USER))
    assert out == {"key": "fallback_value"}
    assert resume.primaries[0].budget.remaining() == 0  # exhausted for the day

    # second call: budget already spent, primary must not be called again
    again = _run(resume.complete_json(SYSTEM, USER))
    assert again == {"key": "fallback_value"}
    assert primary.calls == 1


def test_cache_persists_across_instances(tmp_path):
    p1 = tmp_path / "c2.json"
    key = _cache_key("stub", SYSTEM, USER)
    first = LLMCache(p1)
    first.put(key, {"key": "persisted"})

    second = LLMCache(p1)
    assert second.get(key) == {"key": "persisted"}


def _make_ring(path, keys, budget_limit=20):
    """Build a ResilientLLM over a list of (_Stub, answer|fail, budget_file)."""
    from app.agents.llm_resilience import DailyBudget, LLMCache, PrimaryKey, ResilientLLM

    fallback = _Stub(answer={"key": "fallback_value"})
    primaries = [
        PrimaryKey(llm=stub, budget=DailyBudget(path / f"b_{i}.json", budget_limit))
        for i, (stub, _) in enumerate(keys)
    ]
    resume = ResilientLLM(fallback=fallback, cache=LLMCache(path / "c.json"), primaries=primaries)
    return resume, [s for s, _ in keys], fallback


def test_quota_on_first_key_rotates_to_second(tmp_path):
    """Key 1 hits 429 -> its budget is exhausted and key 2 answers the call."""
    err = RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")
    key1 = (_Stub(fail=err), None)
    key2 = (_Stub(answer={"key": "key2_value"}), None)
    resume, stubs, fallback = _make_ring(tmp_path, [key1, key2])

    out = _run(resume.complete_json(SYSTEM, USER))

    assert out == {"key": "key2_value"}
    assert stubs[0].calls == 1
    assert stubs[1].calls == 1
    assert fallback.calls == 0
    # key 1 exhausted for the day, key 2 still has budget
    assert resume.primaries[0].budget.remaining() == 0
    assert resume.primaries[1].budget.remaining() == 19


def test_all_keys_spent_uses_fallback(tmp_path):
    """Every key exhausted -> deterministic fallback, no key is called."""
    k1 = (_Stub(), None)
    k2 = (_Stub(), None)
    resume, stubs, fallback = _make_ring(tmp_path, [k1, k2], budget_limit=1)
    resume.primaries[0].budget.record()
    resume.primaries[1].budget.record()

    out = _run(resume.complete_json(SYSTEM, USER))

    assert out == {"key": "fallback_value"}
    assert stubs[0].calls == 0
    assert stubs[1].calls == 0
    assert fallback.calls == 1


def test_key_fingerprint_is_non_reversible_and_distinct():
    from app.agents.llm_resilience import key_fingerprint

    a = key_fingerprint("secret-key-A")
    b = key_fingerprint("secret-key-B")
    assert a != b
    assert "secret-key-A" not in a
    assert len(a) == 8


@pytest.fixture()
def result_llm(tmp_path):
    resume, _, _ = _make(tmp_path / "c1.json", tmp_path / "b1.json")
    return resume