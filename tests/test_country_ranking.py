"""Country ranking tests — Phase 7 (spec §12).

Deterministic preference-affinity scoring the search scheduler uses to order
countries. No external facts are invented; only local profile/preferences.
"""
from __future__ import annotations

from app.discovery.country_ranking import CountryScore, rank_countries


def test_language_affinity_ranks_france_first(prefs, profile):
    # fixture profile languages: french, english, arabic
    ranking = rank_countries(prefs.countries, prefs, profile)
    assert isinstance(ranking[0], CountryScore)
    assert ranking[0].country == "France"  # fr matches; stable among fr countries
    fr = next(cs for cs in ranking if cs.country == "France")
    assert fr.score == 1.5
    assert any("language" in reason for reason in fr.reasons)


def test_german_speaker_prefers_german_speaking_countries():
    class _Profile:
        def __init__(self):
            self.languages = {"german": "native"}

    ranking = rank_countries(["France", "Germany", "Spain"], profile=_Profile())
    assert ranking[0].country == "Germany"
    assert ranking[0].score == 1.5


def test_relocation_preference_adds_signal():
    class _Profile:
        def __init__(self):
            self.languages = {}
            self.relocation = {"preferred_countries": ["Canada"]}

    ranking = rank_countries(["France", "Canada"], profile=_Profile())
    canada = next(cs for cs in ranking if cs.country == "Canada")
    assert canada.score == 2.0
    assert "preferred relocation" in canada.reasons


def test_stable_order_without_any_signals():
    ranking = rank_countries(["Zulu", "Alpha", "Beta"])
    assert [cs.country for cs in ranking] == ["Zulu", "Alpha", "Beta"]  # stable
    assert all(cs.score == 0.0 and cs.reasons == [] for cs in ranking)
