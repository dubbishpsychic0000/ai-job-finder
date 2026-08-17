"""Scoring engine — deterministic aggregation.

The LLM produces *component* sub-scores; THIS module combines them into the
six reported dimensions and the overall score. Deterministic weights live in
config/settings.yaml so behaviour is reproducible and testable (see critique
point #1: engineered outputs, not vibes).
"""
from __future__ import annotations

from app.config import AgentConfig

SOURCE_CONFIDENCE = {
    "rss": 0.92,
    "static_files": 0.95,
    "company_careers": 0.85,
    "search_engine": 0.65,
}


def employer_confidence(source: str, default: float = 0.75) -> float:
    return SOURCE_CONFIDENCE.get(source.split(":")[0], default)


def compute_scores(
    *,
    matcher: dict[str, float],
    mobility: dict[str, float],
    source: str,
    config: AgentConfig | None = None,
) -> tuple[dict[str, float], float]:
    """Returns (dimension_scores, overall_score)."""
    config = config or AgentConfig()
    conf = employer_confidence(source)
    dimensions = {
        "qualification": matcher.get("qualification", 0),
        "career": matcher.get("career", 0),
        "international_fit": mobility.get("international_fit", 0),
        "sponsorship_potential": mobility.get("sponsorship_potential", 0),
        "relocation_practicality": matcher.get("location", 0),
        "employer_confidence": round(conf * 100, 1),
    }
    w = config.scoring_weights
    total_w = sum(w.values())
    overall = sum(dimensions[k] * w.get(k, 0) for k in dimensions) / total_w if total_w else 0.0
    return dimensions, round(overall, 1)
