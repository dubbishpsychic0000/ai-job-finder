"""Communication Agent — generates application / info-request emails.

THE RULE: the generated message may only reference facts present in the
candidate profile (claims allowedlist). It is explicitly prohibited from
inventing experience, qualifications, certifications, employers, projects,
languages, visa status, or salary history. The safety gate re-verifies this
before anything is sent.
"""
from __future__ import annotations

import json
import logging

from app.agents.llm import LLMProvider

logger = logging.getLogger(__name__)

_COMM_SYSTEM = """You write concise, professional, honest job-application email
content. The candidate facts are provided in JSON under "candidate". You MUST
NOT invent or imply anything not in those facts. Write in the language of the
"target_language" field. Return ONLY JSON:
{"subject": str, "body": str}
No filler, no lies, no exaggerated claims. COMMUNICATION_AGENT"""


class CommunicationAgent:
    def __init__(self, llm: LLMProvider, profile):
        self.llm = llm
        self.profile = profile

    async def generate(self, job, action: str, target_language: str = "en",
                       recipient_name: str = "") -> dict:
        facts = {
            "candidate_name": self.profile.name,
            "candidate_title": self.profile.title,
            "candidate_phone": self.profile.phone,
            "experience_years": self.profile.experience_years,
            "skills": self.profile.skills,
            "languages": self.profile.languages,
            "education": self.profile.education,
            "relocation_willing": bool((self.profile.relocation or {}).get("willing")),
            "hard_constraints": self.profile.identity_hard_constraints,
        }
        payload = {
            "action": action,
            "target_language": target_language,
            "job_title": job.title,
            "company": job.company.name if job.company else "",
            "job_location": job.location,
            "recipient_name": recipient_name,
            "candidate": facts,
        }
        result = await self.llm.complete_json(_COMM_SYSTEM, json.dumps(payload, ensure_ascii=False))
        if not isinstance(result, dict):
            result = {}
        body = str(result.get("body", "")).strip()
        phone = self.profile.phone or ""
        if phone and phone not in body:
            # deterministic signature so the phone is always on the email
            tail = [self.profile.name, self.profile.title]
            block = tail + [phone]
            if self.profile.name.lower() in body.lower() and self.profile.title.lower() in body.lower():
                block = [phone]
            body = (body + "\n\n" + "\n".join(x for x in block if x)).strip()
        return {
            "subject": str(result.get("subject", "")).strip(),
            "body": body,
        }
