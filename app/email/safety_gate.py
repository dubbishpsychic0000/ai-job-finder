"""Email Safety Gate — the last line of defence before any outbound message.

Deterministic checks (not LLM self-checks):
  1. recipient is present & syntactically valid
  2. we haven't contacted this address during the cooldown window
  3. no invented claims: every recognizable claim token in the body must exist
     in the candidate profile allowedlist (skills, years, name, languages...)
  4. the posting is not stale (> max_days_since_posted)
  5. a real CV file exists to attach
  6. global + per-day outbound limits are respected

A single failed check BLOCKS the send and explains why.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import memory as mem
from app.config import AgentConfig, CandidateProfile

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class ValidationReport:
    allowed: bool
    checks: dict[str, bool | str] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def block(self, reason: str, check: str) -> None:
        self.allowed = False
        self.checks[check] = False
        self.reasons.append(reason)


def _claims_ok(body: str, profile: CandidateProfile, job) -> list[str]:
    """Return list of invented-claim problems (empty == clean)."""
    problems: list[str] = []
    body_low = body.lower()
    allowed = profile.claims_allowedlist()

    for skill in profile.skills:
        if skill and skill.lower() not in allowed:
            allowed.add(skill.lower())

    # years-of-experience claim must equal profile value
    for m in re.finditer(r"(\d{1,2})\s*(?:years|yrs|ans)\b", body_low):
        claimed = int(m.group(1))
        if claimed != profile.experience_years:
            problems.append(f"claims {claimed} years experience (profile says {profile.experience_years})")

    # language fluency claims must not exceed the profile level
    language_levels = {"native": 4, "courant": 3, "fluent": 3, "excellent": 3, "bilingue": 3,
                       "c2": 3, "c1": 3, "advanced": 3, "intermediate": 2, "b1": 2, "b2": 2,
                       "basic": 1, "a1": 1, "a2": 1}
    known_langs = list(_LANGUAGE_NAMES) + list(profile.languages)
    for lang in known_langs:
        if lang in body_low:
            quals = [w for w, _ in language_levels.items()
                     if _protected(w) and _same_sentence(body_low, lang, w)]
            for qual in quals:
                claimed_level = language_levels[qual]
                profile_level = language_levels.get(profile.languages.get(lang, ""), 0)
                if claimed_level > profile_level:
                    problems.append(f"claims {qual} in {lang} (profile: {profile.languages.get(lang, 'not listed')})")
                break  # first qualifier per language is enough

    # invented-object claims: strong claim verb followed by a noun phrase that
    # must exist in the profile allowedlist (employer/school/certification/etc).
    for m in _CLAIM_VERB_RE.finditer(body_low):
        obj = m.group(2).strip(" ,.;:()")
        if obj and obj not in allowed:
            problems.append(f"references unknown fact: '{m.group(1).strip()} {obj}'")
    return problems


_CLAIM_VERB_RE = re.compile(
    r"(worked\s+at|studied\s+at|graduated\s+from|certified\b|employed\s+by|"
    r"based\s+in|living\s+in|residing\s+in|hold\s+a|possess\s+an?)"
    r"\s*([\w'’-]+(?:\s+[\w'’-]+){0,3})"
)


_LANGUAGE_NAMES = ("french", "français", "english", "anglais", "arabic", "arabe",
                   "dutch", "néerlandais", "nederlands", "spanish", "espagnol", "español",
                   "german", "allemand", "deutsch", "portuguese", "portugais")


def _protected(w: str) -> bool:
    return re.fullmatch(r"[a-zà-ÿ]+", w) is not None


def _same_sentence(body_low: str, lang: str, qual: str) -> bool:
    """Whether lang and qualifier appear within the same sentence window (±40 chars)."""
    i = body_low.find(lang)
    j = body_low.find(qual)
    return j != -1 and i != -1 and abs(i - j) <= 40





def validate(
    *,
    to_addr: str,
    subject: str,
    body: str,
    attachments: list[str],
    job,
    profile: CandidateProfile,
    config: AgentConfig,
    session: Session,
    action: str = "APPLY",
    daily_sent: int = 0,
    daily_applications: int = 0,
    daily_inquiries: int = 0,
    daily_total: int | None = None,
    employer_cooldown_days: int | None = None,
    now: datetime | None = None,
    check_cooldown: bool = True,
) -> ValidationReport:
    report = ValidationReport(allowed=True)
    now = now or datetime.now(timezone.utc)
    email_cfg = config.email
    rules = config.rules

    if not to_addr or not _EMAIL_RE.match(to_addr):
        report.block(f"invalid recipient '{to_addr}'", "recipient")

    if email_cfg.get("require_verifiable_recipient") and (not to_addr or not _EMAIL_RE.match(to_addr)):
        report.block("recipient not verifiable", "recipient_verifiable")

    if check_cooldown:
        recent = mem.store.recent_contact(session, to_addr, days=int(rules.get("follow_up_days", [7])[0]))
        if recent:
            report.block(f"already contacted this address {recent.last_contacted_at}", "cooldown")

    # employer-level cooldown: never spam the same company within the window
    cdays = employer_cooldown_days if employer_cooldown_days is not None else int(rules.get("employer_cooldown_days", 7))
    if check_cooldown and cdays > 0 and job.company_id:
        employer = mem.store.recent_company_contact(session, job.company_id, days=cdays)
        if employer:
            report.block(
                f"employer already contacted {employer.last_contacted_at} "
                f"(cooldown {cdays}d)", "employer_cooldown")

    # duplicate-application check: never email twice for the same job
    submitted = ("sent", "drafted", "deferred")
    if mem.store.find_applications(session, job.id, statuses=submitted):
        report.block("this job was already applied to", "duplicate")

    if email_cfg.get("require_claims_allowedlist"):
        for problem in _claims_ok(body, profile, job):
            report.block(f"invented claim: {problem}", "claims")

    if job.posted_at:
        max_age = int(config.rules.get("max_days_since_posted", 45))
        posted = job.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        age = (now - posted).days
        if age > max_age:
            report.block(f"posting is {age} days old (> {max_age})", "freshness")

    if not attachments:
        report.block("no CV attachment resolved", "attachment")
    else:
        from pathlib import Path

        for a in attachments:
            if not Path(a).exists():
                report.block(f"attachment missing: {a}", "attachment")

    max_total = int(email_cfg.get("max_daily_outbound", rules.get("max_daily_outbound", 10)))
    total = daily_total if daily_total is not None else daily_sent
    if total >= max_total:
        report.block(f"daily outbound limit {max_total} reached", "rate_limit")

    apply_limit = int(rules.get("max_daily_applications", 5))
    inquiry_limit = int(rules.get("max_daily_inquiries", 5))
    if action in ("APPLY", "APPLIED") and daily_applications >= apply_limit:
        report.block(f"daily application limit {apply_limit} reached", "daily_applications")
    if action in ("ASK_EMPLOYER",) and daily_inquiries >= inquiry_limit:
        report.block(f"daily inquiry limit {inquiry_limit} reached", "daily_inquiries")

    return report
