"""Job Analyst — extracts a structured requirements profile from a posting."""
from __future__ import annotations

from app.agents.llm import LLMProvider

_ANALYST_SYSTEM = """You are a senior recruitment analyst. Read the job posting
below and return a single JSON object with EXACTLY these keys:
{"skills_required": [...], "experience_min": int|null, "experience_max": int|null,
 "education_required": str, "languages": [...], "responsibilities": [...],
 "sponsorship_mentioned": bool, "work_authorization": str,
 "salary_estimate": str, "summary": str}
Do not invent skills not present in the posting. Empty arrays instead of guesses.
JOB_ANALYST"""


class JobAnalyzer:
    def __init__(self, llm: LLMProvider):
        self.llm = llm

    async def analyze(self, job) -> dict:
        text = f"TITLE: {job.title}\nCOMPANY: {job.company.name if job.company else ''}\nLOCATION: {job.location}\nDESCRIPTION:\n{job.description}"
        analysis = await self.llm.complete_json(_ANALYST_SYSTEM, text)
        return self._shape(analysis, job)

    def _shape(self, raw: dict, job) -> dict:
        if not isinstance(raw, dict):
            raw = {}
        return {
            "skills_required": list(raw.get("skills_required") or []),
            "experience_min": raw.get("experience_min"),
            "experience_max": raw.get("experience_max"),
            "education_required": raw.get("education_required") or "",
            "languages": list(raw.get("languages") or []),
            "responsibilities": list(raw.get("responsibilities") or []),
            "sponsorship_mentioned": bool(raw.get("sponsorship_mentioned")),
            "work_authorization": raw.get("work_authorization") or "unspecified",
            "salary_estimate": raw.get("salary_estimate") or (job.salary or ""),
            "summary": raw.get("summary") or "",
        }
