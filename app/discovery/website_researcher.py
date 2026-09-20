"""Conservative, incremental research of public company websites."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from app import models
from app.discovery.email_verification import (
    ATS_VENDOR_DOMAINS,
    FREE_EMAIL_DOMAINS,
    EMAIL_RE,
    is_safe_email,
)

logger = logging.getLogger(__name__)
USER_AGENT = "WorldwideCareerAgent/0.1 (+public website research; respects robots.txt)"
PAGE_TERMS = {
    "careers": 100, "career": 100, "jobs": 95, "job": 90, "recruit": 95,
    "recruitment": 100, "hr": 80, "human-resources": 80, "join-us": 95,
    "join": 65, "contact": 75, "about": 45, "spontaneous": 100,
    "candidature-spontanee": 100, "application": 90, "emploi": 95,
}
EMAIL_LOCAL_TERMS = ("recruit", "recrut", "career", "emploi", "talent", "hr", "rh", "hiring", "jobs")


def normalize_url(url: str) -> str:
    parsed = urlparse(urldefrag((url or "").strip())[0])
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    path = parsed.path or "/"
    return parsed._replace(fragment="", query="", path=path.rstrip("/") or "/").geturl()


def host_domain(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def same_site(url: str, official_domain: str) -> bool:
    domain = host_domain(url)
    official = (official_domain or "").lower().lstrip(".")
    return bool(domain and official and (domain == official or domain.endswith("." + official)))


def classify_email(email: str, official_domain: str, source_domain: str = "") -> tuple[str, str, int]:
    """Return relationship, reason code, confidence without guessing."""
    value = email.lower().strip()
    domain = value.rsplit("@", 1)[-1] if "@" in value else ""
    official = (official_domain or "").lower().lstrip("www.")
    source = (source_domain or "").lower().lstrip("www.")
    if not is_safe_email(value):
        return "unsupported", "invalid_or_placeholder", 0
    if domain in ATS_VENDOR_DOMAINS or any(domain.endswith("." + d) for d in ATS_VENDOR_DOMAINS):
        return "ats_vendor", "ats_vendor_domain", 20
    if domain in FREE_EMAIL_DOMAINS:
        return "free_mail", "free_mail_domain", 20
    if official and (domain == official or domain.endswith("." + official)):
        local = value.split("@", 1)[0]
        recruitment = any(term in local for term in EMAIL_LOCAL_TERMS)
        return ("company_recruitment" if recruitment else "company_general",
                "employer_domain_recruitment" if recruitment else "employer_domain", 100)
    if source and (domain == source or domain.endswith("." + source)):
        return "third_party", "source_domain_not_official", 35
    return "third_party", "unrelated_domain", 20


@dataclass
class EvidenceFact:
    source_url: str
    source_type: str
    page_title: str
    field_name: str
    extracted_value: str
    snippet: str
    domain_relationship: str = ""
    confidence: int = 0
    reason_code: str = ""
    discovered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ResearchResult:
    official_domain: str
    pages: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    evidence: list[EvidenceFact] = field(default_factory=list)
    visited: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)

    def facts(self, field_name: str) -> list[EvidenceFact]:
        return [fact for fact in self.evidence if fact.field_name == field_name]


class WebsiteResearcher:
    """Prioritized same-site crawler with robots, host pacing and resumable state."""

    def __init__(self, *, max_pages: int = 12, per_host_delay: float = 0.25,
                 state_path: Path | None = None, session=None, fetcher=None,
                 robots_fetcher=None):
        self.max_pages = max(1, max_pages)
        self.per_host_delay = max(0.0, per_host_delay)
        self.state_path = state_path
        self.db_session = session if session is not None and hasattr(session, "add") else None
        self.session = requests.Session() if self.db_session is not None else (session or requests.Session())
        self.fetcher = fetcher or self._fetch
        self.robots_fetcher = robots_fetcher or self._robots
        self._last_fetch: dict[str, float] = {}
        self._robots_cache: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self._state = self._load_state()

    def research(self, start_url: str, *, company_id: int | None = None,
                 job_id: int | None = None, official_domain: str = "") -> ResearchResult:
        start = normalize_url(start_url)
        domain = (official_domain or host_domain(start)).lower().lstrip("www.")
        result = ResearchResult(official_domain=domain)
        if not start or not domain:
            return result
        queue: list[tuple[int, str]] = [(self._priority(start), start)]
        for sitemap_url in self.discover_sitemap(start):
            if same_site(sitemap_url, domain):
                queue.append((self._priority(sitemap_url), sitemap_url))
        seen = set(self._state.get(domain, []))
        while queue and len(result.visited) < self.max_pages:
            _, url = queue.pop(0)
            url = normalize_url(url)
            if not url or url in seen or not same_site(url, domain):
                continue
            seen.add(url)
            if not self._allowed(url):
                result.blocked.append(url)
                continue
            response = self.fetcher(url)
            if response is None:
                result.blocked.append(url)
                continue
            result.visited.append(url)
            content_type = response.headers.get("Content-Type", "").lower()
            if "pdf" in content_type or url.lower().endswith(".pdf"):
                result.documents.append(url)
                self._add(result, url, "pdf", "document_url", url, url, 50, "public_document",
                          company_id, job_id)
                continue
            if "html" not in content_type and content_type:
                continue
            soup = BeautifulSoup(response.text[:500_000], "lxml")
            title = soup.title.get_text(" ", strip=True)[:512] if soup.title else ""
            result.pages.append(url)
            self._extract_page(result, soup, url, title, domain, company_id, job_id)
            for href, anchor in self._links(soup, url):
                if same_site(href, domain) and href not in seen:
                    queue.append((self._priority(href, anchor), href))
            queue.sort(key=lambda item: item[0], reverse=True)
        self._state[domain] = sorted(seen)
        self._save_state()
        return result

    def research_and_apply(self, start_url: str, *, company_id: int | None = None,
                           job_id: int | None = None, official_domain: str = "") -> ResearchResult:
        """Research and project only evidence-backed routes into existing rows."""
        result = self.research(start_url, company_id=company_id, job_id=job_id,
                               official_domain=official_domain)
        if self.db_session is None:
            return result
        from app import memory as mem
        company = self.db_session.get(models.Company, company_id) if company_id else None
        job = self.db_session.get(models.Job, job_id) if job_id else None
        pages = result.facts("relevant_page")
        if company:
            company.official_domain = company.official_domain or result.official_domain
            for fact in pages:
                if fact.field_name != "relevant_page":
                    continue
                if fact.source_type in {"careers", "recruitment", "spontaneous"} and not company.careers_url:
                    company.careers_url = fact.extracted_value
                if fact.source_type in {"recruitment", "spontaneous"} and not company.recruitment_url:
                    company.recruitment_url = fact.extracted_value
            company.last_checked_at = datetime.now(timezone.utc)
        if job:
            for fact in result.evidence:
                if fact.field_name == "application_method" and not job.application_method:
                    job.application_method = "ONLINE_FORM"
                if fact.field_name == "application_url" and not job.application_url:
                    job.application_url = fact.extracted_value
                if fact.field_name == "recruitment_email" and not job.contact_email:
                    job.contact_email = fact.extracted_value
                    job.application_method = "EMAIL"
                if fact.field_name == "job_title" and not job.title:
                    job.title = fact.extracted_value
                if fact.field_name == "location" and not job.location:
                    job.location = fact.extracted_value
            for fact in pages:
                if fact.source_type in {"careers", "recruitment", "spontaneous"} and not job.application_url:
                    job.application_url = fact.extracted_value
        return result

    def _extract_page(self, result, soup, url, title, domain, company_id, job_id):
        self._current_title = title
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        page_kind = self._page_type(url, text)
        site_name = soup.find("meta", attrs={"property": "og:site_name"})
        if site_name and site_name.get("content"):
            self._add(result, url, page_kind, "company_name", site_name["content"],
                      str(site_name["content"]), 85, "og_site_name", company_id, job_id)
        heading = soup.find(["h1", "h2"])
        if heading and heading.get_text(" ", strip=True):
            self._add(result, url, page_kind, "job_title", heading.get_text(" ", strip=True)[:255],
                      heading.get_text(" ", strip=True), 70, "page_heading", company_id, job_id)
        for label, field_name in (("location", "location"), ("deadline", "deadline"),
                                  ("closing date", "deadline"), ("date limite", "deadline")):
            match = re.search(rf"{label}\s*[:\-]\s*([^|.;]{{2,120}})", text, re.I)
            if match:
                value = match.group(1).strip()
                self._add(result, url, page_kind, field_name, value, self._snippet(text, value),
                          75, "labeled_page_field", company_id, job_id)
        for email in dict.fromkeys(EMAIL_RE.findall(text)):
            relationship, reason, confidence = classify_email(email, domain, host_domain(url))
            if relationship in {"company_recruitment", "company_general", "free_mail", "ats_vendor", "third_party"}:
                local = email.split("@", 1)[0].lower()
                field = "recruitment_email" if relationship == "company_recruitment" else "general_email"
                self._add(result, url, page_kind, field, email, self._snippet(text, email),
                          confidence, reason, company_id, job_id, relationship)
        canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
        if canonical and canonical.get("href"):
            self._add(result, url, page_kind, "canonical_url", normalize_url(urljoin(url, canonical["href"])),
                      title or url, 80, "page_canonical", company_id, job_id)
        if page_kind in {"careers", "recruitment", "contact", "spontaneous"}:
            self._add(result, url, page_kind, "relevant_page", url, title or url, 90, "keyword_page", company_id, job_id)
        if re.search(r"(spontaneous|unsolicited|candidature spontan[ée]|candidatura espont[aâ]nea)", text, re.I):
            self._add(result, url, page_kind, "spontaneous_application", text[:800],
                      self._snippet(text, "spontaneous"), 90, "explicit_spontaneous_instruction", company_id, job_id)
        for phrase, field in (("apply online", "application_method"), ("postuler en ligne", "application_method"),
                              ("submit your cv", "application_method"), ("submit application", "application_method")):
            if phrase in text.lower():
                self._add(result, url, page_kind, field, "online_form", self._snippet(text, phrase),
                          85, "explicit_application_instruction", company_id, job_id)
        for href, anchor in self._links(soup, url):
            if re.search(r"\b(apply|application|postuler|candidater|submit)\b", anchor, re.I):
                self._add(result, url, page_kind, "application_url", href, anchor,
                          85, "application_link", company_id, job_id)

    def _add(self, result, url, source_type, field_name, value, snippet, confidence,
             reason, company_id, job_id, relationship=""):
        fact = EvidenceFact(url, source_type, getattr(self, "_current_title", ""), field_name, str(value), snippet[:1200],
                            relationship, confidence, reason)
        result.evidence.append(fact)
        if self.db_session is not None:
            from app import memory as mem
            mem.store.add_evidence(self.db_session, source_url=url, source_type=source_type,
                                   page_title=fact.page_title, field_name=field_name,
                                   extracted_value=fact.extracted_value, snippet=fact.snippet,
                                   domain_relationship=relationship, confidence=confidence,
                                   reason_code=reason, company_id=company_id, job_id=job_id)

    @staticmethod
    def _priority(url: str, anchor: str = "") -> int:
        value = f"{url} {anchor}".lower()
        return max((score for term, score in PAGE_TERMS.items() if term in value), default=10)

    @staticmethod
    def _page_type(url: str, text: str) -> str:
        value = f"{url} {text[:500]}".lower()
        if "spontaneous" in value or "candidature spontan" in value:
            return "spontaneous"
        if any(term in value for term in ("recruit", "join us", "emploi")):
            return "recruitment"
        if any(term in value for term in ("career", "careers", "jobs", "vacancies")):
            return "careers"
        if "contact" in value:
            return "contact"
        return "website"

    @staticmethod
    def _links(soup, base):
        for link in soup.select("a[href]"):
            href = normalize_url(urljoin(base, link["href"]))
            if href:
                yield href, link.get_text(" ", strip=True)
        for link in soup.select("a[href$='.pdf' i]"):
            href = normalize_url(urljoin(base, link["href"]))
            if href:
                yield href, link.get_text(" ", strip=True)

    def _allowed(self, url):
        domain = host_domain(url)
        parser = self._robots_cache.get(domain)
        if parser is None:
            parser = self.robots_fetcher(domain)
            self._robots_cache[domain] = parser
        if parser is not None and not parser.can_fetch(USER_AGENT, url):
            return False
        with self._lock:
            wait = self.per_host_delay - (time.monotonic() - self._last_fetch.get(domain, 0))
            if wait > 0:
                time.sleep(wait)
            self._last_fetch[domain] = time.monotonic()
        return True

    def _fetch(self, url):
        try:
            response = self.session.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            if response.status_code in {401, 403, 429}:
                return None
            response.raise_for_status()
            return response
        except requests.RequestException:
            return None

    def _robots(self, domain):
        parser = RobotFileParser()
        parser.set_url(f"https://{domain}/robots.txt")
        try:
            response = self.session.get(parser.url, headers={"User-Agent": USER_AGENT}, timeout=10)
            if response.status_code in {401, 403, 429}:
                return None
            if response.ok:
                parser.parse(response.text.splitlines())
                return parser
        except requests.RequestException:
            return None
        return None

    def discover_sitemap(self, start_url: str) -> list[str]:
        domain = host_domain(start_url)
        urls = []
        for path in ("/sitemap.xml", "/sitemap_index.xml"):
            response = self.fetcher(f"{urlparse(start_url).scheme}://{domain}{path}")
            if response is None:
                continue
            soup = BeautifulSoup(response.text, "xml")
            urls.extend(normalize_url(loc.get_text(strip=True)) for loc in soup.select("url loc, sitemap loc"))
        return list(dict.fromkeys(url for url in urls if url))

    @staticmethod
    def _snippet(text: str, needle: str) -> str:
        pos = text.lower().find(needle.lower())
        return text[max(0, pos - 180):pos + len(needle) + 300] if pos >= 0 else text[:500]

    def _load_state(self):
        if not self.state_path or not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._state), encoding="utf-8")


def evidence_backed_fields(values: dict, evidence: list[EvidenceFact]) -> dict:
    """Discard extraction output whose value is not present in source evidence."""
    source_values = {fact.extracted_value.lower() for fact in evidence}
    return {key: value for key, value in values.items()
            if value and (str(value).lower() in source_values or
                          any(str(value).lower() in fact.snippet.lower() for fact in evidence))}
