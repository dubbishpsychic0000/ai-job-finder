"""Candidate Matcher — compares the job analysis against the candidate profile.

Returns sub-scores in the 0..100 scale. The deterministic weighted aggregation
happens in app/scoring/engine.py, NOT here — this agent only produces the
component scores (see critique point #1: scores are engineered outputs).
"""
from __future__ import annotations

import json

from app.agents.llm import LLMProvider

_MATCHER_SYSTEM = """You compare a candidate profile against a job's requirements.
Return ONLY a JSON object with numeric sub-scores 0-100:
{"qualification": int, "career": int, "language": int, "location": int,
 "immigration_fit": int}
Here qualification = skill & education fit, career = relevance to target roles,
language = language fit, location = country fit & relocatability,
immigration_fit = work-authorization/sponsorship openness.
Be strict: a missing requirement is a penalty, not a guess.
CANDIDATE_MATCHER"""


class CandidateMatcher:
    def __init__(self, llm: LLMProvider, profile):
        self.llm = llm
        self.profile = profile

    async def match(self, job, analysis: dict) -> dict[str, float]:
        payload = {
            "candidate_skills": self.profile.skills,
            "candidate_languages": list(self.profile.languages),
            "experience_years": self.profile.experience_years,
            "relocation_willing": bool((self.profile.relocation or {}).get("willing")),
            "target_countries": [],  # filled by caller if needed
            "job_skills": analysis.get("skills_required") or [],
            "job_languages": analysis.get("languages") or [],
            "job_experience_min": analysis.get("experience_min"),
            "sponsorship_mentioned": analysis.get("sponsorship_mentioned"),
            "location": job.location,
        }
        result = await self.llm.complete_json(_MATCHER_SYSTEM, json.dumps(payload, ensure_ascii=False))
        if not isinstance(result, dict):
            result = {}
        return {
            "qualification": _num(result.get("qualification")),
            "career": _num(result.get("career")),
            "language": _num(result.get("language")),
            "location": _num(result.get("location")),
            "immigration_fit": _num(result.get("immigration_fit")),
        }


def _num(v) -> float:
    try:
        return min(max(float(v), 0.0), 100.0)
    except (TypeError, ValueError):
        return 0.0
