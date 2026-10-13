from __future__ import annotations

import json
import time
from pathlib import Path

from app import memory as mem
from app.discovery.website_researcher import (
    WebsiteResearcher,
    classify_email,
    evidence_backed_fields,
    extract_application_instructions,
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


def test_sitemap_long_tail_does_not_crowd_out_linked_contact_and_careers_pages():
    long_tail = "".join(
        f"<url><loc>https://acme.ma/projects/archive-{index}</loc></url>"
        for index in range(60)
    )
    pages = {
        "https://acme.ma/sitemap.xml": Response(
            "<urlset>"
            + long_tail
            + "<url><loc>https://acme.ma/fr/contacteznous</loc></url>"
            + "<url><loc>https://acme.ma/careers</loc></url>"
            + "</urlset>",
            "application/xml",
        ),
        "https://acme.ma/sitemap_index.xml": None,
        "https://acme.ma/": Response(
            '<nav><a href="/fr/contacteznous">Contact</a>'
            '<a href="/careers">Careers</a></nav>'
            '<a href="/projects/archive">Old projects</a>'
        ),
        "https://acme.ma/fr/contacteznous": Response(
            "<title>Contact</title><h1>Contactez-nous</h1>"
            "Envoyez votre CV en précisant le poste souhaité à rh@acme.ma."
        ),
        "https://acme.ma/careers": Response(
            "<title>Careers</title><h1>Careers</h1>"
            "Open positions and spontaneous applications."
        ),
        "https://acme.ma/projects/archive": Response(
            "<title>Projects</title><h1>Projects</h1>"
            "Our historical construction projects mention recruitment."
        ),
    }
    result = researcher(pages, max_pages=3).research("https://acme.ma/")

    assert result.pages == [
        "https://acme.ma/",
        "https://acme.ma/careers",
        "https://acme.ma/fr/contacteznous",
    ]
    contact_relevant = [
        fact for fact in result.facts("relevant_page")
        if fact.source_url.endswith("/fr/contacteznous")
    ]
    assert contact_relevant[0].source_type == "contact"
    assert not any(
        fact.field_name == "relevant_page"
        and fact.source_url.endswith("/projects/archive")
        for fact in result.evidence
    )
    assert extract_application_instructions(
        "Envoyez votre CV en précisant le poste souhaité à rh@acme.ma."
    )


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


def test_safe_same_site_redirects_follow_only_https_canonical_host():
    class RedirectResponse(Response):
        def __init__(self, status_code, location=""):
            super().__init__()
            self.status_code = status_code
            self.headers["Location"] = location

    class FakeSession:
        def __init__(self, responses):
            self.responses = iter(responses)
            self.requested = []

        def get(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            self.requested.append(url)
            return next(self.responses)

    session = FakeSession([
        RedirectResponse(301, "https://www.megec.com/candidature-spontanee/"),
        Response("<title>Candidature spontanée</title>"),
    ])
    crawler = WebsiteResearcher(
        session=session,
        per_host_delay=0,
        allow_redirects=False,
        allow_safe_same_site_redirects=True,
    )

    response = crawler._fetch("https://www.megec.com/candidature-spontanee")

    assert response is not None
    assert session.requested == [
        "https://www.megec.com/candidature-spontanee",
        "https://www.megec.com/candidature-spontanee/",
    ]

    for location in (
        "https://example.net/private",
        "http://www.megec.com/candidature-spontanee/",
        "https://www.megec.com:8443/candidature-spontanee/",
    ):
        blocked_session = FakeSession([RedirectResponse(302, location)])
        blocked_crawler = WebsiteResearcher(
            session=blocked_session,
            per_host_delay=0,
            allow_redirects=False,
            allow_safe_same_site_redirects=True,
        )
        assert blocked_crawler._fetch(
            "https://www.megec.com/candidature-spontanee"
        ) is None
        assert blocked_session.requested == [
            "https://www.megec.com/candidature-spontanee",
        ]


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


def test_incremental_research_without_database_refetches_for_missing_evidence(
    tmp_path: Path,
):
    pages = {"https://acme.ma/": Response("<a href='/careers'>Careers</a>"),
             "https://acme.ma/careers": Response("careers")}
    state = tmp_path / "research.json"
    first = researcher(pages, state_path=state).research("https://acme.ma/")
    second = researcher(pages, state_path=state).research("https://acme.ma/")
    assert first.visited
    assert second.visited == ["https://acme.ma/"]
    assert second.revisited_missing_evidence == ["https://acme.ma/"]


def test_failed_fetch_is_retried(tmp_path: Path):
    attempts = {"count": 0}

    def fetch(url):
        if url.endswith(("sitemap.xml", "sitemap_index.xml")):
            return None
        attempts["count"] += 1
        return None if attempts["count"] == 1 else Response("careers")

    crawler = WebsiteResearcher(
        max_pages=1, per_host_delay=0, state_path=tmp_path / "state.json",
        fetcher=fetch, robots_fetcher=lambda domain: None,
    )
    first = crawler.research("https://acme.ma/")
    second = crawler.research("https://acme.ma/")
    assert not first.visited
    assert second.visited == ["https://acme.ma/"]
    assert attempts["count"] == 2


def test_research_refreshes_expired_pages(tmp_path: Path):
    pages = {"https://acme.ma/": Response("Careers")}
    state = tmp_path / "state.json"
    first = researcher(pages, state_path=state).research("https://acme.ma/")
    assert first.visited
    saved = json.loads(state.read_text(encoding="utf-8"))
    saved["acme.ma"]["https://acme.ma/"] = time.time() - 8 * 86400
    state.write_text(json.dumps(saved), encoding="utf-8")
    refreshed = researcher(pages, state_path=state).research("https://acme.ma/")
    assert refreshed.visited == ["https://acme.ma/"]


def test_previously_visited_url_is_refetched_when_database_evidence_is_missing(
    db, tmp_path: Path,
):
    pages = {"https://acme.ma/": Response("<title>Acme</title>")}
    state = tmp_path / "research.json"
    first = researcher(pages, db=db, state_path=state, max_pages=1).research(
        "https://acme.ma/", company_id=42,
    )
    second = researcher(pages, db=db, state_path=state, max_pages=1).research(
        "https://acme.ma/", company_id=42,
    )

    assert first.visited == ["https://acme.ma/"]
    assert second.visited == ["https://acme.ma/"]
    assert second.revisited_missing_evidence == ["https://acme.ma/"]


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
