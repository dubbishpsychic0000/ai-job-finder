"""Public, robots-aware adapter for Morocco's Emploi-Public recruitment portal."""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup

from app.connectors.base import Opportunity, infer_country

logger = logging.getLogger(__name__)
PORTAL_HOSTS = {"emploi-public.ma", "www.emploi-public.ma"}
USER_AGENT = "WorldwideCareerAgent/0.1 (public recruitment discovery)"
_DETAIL_PATH = re.compile(
    r"^/(?:fr|ar)/concours/details/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$",
    re.IGNORECASE,
)
_RELEVANCE_TERMS = (
    "genie civil", "ingenierie civile", "travaux publics", "btp", "vrd",
    "batiment", "construction", "gros oeuvre", "technicien specialise",
    "technicien de 3eme grade", "technicien de 4eme grade",
    "technicien en genie civil", "etudes techniques", "chantier",
    "infrastructure", "routes", "dessin", "projection", "topographe",
    "الهندسة المدنية", "الأشغال العمومية", "البناء", "تقني متخصص",
    "مباراة تقني", "الطرق", "البنية التحتية", "الرسم", "المسح الطبوغرافي",
)
_FRENCH_MONTHS = {
    "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "aout": 8, "septembre": 9, "octobre": 10,
    "novembre": 11, "decembre": 12,
}
_ARABIC_MONTHS = {
    "يناير": 1, "فبراير": 2, "مارس": 3, "ابريل": 4, "أبريل": 4,
    "ماي": 5, "مايو": 5, "يونيو": 6, "يوليو": 7, "يوليوز": 7,
    "غشت": 8, "أغسطس": 8, "اغسطس": 8, "شتنبر": 9, "سبتمبر": 9,
    "أكتوبر": 10, "اكتوبر": 10, "نونبر": 11, "نوفمبر": 11,
    "دجنبر": 12, "ديسمبر": 12,
}
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def _plain(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", _plain(value).casefold())
    return "".join(char for char in value if not unicodedata.combining(char))


def _official_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in PORTAL_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
        )
    except ValueError:
        return False


def _portal_id(url: str) -> str:
    try:
        match = _DETAIL_PATH.fullmatch(urlsplit(url).path)
    except ValueError:
        return ""
    return match.group(1).lower() if match else ""


def _portal_datetime(value: str, *, end_of_day: bool = False) -> datetime | None:
    value = _plain(value).translate(_ARABIC_DIGITS)
    if not value:
        return None
    try:
        zone = ZoneInfo("Africa/Casablanca")
    except ZoneInfoNotFoundError:
        return None

    date_values: tuple[int, int, int] | None = None
    numeric = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", value)
    if numeric:
        day, month, year = (int(part) for part in numeric.groups())
        date_values = year, month, day
    else:
        month_names = {
            **_FRENCH_MONTHS,
            **_ARABIC_MONTHS,
        }
        month_pattern = "|".join(
            re.escape(month) for month in sorted(month_names, key=len, reverse=True)
        )
        named = re.search(
            rf"(\d{{1,2}})\s+({month_pattern})\s+(\d{{4}})",
            value,
            re.IGNORECASE,
        )
        if named:
            day = int(named.group(1))
            month = month_names[named.group(2).casefold()]
            year = int(named.group(3))
            date_values = year, month, day
    if date_values is None:
        return None

    clock = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", value)
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2))
    elif end_of_day:
        hour, minute = 23, 59
    else:
        hour, minute = 0, 0
    try:
        local_value = datetime(
            *date_values, hour, minute, tzinfo=zone,
        )
    except ValueError:
        return None
    return local_value.astimezone(timezone.utc)


def _stage(active_label: str, page_text: str) -> str:
    value = _fold(active_label)
    full = _fold(page_text)
    cancellation = (
        "annulation du concours", "concours annule", "concours est annule",
        "إلغاء المباراة", "الغاء المباراة", "تم إلغاء المباراة", "تم الغاء المباراة",
    )
    results = ("resultat", "liste des admis", "النتائج", "لائحة الناجحين")
    convocation = ("convocation", "استدعاء", "دعوة لاجتياز")
    other = ("avis modificatif", "report du concours", "تغيير تاريخ المباراة")
    if any(token in full for token in cancellation):
        return "CANCELLATION"
    if any(token in value for token in results):
        return "RESULTS_PUBLICATION"
    if any(token in value for token in convocation):
        return "EXAM_CONVOCATION"
    if any(token in value for token in other):
        return "OTHER_PROCEDURAL_NOTICE"
    if value in {"annonce", "اعلان"}:
        return "OPEN_ANNOUNCEMENT"
    return "UNKNOWN"


