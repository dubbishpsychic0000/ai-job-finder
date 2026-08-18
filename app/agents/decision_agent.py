"""Decision Agent — hard rules FIRST, then AI on top (critique point: rules + AI).

Pipeline per job:
  1) deterministic guards (already applied, stale post, impossible experience)
  2) opportunity band from overall score (config thresholds)
  3) LLM final call given band + context (may downgrade, rarely upgrades)
  4) the result is recorded in `decisions` and drives the action engine.

Return values: APPLY | ASK_EMPLOYER | INVESTIGATE | HOLD | IGNORE
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import memory as mem
from app.agents.llm import LLMProvider
from app.config import AgentConfig

logger = logging.getLogger(__name__)

_DECISION_SYSTEM = """You are the decision engine of a job-search agent.
Given the opportunity score band, candidate context and job signals, choose ONE action:
APPLY | ASK_EMPLOYER | INVESTIGATE | HOLD | IGNORE
Return ONLY JSON: {"decision": "...", "confidence": 0-1, "reason": "short reason"}
Rules of thumb:
- APPLY: high band and a verifiable contact channel.
- ASK_EMPLOYER: good fit but the posting lacks key info (sponsorship, remote/relocation).
- INVESTIGATE: unclear fit or a missing application channel — research first, do not email yet.
- HOLD: decent but under thresholds; keep for later.
- IGNORE: low band or a blocking rule fired.
DECISION_AGENT"""


class DecisionResult:
    def __init__(self, decision: str, overall: float, dimensions: dict, reason: str,
                 rules_fired: list[str], ai_reason: str = ""):
        self.decision = decision
        self.overall = overall
        self.dimensions = dimensions
        self.reason = reason
        self.rules_fired = rules_fired
        self.ai_reason = ai_reason


class DecisionAgent:
    def __init__(self, llm: LLMProvider, config: AgentConfig, profile=None):
        self.llm = llm
        self.config = config
        self.profile = profile

    def hard_rules(self, session: Session, job, overall: float, band: str) -> tuple[str | None, list[str]]:
        """Return (blocking_decision, fired_rules). None means no rule blocked."""
        fired: list[str] = []
        rules = self.config.rules

        if job.status in ("acted", "closed", "ignored"):
            return "IGNORE", ["already_processed"]
        if mem.store.find_job_by_key(session, job.dedup_key) and job.status == "new":
            pass  # duplicate guard lives in discovery; keep decision pure

        if overall < rules.get("ignore_below", 50):
            fired.append("low_score")

        if job.posted_at:
            max_age = rules.get("max_days_since_posted", 45)
            posted = job.posted_at
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - posted).days
            if age_days > max_age:
                fired.append(f"stale_posting_{age_days}d")

        # experience requirement vs candidate ceiling
        analysis = mem.store.get_analysis(session, job.id)
        if analysis and analysis.experience_max:
            buffer = rules.get("tolerance_years_buffer", 2)
            # Use the candidate injected into this run. Reading the live YAML
            # here made decisions depend on an unrelated local profile.
            if self.profile is not None:
                max_allowed = self.profile.experience_years + buffer
            else:  # backwards-compatible direct construction
                from app.config import get_profile
                max_allowed = get_profile().experience_years + buffer
            if analysis.experience_max > max_allowed:
                fired.append(f"experience_requires_{analysis.experience_max}y")

        if fired and "low_score" in fired:
            return "IGNORE", fired
        if any(rule.startswith("stale_posting_") or rule.startswith("experience_requires_")
               for rule in fired):
            return "IGNORE", fired
        return None, fired

    async def decide(self, session: Session, job, overall: float, dimensions: dict[str, float],
                     band: str, context: dict | None = None) -> DecisionResult:
        blocking, rules_fired = self.hard_rules(session, job, overall, band)
        if blocking:
            return DecisionResult(blocking, overall, dimensions,
                                  reason=f"hard rule blocked: {', '.join(rules_fired)}",
                                  rules_fired=rules_fired)

        context = context or {}
        payload = {
            "band": band,
            "overall_score": overall,
            "contact_email": bool(job.contact_email),
            "sponsorship_ok": context.get("sponsorship_ok", True),
            "has_analysis": bool(mem.store.get_analysis(session, job.id)),
            "job_country": job.country,
            "rules_fired": rules_fired,
        }
        result = await self.llm.complete_json(_DECISION_SYSTEM, json.dumps(payload, ensure_ascii=False))
        ai_decision = (result or {}).get("decision", "INVESTIGATE")
        ai_decision = str(ai_decision).upper().replace("-", "_")
        if ai_decision not in ("APPLY", "ASK_EMPLOYER", "INVESTIGATE", "HOLD", "IGNORE"):
            ai_decision = "INVESTIGATE"

        # Deterministic auto-apply guard: never APPLY below policy score &
        # confidence, no matter what the model says.
        min_score = float(self.config.rules.get("min_application_score", 80))
        min_conf = float(self.config.rules.get("min_application_confidence", 0.80))
        confidence = round(min(0.95, 0.5 + (overall / 100.0) * 0.5), 2)
        guard_hit = None
        if ai_decision == "APPLY" and (overall < min_score or confidence < min_conf):
            guard_hit = f"auto-apply guard: score {overall:.0f}, conf {confidence:.2f} " \
                        f"(policy >= {min_score:.0f} / {min_conf:.2f})"
            ai_decision = "ASK_EMPLOYER" if (overall >= 70 and job.contact_email) else "INVESTIGATE"

        return DecisionResult(
            decision=ai_decision,
            overall=overall,
            dimensions=dimensions,
            reason=f"score {overall:.0f} -> {band} band; AI: {ai_decision}" +
                   (f"; {guard_hit}" if guard_hit else ""),
            rules_fired=rules_fired,
            ai_reason=(result or {}).get("reason", ""),
        )
