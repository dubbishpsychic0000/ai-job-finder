"""Scoring engine — deterministic aggregation, weights, thresholds."""
from __future__ import annotations

import pytest

from app.config import get_config
from app.scoring.engine import compute_scores, employer_confidence


def test_compute_scores_weighted():
    config = get_config()
    matcher = {"qualification": 80, "career": 90, "language": 70, "location": 60, "immigration_fit": 50}
    mobility = {"international_fit": 75, "sponsorship_potential": 60}
    dims, overall = compute_scores(matcher=matcher, mobility=mobility, source="rss", config=config)

    assert set(dims) == {"qualification", "career", "international_fit", "sponsorship_potential",
                         "relocation_practicality", "employer_confidence"}
    assert 0 <= overall <= 100
    # employer confidence comes from source, not the LLM
    assert dims["employer_confidence"] == 92.0
    # relocation practicality = location sub-score
    assert dims["relocation_practicality"] == 60.0


def test_overall_within_band_bounds():
    config = get_config()
    _, overall = compute_scores(
        matcher={"qualification": 100, "career": 100, "language": 100, "location": 100},
        mobility={"international_fit": 100, "sponsorship_potential": 100},
        source="rss", config=config)
    # employer confidence is 0.92 (rss) -> tiny drag below 100
    assert overall > 99 and overall <= 100

    _, overall = compute_scores(
        matcher={"qualification": 0, "career": 0, "language": 0, "location": 0},
        mobility={"international_fit": 0, "sponsorship_potential": 0},
        source="rss", config=config)
    # only employer confidence contributes (0.92*0.05)
    assert overall == pytest.approx(4.6, abs=0.1)


def test_employer_confidence_weighted_in():
    config = get_config()
    _, high = compute_scores(
        matcher={"qualification": 100, "career": 100, "language": 100, "location": 100},
        mobility={"international_fit": 100, "sponsorship_potential": 100},
        source="static_files", config=config)
    _, low = compute_scores(
        matcher={"qualification": 100, "career": 100, "language": 100, "location": 100},
        mobility={"international_fit": 100, "sponsorship_potential": 100},
        source="search_engine", config=config)
    assert high > low  # trusted source outranks a scrape search


def test_employer_confidence_per_source():
    assert employer_confidence("static_files") == 0.95
    assert employer_confidence("search_engine") == 0.65
    assert employer_confidence("unknown_thing") == 0.75


def test_threshold_bands():
    config = get_config()
    assert config.threshold_for(90) == "apply"
    assert config.threshold_for(75) == "investigate"
    assert config.threshold_for(60) == "hold"
    assert config.threshold_for(30) == "ignore"