def _label_key(label: str) -> str:
    return _fold(label).replace("’", "'").strip(" :")


def _fields_from_description(box) -> tuple[dict[str, str], list[str]]:
    fields: dict[str, str] = {}
    lines: list[str] = []
    if box is None:
        return fields, lines
    for item in box.select("li"):
        text = _plain(item.get_text(" ", strip=True))
        if not text:
            continue
        lines.append(text)
        if ":" not in text:
            continue
        label, value = text.split(":", 1)
        fields[_label_key(label)] = _plain(value)
    return fields, lines


def _field(fields: dict[str, str], *labels: str) -> str:
    folded = {_label_key(key): value for key, value in fields.items()}
    for requested in labels:
        expected = _label_key(requested)
        for key, value in folded.items():
            if key == expected or expected in key:
                return value
    return ""


def _heading_value(soup: BeautifulSoup, *prefixes: str) -> str:
    expected = tuple(_fold(prefix) for prefix in prefixes)
    for heading in soup.select("h2, h3, h4"):
        text = _plain(heading.get_text(" ", strip=True))
        folded = _fold(text)
        for prefix in expected:
            if folded.startswith(prefix):
                return _plain(text[len(prefix):].lstrip(" :："))
    return ""


def _submission_route(kind: str, destination: str, destination_href: str) -> dict[str, Any]:
    kind_folded = _fold(kind)
    value = _plain(destination)
    href = _plain(destination_href)
    emails = re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", value)
    explicit_url = href if _official_or_https_url(href) else ""
    is_online = any(word in kind_folded for word in ("en ligne", "online", "الكتروني", "إلكتروني"))
    is_email = any(word in kind_folded for word in ("email", "e-mail", "courriel", "البريد الإلكتروني"))
    is_postal = any(word in kind_folded for word in ("postal", "courrier", "par voie postale", "البريد"))
    conflict = bool(
        (is_online and emails and not explicit_url)
        or (is_email and explicit_url and not emails)
        or (is_postal and (emails or explicit_url))
    )
    if conflict:
        route_kind = "AMBIGUOUS"
    elif explicit_url and is_online:
        route_kind = "ONLINE_PORTAL"
    elif emails and is_email:
        route_kind = "EMAIL"
    elif is_postal:
        route_kind = "POSTAL"
    elif explicit_url:
        route_kind = "ONLINE_PORTAL"
    elif emails:
        route_kind = "EMAIL"
    else:
        route_kind = "UNKNOWN"
    return {
        "kind": route_kind,
        "instructions_raw": kind,
        "destination_raw": value,
        "destination_url": explicit_url,
        "destination_email": emails[0] if emails else "",
        "conflict": conflict,
    }


def _official_or_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.port in {None, 443}
        )
    except ValueError:
        return False


