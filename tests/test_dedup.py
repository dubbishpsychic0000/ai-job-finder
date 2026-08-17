"""Deduplication — exact key + cross-source fuzzy."""
from __future__ import annotations

from datetime import datetime, timezone

from app.connectors.base import Opportunity
from app.deduplication import find_duplicates
from app.memory.store import get_or_create_company, upsert_job


def _opp(title, company, location="France", country="France", source="a", ext="1"):
    return Opportunity(source=source, external_id=ext, title=title, company=company,
                       location=location, country=country, url="https://example.com/x",
                       posted_at=datetime.now(timezone.utc))


def test_exact_dedup_by_external_id(db):
    o1 = _opp("Technicien VRD", "Colas", ext="abc")
    o2 = _opp("Technicien VRD", "Colas", ext="abc")
    assert o1.dedup_key() == o2.dedup_key()

    dup = find_duplicates(db, [o1, o2])
    assert 1 not in dup, "same external id, same source -> duplicate"


def test_cross_source_hash_key(db):
    o1 = _opp("Technicien Génie Civil", "Bouygues", ext="EXT-1")
    o2 = _opp("Technicien Génie Civil", "Bouygues", ext="EXT-2")
    # different sources with no external id: normalize to hash key
    o1.external_id = o2.external_id = ""
    assert o1.dedup_key() == o2.dedup_key()


def test_probable_duplicate_same_title_company(db):
    o1 = _opp("Technicien VRD", "Colas", ext="x1")
    o1.external_id = ""
    _, _ = upsert_job(db, {
        "source": "a", "external_id": "X", "dedup_key": o1.dedup_key(),
        "title": o1.title, "company_id": get_or_create_company(db, "Colas").id,
        "location": o1.location, "country": "France", "description": "", "url": o1.url,
        "posted_at": datetime.now(timezone.utc), "status": "new",
    })
    db.flush()

    # a NEW external id (as if surfaced on another board): hash key differs,
    # but the fuzzy title+company check must still flag it as probable
    fresh = _opp("Technicien VRD", "Colas", ext="ext-online-987")
    fresh.external_id = "ext-online-987"
    dup = find_duplicates(db, [fresh])
    assert dup.get(0) == "probable"


def test_distinct_jobs_not_duplicates(db):
    o1 = _opp("Technicien VRD", "Colas")
    o2 = _opp("Chef de Projet Informatique", "Some Tech")
    dup = find_duplicates(db, [o1, o2])
    assert dup == {}
