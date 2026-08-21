"""Central configuration: environment + YAML settings + candidate profile.

Everything the agents need (thresholds, weights, preferences, LLM settings)
is loadable from here so the rest of the code stays dependency-injected.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class RunnerSettings(BaseSettings):
    """Environment-driven settings (secrets, toggles, infra)."""

    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = f"sqlite:///{ROOT_DIR / 'data' / 'agent.db'}"

    llm_provider: str = "null"  # null | gemini
    gemini_api_key: str = ""
    gemini_api_keys: str = ""  # comma-separated ring; fallback first key to gemini_api_key
    gemini_model: str = "gemini-flash-latest"
    llm_cache_path: str = str(ROOT_DIR / "data" / "llm_cache.json")
    llm_budget_path: str = str(ROOT_DIR / "data" / "llm_budget.json")
    llm_daily_budget: int = 20
    llm_fallback: bool = True

    enable_email: bool = False
    email_provider: str = "gmail"  # log | smtp | gmail
    email_mode: str = "draft"      # dry_run | draft | live
    email_from: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    gmail_client_secret_path: str = ""  # defaults to secrets/client_secret.json
    gmail_token_path: str = ""          # defaults to secrets/gmail_token.json

    # Meta WhatsApp webhook verification. Both stay in repository secrets.
    whatsapp_webhook_verify_token: str = ""
    whatsapp_app_secret: str = ""

    # Outbound communication policy (configurable via .env, no code changes).
    daily_max_applications: int = 5      # APPLY emails per day
    daily_max_inquiries: int = 5         # ASK_EMPLOYER / information requests per day
    daily_max_outbound: int = 10         # total outbound emails per day
    employer_cooldown_days: int = 7      # cooldown between communications with same company
    min_application_score: float = 80.0  # min opportunity score to auto-apply
    min_application_confidence: float = 0.80  # min agent confidence to auto-apply

    apply_daily_limit: int = 10
    global_pause: bool = False
    search_cadence_hours: int = 12

    @property
    def sqlite_path(self) -> Path:
        if self.database_url.startswith("sqlite"):
            return Path(self.database_url.split("///")[-1])
        return ROOT_DIR / "data" / "agent.db"


class CandidateProfile(BaseModel):
    name: str
    title: str = ""
    phone: str = ""
    summary: str = ""
    education: list[dict[str, Any]] = Field(default_factory=list)
    experience_years: int = 0
    skills: list[str] = Field(default_factory=list)
    languages: dict[str, str] = Field(default_factory=dict)
    certifications: list[Any] = Field(default_factory=list)
    projects: list[dict[str, Any]] = Field(default_factory=list)
    relocation: dict[str, Any] = Field(default_factory=dict)
    availability: dict[str, Any] = Field(default_factory=dict)
    identity_hard_constraints: list[str] = Field(default_factory=list)

    def claims_allowedlist(self) -> set[str]:
        """Flat set of every string the profile authorises agent to claim."""
        tokens = {self.title, self.name, str(self.experience_years), self.phone}
        for e in self.education:
            tokens.update(str(v) for v in e.values())
        tokens.update(self.skills)
        tokens.update(self.languages)
        for p in self.projects:
            tokens.update(str(v) for v in p.values())
        return {t.lower() for t in tokens if t}


class Preferences(BaseModel):
    target_countries: list[str] = Field(default_factory=list)
    target_roles: list[str] = Field(default_factory=list)
    localized_roles: dict[str, list[str]] = Field(default_factory=dict)
    employment_preferences: dict[str, bool] = Field(default_factory=dict)
    min_salary_monthly_eur: int = 0
    sponsorship_required: bool = True

    @property
    def countries(self) -> list[str]:
        return self.target_countries


class AgentConfig(BaseModel):
    """Policy config — thresholds, weights, hard rules (from config/settings.yaml)."""

    score_thresholds: dict[str, dict[str, int]] = Field(
        default_factory=lambda: {
            "apply": {"min": 85, "max": 100},
            "investigate": {"min": 70, "max": 84},
            "hold": {"min": 50, "max": 69},
            "ignore": {"min": 0, "max": 49},
        }
    )
    scoring_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "qualification": 0.30,
            "career": 0.20,
            "international_fit": 0.20,
            "sponsorship_potential": 0.15,
            "relocation_practicality": 0.10,
            "employer_confidence": 0.05,
        }
    )
    rules: dict[str, Any] = Field(default_factory=dict)
    email: dict[str, Any] = Field(default_factory=dict)
    discovery: dict[str, Any] = Field(default_factory=dict)
    search_plan: dict[str, Any] = Field(default_factory=dict)
    scheduler: dict[str, Any] = Field(default_factory=dict)

    def threshold_for(self, score: float) -> str:
        for name in ("apply", "investigate", "hold", "ignore"):
            band = self.score_thresholds[name]
            if band["min"] <= score <= band["max"]:
                return name
        return "ignore"


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def get_settings() -> RunnerSettings:
    return RunnerSettings()


def get_profile(path: Path | None = None) -> CandidateProfile:
    p = path or ROOT_DIR / "candidate" / "profile.yaml"
    return CandidateProfile.model_validate(load_yaml(p))


def get_preferences(path: Path | None = None) -> Preferences:
    p = path or ROOT_DIR / "candidate" / "preferences.yaml"
    return Preferences.model_validate(load_yaml(p))


def get_config(path: Path | None = None) -> AgentConfig:
    p = path or ROOT_DIR / "config" / "settings.yaml"
    return AgentConfig.model_validate(load_yaml(p))
