"""ATS / career-site technology detection (spec §8).

Given a career-page URL and/or its HTML body, detect which Applicant Tracking
System (Workday, Greenhouse, Lever, SmartRecruiters, iCIMS, Taleo/Oracle,
SAP SuccessFactors, ...) powers it. Detection uses public markers only — page
source and obvious URLs — never probes authenticated endpoints.
"""
from __future__ import annotations

from urllib.parse import urlsplit

ATS_MARKERS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "greenhouse": (("greenhouse.io", "boards.greenhouse.io", "grnh.se"), ("greenhouse", "gh_src")),
    "lever": (("lever.co", "jobs.lever.co"), ("lever",)),
    "smartrecruiters": (("smartrecruiters.com",), ("smartrecruiters",)),
    "workday": (("myworkdayjobs.com", "wd5.myworkdaysite.com", "wdhr"), ("workday", "wd=urn:li:jobposting")),
    "icims": (("icims.com",), ("icims",)),
    "oracle": (("oraclecloud.com", "taleo.net", "taleo"), ("taleo", "oracle recruiting")),
    "successfactors": (("successfactors.eu", "successfactors.com"), ("successfactors", "sap successfactors")),
    "teamtailor": (("teamtailor.com", "teamtailor"), ("teamtailor",)),
    "personio": (("personio.com",), ("personio",)),
    "recruitee": (("recruitee.com",), ("recruitee",)),
    "ashby": (("ashbyhq.com",), ("ashby", "ashbyhq")),
}


def detect_ats(url: str = "", html: str = "") -> str:
    """Return the ATS name for a career URL/page body, or '' when unknown."""
    if not isinstance(url, str):
        host = ""
    else:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            host = ""
    body = (html or "")[:200_000].lower()
    for name, (hosts, markers) in ATS_MARKERS.items():
        host_match = any(
            host == marker or host.endswith(f".{marker}")
            for marker in hosts
        )
        if host_match or any(m in body for m in markers):
            return name
    return ""
