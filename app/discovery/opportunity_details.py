"""Deterministic opportunity classification and application-route detection.

These helpers intentionally make conservative claims.  They are used before
an LLM's optional interpretation so a job-board link never becomes a made-up
email application route.
"""
from __future__ import annotations

import re

OPPORTUNITY_TYPES = {
    "JOB", "INTERNSHIP", "APPRENTICESHIP", "TRAINING", "RECRUITMENT_EVENT",
    "RECRUITER_OPPORTUNITY", "IMMIGRATION_OPPORTUNITY", "SCHOLARSHIP", "GRADUATE_PROGRAM",
}
APPLICATION_METHODS = {
    "EMAIL", "ONLINE_FORM", "COMPANY_PORTAL", "JOB_BOARD", "RECRUITER",
    "PHONE", "WHATSAPP", "IN_PERSON", "UNKNOWN",
}

_TYPE_PATTERNS = (
    ("IMMIGRATION_OPPORTUNITY", r"\b(immigration|visa|work permit|permis de travail)\b"),
    ("SCHOLARSHIP", r"\b(scholarship|bourse)\b"),
    ("GRADUATE_PROGRAM", r"\b(graduate program(?:me)?|programme jeune diplômé)\b"),
    ("APPRENTICESHIP", r"\b(apprenticeship|apprenti(?:ssage)?)\b"),
    ("INTERNSHIP", r"\b(internship|intern\b|stage|stagiaire)\b"),
    ("TRAINING", r"\b(training|formation|bootcamp|cours)\b"),
    ("RECRUITMENT_EVENT", r"\b(job fair|recruitment event|forum emploi|salon emploi)\b"),
    ("RECRUITER_OPPORTUNITY", r"\b(recruiter|recruitment agency|cabinet de recrutement|intérim)\b"),
)


def classify_opportunity(title: str = "", description: str = "", source_type: str = "") -> str:
    text = f"{title}\n{description}".lower()
    for kind, pattern in _TYPE_PATTERNS:
        if re.search(pattern, text, re.I):
            return kind
    return "RECRUITER_OPPORTUNITY" if source_type == "recruitment" else "JOB"


def detect_application_method(*, text: str = "", url: str = "", source_type: str = "",
                              has_verified_email: bool = False) -> tuple[str, str]:
    """Return ``(method, application_url)`` without inventing a contact method."""
    value = text.lower()
    if re.search(r"(?:whatsapp|\bwa\.me/)\b", value):
        return "WHATSAPP", url
    if re.search(r"(?:call|phone|téléphone|tel\.)\s*(?:us|nous)?", value):
        return "PHONE", ""
    if re.search(r"(?:in person|walk[ -]?in|sur place|présentez[- ]vous)", value):
        return "IN_PERSON", ""
    if source_type == "recruitment" or re.search(r"\b(recruiter|cabinet de recrutement|recruitment agency)\b", value):
        return "RECRUITER", url
    if re.search(r"(?:apply online|apply now|postuler(?: en ligne)?|candidater|submit application)", value):
        return ("COMPANY_PORTAL" if source_type in {"ats", "company_career"} else "ONLINE_FORM"), url
    if source_type in {"ats", "company_career"}:
        return "COMPANY_PORTAL", url
    if source_type == "job_board":
        return "JOB_BOARD", url
    if has_verified_email:
        return "EMAIL", ""
    return "UNKNOWN", ""