def parse_competition_detail(
    html: str,
    url: str,
    *,
    language: str,
    observed_at: datetime,
) -> dict[str, Any]:
    """Parse one public detail page. PDF links are recorded, never downloaded."""
    portal_id = _portal_id(url)
    if not portal_id or not _official_url(url):
        return {
            "status": "UNKNOWN",
            "stage": "UNKNOWN",
            "reason_code": "invalid_official_detail_url",
            "evidence_url": url,
            "observed_at": observed_at.isoformat(),
            "portal_id": portal_id,
        }

    soup = BeautifulSoup(html or "", "html.parser")
    headings = soup.select("h1")
    page_heading = _plain(headings[0].get_text(" ", strip=True)) if headings else ""
    description_box = next(
        (
            box for box in soup.select("div.s-content-box.full")
            if box.find(["h2", "h3"])
            and _fold(box.find(["h2", "h3"]).get_text(" ", strip=True)) == "description"
        ),
        None,
    )
    fields, description_lines = _fields_from_description(description_box)
    title = _field(fields, "Nom du poste", "اسم المنصب", "المنصب")
    if not title:
        title = re.sub(
            r"^(?:Concours de recrutement|مباراة توظيف)\s*[:：]\s*",
            "",
            page_heading,
            flags=re.IGNORECASE,
        )
    employer = _heading_value(
        soup,
        "Administration qui recrute",
        "Administration organisatrice",
        "الإدارة المنظمة",
        "الإدارة التي توظف",
    )
    deadline_text = _heading_value(
        soup,
        "Délai de dépôt des candidatures",
        "Dernier délai de dépôt des candidatures",
        "Date limite de dépôt des candidatures",
        "آخر أجل لإيداع الترشيحات",
        "آخر أجل لإيداع ملفات الترشيح",
        "آخر أجل للتسجيل",
    )
    posted_text = _heading_value(
        soup,
        "Date de publication",
        "تاريخ النشر",
    )
    active = soup.select_one("nav .active, [aria-current='page'].active")
    active_label = _plain(active.get_text(" ", strip=True)) if active else ""
    page_text = _plain(soup.get_text(" ", strip=True))
    stage = _stage(active_label, page_text)
    location = _field(
        fields, "Région", "Lieu", "الجهة", "مكان المباراة", "مقر التعيين",
    )
    posts_raw = _field(fields, "Nombre de postes", "عدد المناصب")
    posts_match = re.search(r"\d+", posts_raw.translate(_ARABIC_DIGITS))
    post_count = int(posts_match.group()) if posts_match else None
    deposit_type = _field(
        fields, "Type de dépôt", "Modalité de dépôt", "نوع إيداع الترشيح",
    )
    deposit_site = _field(
        fields, "Site de dépôt", "Adresse de dépôt", "موقع إيداع الترشيح",
    )
    destination_href = ""
    if description_box is not None:
        for item in description_box.select("li"):
            item_text = _fold(item.get_text(" ", strip=True))
            if any(
                _fold(word) in item_text
                for word in ("site de depot", "موقع إيداع الترشيح")
            ):
                link = item.select_one("a[href]")
                if link:
                    destination_href = _plain(link.get("href", ""))
                    if destination_href and not destination_href.startswith(
                        ("http://", "https://", "mailto:")
                    ):
                        destination_href = urljoin(url, destination_href)
                    break
    submission = _submission_route(deposit_type, deposit_site, destination_href)
    diploma = _field(
        fields, "Diplôme demandé", "Diplômes demandés", "Diplôme(s) demandé(s)",
        "الشهادة المطلوبة", "الشهادات المطلوبة",
    )
    specialty = _field(
        fields, "Spécialité", "Spécialité demandée", "التخصص", "التخصص المطلوب",
    )
    experience = _field(fields, "Expérience", "Expérience exigée", "الخبرة")
    nationality = _field(
        fields, "Nationalité", "Conditions de nationalité", "الجنسية",
    )
    age = _field(fields, "Âge", "Limite d'âge", "السن", "السن المطلوب")
    exam_text = _field(
        fields, "Date de l'examen", "Date d'examen", "تاريخ المباراة",
    )
    exam_at = _portal_datetime(exam_text)
    notice_documents: list[dict[str, str]] = []
    for link in soup.select("a[href]"):
        href = _plain(link.get("href", ""))
        if not _official_url(urljoin(url, href)):
            continue
        path = urlsplit(urljoin(url, href)).path.casefold()
        if "/concours/download/" not in path and "/تحميل/المباريات/" not in path:
            continue
        notice_documents.append({
            "label": _plain(link.get_text(" ", strip=True)),
            "url": urljoin(url, href),
        })

    deadline = _portal_datetime(deadline_text, end_of_day=True)
    posted_at = _portal_datetime(posted_text)
    if stage == "CANCELLATION":
        status, reason = "CANCELLATION", "official_cancellation_notice"
    elif stage in {
        "EXAM_CONVOCATION", "RESULTS_PUBLICATION", "OTHER_PROCEDURAL_NOTICE",
    }:
        status, reason = stage, "official_procedural_notice"
    elif stage != "OPEN_ANNOUNCEMENT":
        status, reason = "UNKNOWN", "announcement_stage_not_explicit"
    elif deadline is None:
        status, reason = "UNKNOWN", "deadline_missing_or_ambiguous"
    elif deadline <= observed_at:
        status, reason = "CLOSED_ANNOUNCEMENT", "application_deadline_elapsed"
    elif submission["conflict"]:
        status, reason = "UNKNOWN", "submission_route_conflict"
    elif (
        not all((title, employer, location, description_lines, deposit_type, deposit_site))
        or len("\n".join(description_lines)) < 80
    ):
        status, reason = "UNKNOWN", "detail_page_missing_required_evidence"
    else:
        status, reason = "OPEN_ANNOUNCEMENT", "official_detail_open_before_deadline"

    grade_match = re.search(
        r"\b(?:\d(?:er|e|ème|eme)?\s+grade|grade\s+\d)\b",
        title,
        re.IGNORECASE,
    )
    description = "\n".join(description_lines)
    return {
        "source": "emploi_public_ma",
        "portal_id": portal_id,
        "competition_code": _field(
            fields, "Code du concours", "رقم المباراة", "رمز المباراة",
        ),
        "status": status,
        "stage": stage,
        "verification_status": (
            "verified" if status == "OPEN_ANNOUNCEMENT"
            else "closed" if status == "CLOSED_ANNOUNCEMENT"
            else "unverified"
        ),
        "reason_code": (
            "official_public_competition_verified_open"
            if status == "OPEN_ANNOUNCEMENT" else reason
        ),
        "evidence_url": url,
        "observed_at": observed_at.isoformat(),
        "language": language,
        "title_original": title,
        "administration_original": employer,
        "grade_original": grade_match.group() if grade_match else "",
        "specialty_original": specialty,
        "post_count": post_count,
        "location_original": location,
        "diploma_requirements_original": diploma,
        "experience_requirements_original": experience,
        "nationality_requirements_original": nationality,
        "age_requirements_original": age,
        "requirements_original": {
            "diploma": diploma,
            "specialty": specialty,
            "experience": experience,
            "nationality": nationality,
            "age": age,
        },
        "submission_route": submission,
        "submission_instructions_original": deposit_type,
        "submission_destination_original": deposit_site,
        "notice_documents": notice_documents,
        "notice_documents_not_fetched": bool(notice_documents),
        "deadline_original": deadline_text,
        "deadline_at": deadline.isoformat() if deadline else "",
        "posted_at_original": posted_text,
        "posted_at": posted_at.isoformat() if posted_at else "",
        "exam_date_original": exam_text,
        "exam_at": exam_at.isoformat() if exam_at else "",
        "description_original": description,
        "page_heading_original": page_heading,
    }


