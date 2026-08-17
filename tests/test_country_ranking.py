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


# ---- full §12 model (DB-backed evidence) ------------------------------------


def _seed_job(db, country, sponsorship="unknown", international="unknown", i=0):
    from app import memory as mem

    company = mem.store.get_or_create_company(db, f"C{i}", "", country)
    mem.store.upsert_job(db, {
        "source": "test", "external_id": f"s{i}", "dedup_key": f"hash:{country}{i}",
        "title": "Tech", "company_id": company.id, "location": country, "country": country,
        "description": "", "url": f"https://example.com/{i}", "posted_at": None,
        "employment_type": "full_time", "salary": "", "contact_email": "", "status": "new",
        "source_type": "ats", "source_quality": 90, "source_confidence": 90,
        "closing_at": None, "language": "", "sponsorship_signal": sponsorship,
        "international_candidate_signal": international, "relocation_signal": "unknown",
        "work_permit_signal": "unknown", "verification_status": "verified",
        "search_query": "", "search_language": "", "search_country": "",
        "canonical_job_id": f"canon:{country}{i}",
    })


def test_demand_and_sponsorship_evidence(db):
    """§12 — more sponsorship-heavy demand ranks a country higher than raw volume."""
    _seed_job(db, "France", sponsorship="yes", i=0)
    _seed_job(db, "France", sponsorship="yes", i=1)
    _seed_job(db, "Spain", sponsorship="none", i=2)
    _seed_job(db, "Spain", sponsorship="none", i=3)
    _seed_job(db, "Spain", sponsorship="none", i=4)
    _seed_job(db, "Spain", sponsorship="none", i=5)
    ranking = rank_countries(["France", "Spain"], session=db)
    top = {cs.country: cs.score for cs in ranking}
    assert top["France"] == 25.0   # demand 0.5*20 + sponsorship 1.0*15
    assert top["Spain"] == 20.0    # demand 1.0*20 + sponsorship 0
    assert ranking[0].country == "France"
    assert any("visa sponsorship" in cs.reasons for cs in ranking if cs.country == "France")


def test_language_weight_in_full_model(db):
    class _Profile:
        def __init__(self):
            self.languages = {"french": "native"}

    ranking = rank_countries(["France", "Germany"], profile=_Profile(), session=db)
    top = {cs.country: cs.score for cs in ranking}
    assert top["France"] == 25.0
    assert ranking[0].country == "France"


def test_immigration_pathway_evidence(db):
    from app import models

    db.add(models.ImmigrationProgram(country="Canada", program="Express Entry"))
    db.add(models.ImmigrationProgram(country="Canada", program="PNP"))
    db.flush()
    ranking = rank_countries(["Canada", "Spain"], session=db)
    top = {cs.country: cs.score for cs in ranking}
    assert top["Canada"] == 5.0   # immigration fraction 1.0 * weight 5
    assert top["Spain"] == 0.0
    assert ranking[0].country == "Canada"
    assert any("immigration pathways" in cs.reasons for cs in ranking if cs.country == "Canada")
