"""SQLAlchemy 2.0 models — mirror the relational design in the architecture spec.

                      Memory tables
    candidates  jobs  companies  job_analysis  decisions  applications
    contacts    emails  immigration_programs  sources  events

Works on SQLite (dev) and PostgreSQL (production) without code changes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Candidate(Base):
    __tablename__ = "candidates"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255), default="")
    profile_yaml: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255), index=True)
    website: Mapped[str] = mapped_column(String(512), default="")
    country: Mapped[str] = mapped_column(String(64), default="")
    sponsorship_policy: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(128), index=True)
    external_id: Mapped[str] = mapped_column(String(255), index=True)
    dedup_key: Mapped[str] = mapped_column(String(512), index=True)
    title: Mapped[str] = mapped_column(String(255))
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    company: Mapped[Company | None] = relationship("Company")
    location: Mapped[str] = mapped_column(String(255), default="")
    country: Mapped[str] = mapped_column(String(64), default="", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(1024), default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    employment_type: Mapped[str] = mapped_column(String(32), default="")
    salary: Mapped[str] = mapped_column(String(128), default="")
    contact_email: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)  # new|analyzed|acted|ignored|closed


class JobAnalysis(Base):
    __tablename__ = "job_analysis"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    skills_required: Mapped[list[str]] = mapped_column(JSON, default=list)
    experience_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    experience_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    education_required: Mapped[str] = mapped_column(Text, default="")
    languages: Mapped[list[str]] = mapped_column(JSON, default=list)
    responsibilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    sponsorship_mentioned: Mapped[bool] = mapped_column(default=False)
    work_authorization: Mapped[str] = mapped_column(Text, default="")
    salary_estimate: Mapped[str] = mapped_column(String(128), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    model_used: Mapped[str] = mapped_column(String(128), default="")
    analyzed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    decision: Mapped[str] = mapped_column(String(32))  # APPLY|ASK_EMPLOYER|INVESTIGATE|HOLD|IGNORE
    overall_score: Mapped[float] = mapped_column(Float, default=0)
    scores: Mapped[dict[str, float]] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    rules_fired: Mapped[list[str]] = mapped_column(JSON, default=list)
    ai_reason: Mapped[str] = mapped_column(Text, default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Application(Base):
    __tablename__ = "applications"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    job: Mapped[Job | None] = relationship("Job")
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    # draft|sent|replied|interview|offer|rejected|deferred|blocked
    score: Mapped[float] = mapped_column(Float, default=0)
    action: Mapped[str] = mapped_column(String(32), default="")
    contact_email: Mapped[str] = mapped_column(String(255), default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    response: Mapped[str] = mapped_column(Text, default="")
    follow_up_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    follow_ups_sent: Mapped[int] = mapped_column(Integer, default=0)


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    person_name: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(128), default="")
    source: Mapped[str] = mapped_column(String(128), default="")
    first_contacted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Email(Base):
    __tablename__ = "emails"
    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    to_addr: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="validated")  # draft|validated|dry_run|drafted|sent|blocked|failed
    validation_log: Mapped[list[str]] = mapped_column(JSON, default=list)
    mode: Mapped[str] = mapped_column(String(16), default="")  # dry_run|draft|live
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    message_id: Mapped[str] = mapped_column(String(255), default="")  # Gmail message id (live)
    draft_id: Mapped[str] = mapped_column(String(255), default="")    # Gmail Draft id (draft mode)
    error: Mapped[str] = mapped_column(Text, default="")


class ImmigrationProgram(Base):
    __tablename__ = "immigration_programs"
    id: Mapped[int] = mapped_column(primary_key=True)
    country: Mapped[str] = mapped_column(String(64), index=True)
    program: Mapped[str] = mapped_column(String(255))
    occupation: Mapped[str] = mapped_column(String(255), default="")
    eligibility: Mapped[str] = mapped_column(Text, default="")
    language_requirements: Mapped[str] = mapped_column(Text, default="")
    work_experience: Mapped[str] = mapped_column(Text, default="")
    occupation_restrictions: Mapped[str] = mapped_column(Text, default="")
    claim: Mapped[str] = mapped_column(Text, default="")
    official_source_url: Mapped[str] = mapped_column(String(1024), default="")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))  # rss|api|html|html_search
    base_url: Mapped[str] = mapped_column(String(1024), default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    last_fetch_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    items_found: Mapped[int] = mapped_column(Integer, default=0)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    type: Mapped[str] = mapped_column(String(64), index=True)  # discovery|analysis|decision|action|followup|error
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
