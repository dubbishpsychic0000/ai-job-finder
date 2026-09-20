from __future__ import annotations

from pathlib import Path

from app import memory as mem, models
from app.discovery.website_researcher import (
    WebsiteResearcher,
    classify_email,
    evidence_backed_fields,
    normalize_url,
)


class Response:
    def __init__(self, text="", content_type="text/html"):
        self.text = text
        self.headers = {"Content-Type": content_type}
        self.status_code = 200
        self.ok = True

    def raise_for_status(self):
        return None


def researcher(pages, *, db=None, max_pages=8, state_path=None, robots=None):
    def fetch(url):
        return pages.get(url)

    return WebsiteResearcher(
        max_pages=max_pages,
        per_host_delay=0,
        state_path=state_path,
        session=db,
        fetcher=fetch,
        robots_fetcher=lambda domain: robots,
    )


def test_sitemap_discovery_and_internal_priority():
    pages = {
        "https://acme.ma/sitemap.xml": Response(
            "<urlset><url><loc>https://acme.ma/about</loc></url>"
            "<url><loc>https://acme.ma/careers</loc></url></urlset>",
            "application/xml",
        ),
        "https://acme.ma/": Response(
            '<title>Acme</title><a href="/about">About</a><a href="/careers">Careers</a>'
        ),
        "https://acme.ma/careers": Response(
            "<title>Careers</title>We accept applications at recruitment@acme.ma"
        ),
        "https://acme.ma/about": Response("<title>About</title>Company information"),
    }
    result = researcher(pages).research("https://acme.ma/")
    assert "https://acme.ma/careers" in result.pages
    assert result.pages.index("https://acme.ma/careers") < result.pages.index("https://acme.ma/about")
    assert result.facts("recruitment_email")[0].extracted_value == "recruitment@acme.ma"


def test_url_deduplication_and_crawl_limit():
    pages = {
        "https://acme.ma/": Response(
            '<a href="/careers#top">Careers</a><a href="/careers">same</a>'
            '<a href="/contact">Contact</a><a href="https://other.example/x">external</a>'
        ),
        "https://acme.ma/careers": Response("careers"),
        "https://acme.ma/contact": Response("contact"),
    }
    result = researcher(pages, max_pages=2).research("https://acme.ma/")
    assert len(result.visited) == 2
    assert len(result.visited) == len(set(result.visited))
    assert all("other.example" not in url for url in result.visited)
    assert normalize_url("https://acme.ma/careers#top") == "https://acme.ma/careers"


def test_robots_handling():
    pages = {"https://acme.ma/": Response("<a href='/careers'>Careers</a>")}

    class Robots:
        def can_fetch(self, agent, url):
            return url.endswith("/")

    result = researcher(pages, robots=Robots()).research("https://acme.ma/")
    assert "https://acme.ma/careers" in result.blocked


def test_email_classification_and_domain_validation():
    assert classify_email("rh@acme.ma", "acme.ma")[:2] == ("company_recruitment", "employer_domain_recruitment")
    assert classify_email("support@greenhouse.io", "acme.ma")[0] == "ats_vendor"
    assert classify_email("person@gmail.com", "acme.ma")[0] == "free_mail"
    assert classify_email("jobs@other.ma", "acme.ma")[0] == "third_party"


def test_evidence_persistence_and_spontaneous_route(db):
    pages = {
        "https://acme.ma/": Response('<a href="/contact">Contact</a>'),
        "https://acme.ma/contact": Response(
            "<title>Contact</title>Send unsolicited applications to recrutement@acme.ma"
        ),
    }
    company = mem.store.get_or_create_company(db, "Acme", "https://acme.ma", official_domain="acme.ma")
    db.flush()
    result = researcher(pages, db=db).research_and_apply(
        "https://acme.ma/", company_id=company.id
    )
    evidence = mem.store.get_evidence(db, company_id=company.id)
    assert evidence
    assert any(row.field_name == "spontaneous_application" for row in evidence)
    assert any(row.page_title == "Contact" for row in evidence)
    assert result.facts("recruitment_email")


def test_incremental_research_reuses_frontier(tmp_path: Path):
    pages = {"https://acme.ma/": Response("<a href='/careers'>Careers</a>"),
             "https://acme.ma/careers": Response("careers")}
    state = tmp_path / "research.json"
    first = researcher(pages, state_path=state).research("https://acme.ma/")
    second = researcher(pages, state_path=state).research("https://acme.ma/")
    assert first.visited
    assert second.visited == []


def test_llm_values_without_matching_evidence_are_discarded():
    result = researcher({"https://acme.ma/": Response("jobs@acme.ma")}).research("https://acme.ma/")
    assert evidence_backed_fields(
        {"email": "jobs@acme.ma", "company": "Invented Co"},
        result.evidence,
    ) == {"email": "jobs@acme.ma"}


def test_recruitment_announcement_finds_contact_on_different_page():
    pages = {
        "https://acme.ma/": Response('<a href="/news/recruitment">Recruitment announcement</a>'),
        "https://acme.ma/news/recruitment": Response(
            "<title>Recruitment announcement</title>"
            '<a href="/contact">Contact HR</a>'
        ),
        "https://acme.ma/contact": Response(
            "<title>HR contact</title>For this recruitment campaign email talent@acme.ma"
        ),
    }
    result = researcher(pages).research("https://acme.ma/")
    emails = result.facts("recruitment_email")
    assert emails and emails[0].extracted_value == "talent@acme.ma"
    assert emails[0].source_url.endswith("/contact")
