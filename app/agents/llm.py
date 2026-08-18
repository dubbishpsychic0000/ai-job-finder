"""LLM provider abstraction.

Two drivers:
  * NullLLM  — deterministic, offline heuristics. Powers golden-set tests and
               runs the whole pipeline without any API key.
  * GeminiLLM — real LLM calls via google-genai.

Agents never talk to a vendor directly; they call `complete_json` and always
receive the exact JSON schema they asked for. The Null driver returns the same
schema so behaviour is testable end-to-end offline.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

from app.config import CandidateProfile, RunnerSettings

logger = logging.getLogger(__name__)


async def noop(*_: Any, **__: Any) -> None:
    return None


class LLMProvider(Protocol):
    name: str

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        ...


# ---------------------------------------------------------------------------
# Null driver — deterministic heuristics
# ---------------------------------------------------------------------------

CIVIL_SKILLS = {
    "civil engineering": ["genie civil", "génie civil", "civil engineering", "genie", "engineering civil"],
    "road construction": ["road construction", "voirie", "route", "roads", "chaussée", "chaussee", "wegebau"],
    "VRD": ["vrd", "routes et réseaux", "réseaux divers", "assainissement"],
    "geotechnics": ["geotechni", "géotechni", "geotech", "soil", "sol"],
    "site surveying": ["survey", "levé", "leve", "level", "niveaux", "topograph"],
    "AutoCAD": ["autocad", "cad", "cao"],
    "quality control": ["quality", "qualité", "qualite", "qa", "controle", "contrôle"],
    "concrete testing": ["concrete", "béton", "beton", "slump"],
    "project management": ["project management", "gestion de projet", "chef de projet"],
    "cost estimation": ["estimat", "devis", "budget", "métré", "metre"],
    "drainage": ["drainage", "assainissement"],
    "construction supervision": ["supervision", "surveill", "supervision de chantier", "site supervision"],
}


def _kw(text: str, keywords: list[str]) -> bool:
    return any(k.lower() in text.lower() for k in keywords)


def _extract_experience(text: str) -> tuple[int | None, int | None]:
    low = re.search(r"(\d+)\s*(?:\+|-|\s*à\s*)?\s*(\d+)?\s*(?:ans|years|yrs|années|jahren|anos|anni|an\b)", text.lower())
    if low:
        a, b = int(low.group(1)), low.group(2)
        return a, int(b) if b else a
    return None, None


def _extract_salary(text: str) -> str:
    m = re.search(
        r"(?:€|eur|euro|€/mois|k€)?\s*(\d{2,4})\s*(?:k)?\s*"
        r"(?:€|eur|euros|e|/mois|per year|per month|/an|/month|annual)",
        text,
        re.IGNORECASE,
    )
    return m.group(0)[:64] if m else ""


LANG_LEVELS = {
    "french": ["français", "francais", "french"],
    "english": ["english", "anglais"],
    "arabic": ["arabic", "arabe"],
    "dutch": ["dutch", "néerlandais", "nederlands"],
    "spanish": ["spanish", "espagnol", "español"],
    "german": ["german", "allemand", "deutsch"],
}


class NullLLM:
    """Deterministic offline agent. Returns JSON in agent schema."""

    name = "null"

    def __init__(self, profile: CandidateProfile | None = None):
        self.profile = profile

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        # The prompt header tells us which helper to use.
        if "JOB_ANALYST" in system:
            return self._analyze(user)
        if "CANDIDATE_MATCHER" in system:
            return self._match(user)
        if "DECISION" in system:
            return self._decide(user)
        if "COMMUNICATION" in system:
            return self._email(user)
        if "IMMIGRATION" in system:
            return self._immigration(user)
        return {"ok": False}

    # -- JOB_ANALYST ---------------------------------------------------
    def _analyze(self, text: str) -> dict[str, Any]:
        low = text.lower()
        skills = [s for s, kws in CIVIL_SKILLS.items() if _kw(low, kws)]
        exp_min, exp_max = _extract_experience(low)
        languages = [language for language, kws in LANG_LEVELS.items() if _kw(low, kws)]
        sponsorship = _kw(low, ["sponsor", "work permit", "visa", "visa sponsoring", "relocation",
                                "titre de séjour", "séjour", "autorisation de travail", "permis de travail",
                                "willing to sponsor"])
        return {
            "skills_required": skills,
            "experience_min": exp_min,
            "experience_max": exp_max,
            "education_required": "bachelor/civil technician" if _kw(low, ["bachelor", "master", "licence", "degr", "ingénieur", "technicien sp"]) else "",
            "languages": languages,
            "responsibilities": (low or "").split(". ")[:5],
            "sponsorship_mentioned": sponsorship,
            "work_authorization": _detect_work_auth(low),
            "salary_estimate": _extract_salary(low),
            "summary": f"Analyzed {len(skills)} skill groups, {len(languages)} languages, exp {'+' if exp_min else ''}{exp_min or '?'}y",
        }

    # -- CANDIDATE_MATCHER ---------------------------------------------
    def _match(self, text: str) -> dict[str, Any]:
        # text = JSON dump: {profile_fields..., job analysis}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {"qualification": 0, "career": 0, "language": 0, "location": 0, "immigration_fit": 0}
        profile_skills = set(payload.get("candidate_skills") or [])
        job_skills = set(payload.get("job_skills") or [])
        overlap = profile_skills & job_skills
        qual = 0
        if job_skills:
            qual = round(100 * len(overlap) / max(len(job_skills), 1), 1)
        career = min(90, 40 + 20 * len(overlap))
        # language: profile languages present in job
        job_langs = set(payload.get("job_languages") or [])
        lang = 100 if not job_langs else round(100 * len(job_langs & set(payload.get("candidate_languages") or [])) / len(job_langs), 1)
        # location: same country as target or relocatable
        loc = 90 if payload.get("relocation_willing") else 50
        # immigration fit from sponsorship flags
        imm = 80 if payload.get("sponsorship_mentioned") or payload.get("country_matches") else 60
        return {
            "qualification": qual,
            "career": round(career, 1),
            "language": lang,
            "location": loc,
            "immigration_fit": imm,
        }

    # -- DECISION -------------------------------------------------------
    def _decide(self, text: str) -> dict[str, Any]:
        try:
            p = json.loads(text)
        except json.JSONDecodeError:
            return {"decision": "IGNORE", "confidence": 0.5, "reason": "unparsable decision context"}
        score = p.get("overall_score", 0) or 0.0
        decision = str(p.get("band", "ignore")).lower()
        thresholded = {
            "apply": "APPLY",
            "investigate": "ASK_EMPLOYER",
            "hold": "HOLD",
            "ignore": "IGNORE",
        }.get(decision, "IGNORE")
        # AI override seeds: give INVESTIGATE a harder pull than simple banding
        if score >= 85 and p.get("contact_email") and p.get("sponsorship_ok", True):
            thresholded = "APPLY"
        if 70 <= score < 85 and p.get("contact_email"):
            thresholded = "ASK_EMPLOYER"
        if not p.get("contact_email") and 70 <= score < 85:
            thresholded = "INVESTIGATE"
        conf = min(0.95, 0.5 + (score / 100) * 0.5)
        return {"decision": thresholded, "confidence": round(conf, 2),
                "reason": f"score {score:.0f} => {thresholded.lower().replace('_', ' ')}"}

    # -- COMMUNICATION --------------------------------------------------
    def _email(self, text: str) -> dict[str, Any]:
        try:
            p = json.loads(text)
        except json.JSONDecodeError:
            p = {}
        name = p.get("candidate_name") or "Candidate"
        title = p.get("job_title") or p.get("candidate_title") or "the position"
        company = p.get("company") or "your company"
        role = p.get("job_title") or title
        return {
            "subject": f"Application — {role}",
            "body": (
                f"Dear Hiring Team,\n\nI am applying for the {role} position at {company}.\n\n"
                f"My name is {name}. I am a civil engineering technician with "
                f"{p.get('experience_years', 'several')} years of experience in road construction, "
                "VRD and geotechnics. Please find my CV attached.\n\n"
                "I am available for an interview and willing to relocate within a legal "
                "work-permit pathway.\n\nKind regards,\n" + name
            ),
        }

    # -- IMMIGRATION ----------------------------------------------------
    def _immigration(self, text: str) -> dict[str, Any]:
        try:
            p = json.loads(text)
        except json.JSONDecodeError:
            p = {}
        page = (p.get("page_text") or "").lower()
        eligible = any(k in page for k in ["skilled worker", "skilledworker", "express entry",
                                           "passeport talent", "talent", "highly skilled",
                                           "qualified worker", "travailleur qualifié", "travailleur hautement qualifié"])
        language = "varies by program"
        if "b2" in page or "intermediate" in page:
            language = "intermediate (e.g. B1–B2)"
        claims = []
        if eligible:
            claims.append({"claim": "Skilled-worker program may apply to this occupation",
                           "source": p.get("source_url", ""),
                           "verified_at": p.get("verified_at", "")})
        return {"eligible": eligible, "language_level": language,
                "notes": "Extracted from official page (offline heuristic).",
                "claims": claims}


def _detect_work_auth(low: str) -> str:
    if _kw(low, ["eu nationals", "citizens", "residents"]):
        return "restricted_to_nationals"
    if _kw(low, ["open to international", "accept international", "internationally"]):
        return "open_to_international"
    if _kw(low, ["candidate must be eligible", "must be authorized"]):
        return "authorization_required"
    return "unspecified"


# ---------------------------------------------------------------------------
# Gemini driver
# ---------------------------------------------------------------------------
class GeminiLLM:
    name = "gemini"

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        from google import genai  # deferred import

        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        resp = self._client.models.generate_content(
            model=self._model,
            contents=user,
            config={"system_instruction": system, "response_mime_type": "application/json"},
        )
        text = resp.text or "{}"
        return json.loads(text) if isinstance(text, str) else {}


def get_llm(profile: CandidateProfile | None = None, settings: RunnerSettings | None = None) -> LLMProvider:
    settings = settings or __import__("app.config", fromlist=["get_settings"]).get_settings()
    fallback = NullLLM(profile=profile)
    keys = _gemini_keys(settings)
    if settings.llm_provider != "gemini" or not keys:
        return fallback

    try:
        primaries = [GeminiLLM(key, settings.gemini_model) for key in keys]
    except Exception as exc:  # missing google-genai
        logger.warning("Gemini unavailable (%s); using NullLLM", exc)
        return fallback

    if not settings.llm_fallback:
        # fail-fast mode: quota/network errors propagate to callers
        return primaries[0]

    from app.agents.llm_resilience import DailyBudget, LLMCache, PrimaryKey, ResilientLLM, key_fingerprint

    from pathlib import Path

    cache = LLMCache(Path(settings.llm_cache_path))
    budget_dir = Path(settings.llm_budget_path).parent
    ring = [
        PrimaryKey(
            llm=llm,
            budget=DailyBudget(budget_dir / f"llm_budget_{key_fingerprint(key)}.json",
                               settings.llm_daily_budget),
        )
        for llm, key in zip(primaries, keys)
    ]
    return ResilientLLM(fallback=fallback, cache=cache, primaries=ring)


def _gemini_keys(settings: RunnerSettings) -> list[str]:
    """All configured Gemini API keys — secrets are never logged by callers."""
    from_list = [k.strip() for k in settings.gemini_api_keys.split(",") if k.strip()]
    if from_list:
        return from_list
    if settings.gemini_api_key:
        return [settings.gemini_api_key]
    return []