def _listing_cards(html: str, base_url: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    found: dict[str, dict[str, str]] = {}
    for anchor in soup.select("a[href]"):
        href = urljoin(base_url, _plain(anchor.get("href", "")))
        if not _official_url(href) or not _portal_id(href):
            continue
        parent = anchor
        for _ in range(5):
            if parent.parent is None:
                break
            parent = parent.parent
            text = _plain(parent.get_text(" ", strip=True))
            if len(text) >= 45:
                break
        label = _plain(anchor.get_text(" ", strip=True))
        found.setdefault(href, {"url": href, "title": label, "snippet": text})
    return list(found.values())


def _relevance(value: str) -> int:
    folded = _fold(value)
    return sum(2 if term in folded else 0 for term in _RELEVANCE_TERMS)


class EmploiPublicSource:
    """Read current public French/Arabic recruitment listings and detail pages.

    The portal's robots.txt disallows document-download paths. The adapter
    records official notice links but never requests PDF/dossier URLs.
    """

    name = "emploi_public_ma"
    kind = "html"
    source_type = "government"
    access_mode = "public"
    policy_notice = (
        "Public Emploi-Public.ma listing/detail pages only; no login, account, "
        "CAPTCHA, submission, or robots-disallowed document downloads."
    )

    def __init__(
        self,
        *,
        source_name: str = "emploi_public_ma",
        max_pages: int = 2,
        max_detail_pages: int = 12,
        listing_urls: dict[str, str] | None = None,
        get=None,
    ):
        self.source_name = source_name
        self.max_pages = max(1, min(3, int(max_pages)))
        self.max_detail_pages = max(1, min(12, int(max_detail_pages)))
        self.listing_urls = listing_urls or {
            "fr": "https://www.emploi-public.ma/fr/concours-liste",
            "ar": "https://www.emploi-public.ma/ar/",
        }
        self.get = get or requests.get
        self._listing_cache: dict[str, list[dict[str, str]]] = {}
        self._detail_cache: dict[str, dict[str, Any]] = {}
        self._fetched_detail_ids: set[str] = set()
        self._returned_languages: set[str] = set()
        self._robots_checked = False
        self._robots_parser: RobotFileParser | None = None
        self._http_requests = 0
        self._request_metrics = {
            "robots_requests_attempted": 0,
            "robots_responses_fetched": 0,
            "listing_pages_attempted": 0,
            "listing_pages_fetched": 0,
            "listing_pages_parsed": 0,
            "listing_cards_parsed": 0,
            "detail_pages_attempted": 0,
            "detail_pages_fetched": 0,
            "detail_pages_parsed": 0,
        }
        self.last_page_requests = 0
        self.last_request_metrics = dict(self._request_metrics)
        self.last_status_counts: dict[str, int] = {}
        self.last_origin = ""
        self.last_status = ""
        self.last_error_code = ""

    def _load_robots(self) -> bool:
        if self._robots_checked:
            return self._robots_parser is not None
        self._robots_checked = True
        robots_url = "https://www.emploi-public.ma/robots.txt"
        self._http_requests += 1
        self._request_metrics["robots_requests_attempted"] += 1
        try:
            response = self.get(
                robots_url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/plain"},
                timeout=10,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            logger.info("Emploi-Public robots check failed (%s)", type(exc).__name__)
            return False
        if int(getattr(response, "status_code", 0) or 0) != 200:
            return False
        content = getattr(response, "content", b"")
        if len(content) > 65536:
            return False
        self._request_metrics["robots_responses_fetched"] += 1
        if isinstance(content, bytes):
            text = content.decode("utf-8", errors="replace")
        else:
            text = str(getattr(response, "text", "") or "")
        parser = RobotFileParser(robots_url)
        parser.parse(text.splitlines())
        self._robots_parser = parser
        return True

    def _fetch_html(self, url: str, *, page_kind: str) -> str:
        if not _official_url(url):
            return ""
        path = urlsplit(url).path.casefold()
        if "/concours/download/" in path or "/تحميل/المباريات/" in path:
            return ""
        if not self._load_robots() or not self._robots_parser.can_fetch(USER_AGENT, url):
            return ""
        self._http_requests += 1
        metric = f"{page_kind}_pages"
        self._request_metrics[f"{metric}_attempted"] += 1
        try:
            response = self.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                timeout=15,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            logger.info("Emploi-Public page fetch failed (%s)", type(exc).__name__)
            return ""
        if int(getattr(response, "status_code", 0) or 0) != 200:
            return ""
        content_type = str(
            getattr(response, "headers", {}).get("content-type", ""),
        ).casefold()
        if content_type and "text/html" not in content_type:
            return ""
        content = getattr(response, "content", b"")
        if len(content) > 2_000_000:
            return ""
        if isinstance(content, bytes):
            html = content.decode("utf-8", errors="replace")
        else:
            html = str(getattr(response, "text", "") or "")
        self._request_metrics[f"{metric}_fetched"] += 1
        return html

    def _load_listings(self, language: str) -> list[dict[str, str]]:
        if language in self._listing_cache:
            return self._listing_cache[language]
        first_url = self.listing_urls.get(language, "")
        pages = [first_url] if _official_url(first_url) else []
        cards: dict[str, dict[str, str]] = {}
        page_index = 0
        while page_index < min(len(pages), self.max_pages):
            page_url = pages[page_index]
            page_index += 1
            html = self._fetch_html(page_url, page_kind="listing")
            if not html:
                continue
            parsed_cards = _listing_cards(html, page_url)
            self._request_metrics["listing_pages_parsed"] += 1
            self._request_metrics["listing_cards_parsed"] += len(parsed_cards)
            for card in parsed_cards:
                cards.setdefault(card["url"], card)
            soup = BeautifulSoup(html, "html.parser")
            for link in soup.select("nav .pagination a[href], .pagination a[href], a[rel='next']"):
                label = _fold(link.get_text(" ", strip=True))
                if (
                    link.get("rel") == ["next"]
                    or label in {"suivant", "next", "»", "›", "2"}
                ):
                    next_url = urljoin(page_url, link.get("href", ""))
                    if _official_url(next_url) and next_url not in pages:
                        pages.append(next_url)
                        break
        self._listing_cache[language] = list(cards.values())
        return self._listing_cache[language]

    def _detail(self, card: dict[str, str], language: str, observed_at: datetime):
        url = card["url"]
        portal_id = _portal_id(url)
        cache_key = portal_id or url
        if cache_key not in self._detail_cache:
            if len(self._fetched_detail_ids) >= self.max_detail_pages:
                metadata = {
                    "source": self.name,
                    "portal_id": portal_id,
                    "status": "UNKNOWN",
                    "stage": "UNKNOWN",
                    "verification_status": "unverified",
                    "reason_code": "detail_page_limit_reached",
                    "evidence_url": url,
                    "observed_at": observed_at.isoformat(),
                    "title_original": card.get("title", ""),
                    "administration_original": "",
                    "location_original": "",
                    "description_original": card.get("snippet", ""),
                    "notice_documents": [],
                }
            else:
                self._fetched_detail_ids.add(cache_key)
                html = self._fetch_html(url, page_kind="detail")
                if html:
                    metadata = parse_competition_detail(
                        html, url, language=language, observed_at=observed_at,
                    )
                    self._request_metrics["detail_pages_parsed"] += 1
                else:
                    metadata = {
                        "source": self.name,
                        "portal_id": portal_id,
                        "status": "UNKNOWN",
                        "stage": "UNKNOWN",
                        "verification_status": "unverified",
                        "reason_code": "detail_page_unavailable",
                        "evidence_url": url,
                        "observed_at": observed_at.isoformat(),
                        "title_original": card.get("title", ""),
                        "administration_original": "",
                        "location_original": "",
                        "description_original": card.get("snippet", ""),
                        "notice_documents": [],
                    }
            self._detail_cache[cache_key] = metadata
        return self._detail_cache[cache_key]

    async def search(self, query: str, location: str = "") -> list[Opportunity]:
        before = self._http_requests
        before_metrics = dict(self._request_metrics)
        self.last_status_counts = {}
        language = "ar" if re.search(r"[\u0600-\u06ff]", query or "") else "fr"
        if language in self._returned_languages:
            self.last_page_requests = 0
            self.last_request_metrics = {
                key: 0 for key in self._request_metrics
            }
            self.last_origin = "cache"
            self.last_status = "cached"
            self.last_error_code = ""
            return []
        self._returned_languages.add(language)
        now = datetime.now(timezone.utc)
        cards = self._load_listings(language)
        candidates = sorted(
            cards,
            key=lambda card: _relevance(
                f"{card.get('title', '')} {card.get('snippet', '')}",
            ),
            reverse=True,
        )
        out: list[Opportunity] = []
        seen_ids: set[str] = set()
        for card in candidates:
            if _relevance(f"{card.get('title', '')} {card.get('snippet', '')}") <= 0:
                continue
            metadata = self._detail(card, language, now)
            title = str(metadata.get("title_original") or card.get("title") or "")
            description = str(
                metadata.get("description_original") or card.get("snippet") or "",
            )
            if _relevance(f"{title}\n{description}") <= 0:
                continue
            portal_id = str(metadata.get("portal_id") or _portal_id(card["url"]))
            code = str(metadata.get("competition_code") or "")
            external_id = portal_id or code or card["url"]
            if external_id in seen_ids:
                continue
            seen_ids.add(external_id)
            status = str(metadata.get("status") or "UNKNOWN")
            self.last_status_counts[status] = (
                self.last_status_counts.get(status, 0) + 1
            )
            company = str(metadata.get("administration_original") or "")
            area = str(metadata.get("location_original") or location or "Morocco")
            if not metadata.get("title_original"):
                metadata["title_original"] = title
            out.append(Opportunity(
                source=self.source_name,
                external_id=external_id,
                title=title[:255] or "Public competition",
                company=company[:255],
                location=area[:255],
                country=infer_country(area) or "Morocco",
                description=description[:20000],
                url=card["url"],
                posted_at=_portal_datetime(
                    str(metadata.get("posted_at_original") or ""),
                ),
                posted_at_source=(
                    "emploi_public:date_publication"
                    if metadata.get("posted_at") else ""
                ),
                posted_at_evidence_url=(
                    card["url"] if metadata.get("posted_at") else ""
                ),
                closing_at=(
                    datetime.fromisoformat(metadata["deadline_at"])
                    if metadata.get("deadline_at") else None
                ),
                employment_type="government_competition",
                language=language,
                verification_status=str(
                    metadata.get("verification_status") or "unverified",
                ),
                raw={"public_competition": metadata},
            ))
        self.last_page_requests = self._http_requests - before
        self.last_request_metrics = {
            key: self._request_metrics[key] - before_metrics[key]
            for key in self._request_metrics
        }
        self.last_origin = "direct"
        self.last_status = "ok" if out else "empty"
        self.last_error_code = ""
        return out
