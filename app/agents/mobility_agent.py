"""Mobility Agent — evaluates the international-work situation for a posting.

Answers: can foreign applicants apply? sponsorship? work permit? relocation
support? visa pathway? Output feeds `international_fit` + `sponsorship_potential`
sub-scores.
"""
from __future__ import annotations

import json

from app.agents.llm import LLMProvider

_MOBILITY_SYSTEM = """You assess whether a foreign, non-resident candidate could
take this job across borders. Return ONLY JSON:
{"foreign_applicants_ok": bool, "sponsorship_available": bool,
 "work_permit_required": bool, "relocation_support": bool,
 "visa_pathway": str, "notes": str}
Default NO when unclear. Do not speculate about immigration law — that is a
separate subsystem. MOBILITY_ANALYST"""


class MobilityAgent:
    def __init__(self, llm: LLMProvider):
        self.llm = llm

    async def analyze(self, job, analysis: dict) -> dict:
        payload = {
            "title": job.title,
            "country": job.country,
            "location": job.location,
            "work_authorization": analysis.get("work_authorization"),
            "sponsorship_mentioned": analysis.get("sponsorship_mentioned"),
            "description_excerpt": job.description[:1200],
        }
        text = json.dumps(payload, ensure_ascii=False)
        result = await self.llm.complete_json(_MOBILITY_SYSTEM, text)
        if not isinstance(result, dict):
            result = {}

        sponsorship = result.get("sponsorship_available")
        if sponsorship is None:
            sponsorship = bool(analysis.get("sponsorship_mentioned"))
        international_fit = 70.0
        if result.get("work_permit_required") is False:
            international_fit = 90.0
        if result.get("foreign_applicants_ok") is False:
            international_fit = 25.0
        sponsorship_potential = 100.0 if sponsorship else (55.0 if result.get("foreign_applicants_ok") else 20.0)
        return {
            "foreign_applicants_ok": bool(result.get("foreign_applicants_ok", True)),
            "sponsorship_available": bool(sponsorship),
            "work_permit_required": bool(result.get("work_permit_required", True)),
            "relocation_support": bool(result.get("relocation_support", False)),
            "visa_pathway": result.get("visa_pathway", ""),
            "notes": result.get("notes", ""),
            "international_fit": international_fit,
            "sponsorship_potential": sponsorship_potential,
        }
