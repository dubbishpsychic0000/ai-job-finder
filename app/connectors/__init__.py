"""Connector package: multi-source discovery adapters.

Every adapter produces a normalized `Opportunity` — the single currency of the
pipeline. Priority: official API > RSS/feeds > HTML scraping. Each connector
declares its `source_type`, legal `access_mode` and a `policy_notice` (spec §34).
"""
from app.connectors.base import JobSource, Opportunity, get_connector, registry
from app.connectors.company_careers import CompanyCareersSource
from app.connectors.eures import EuresSource
from app.connectors.generic_api import GenericAPISource
from app.connectors.greenhouse import GreenhouseSource
from app.connectors.icims import ICIMSSource
from app.connectors.lever import LeverSource
from app.connectors.rss import RSSJobSource
from app.connectors.search_engine import SearchEngineSource
from app.connectors.smartrecruiters import SmartRecruitersSource
from app.connectors.static_files import StaticFilesSource
from app.connectors.workday import WorkdaySource

__all__ = [
    "CompanyCareersSource",
    "EuresSource",
    "GenericAPISource",
    "GreenhouseSource",
    "ICIMSSource",
    "JobSource",
    "LeverSource",
    "Opportunity",
    "RSSJobSource",
    "SearchEngineSource",
    "SmartRecruitersSource",
    "StaticFilesSource",
    "WorkdaySource",
    "get_connector",
    "registry",
]


def register_defaults() -> None:
    """Register the built-in connectors so `from app.connectors import registry` is ready."""
    registry.update({
        "rss": RSSJobSource,
        "company_careers": CompanyCareersSource,
        "search_engine": SearchEngineSource,
        "static_files": StaticFilesSource,
        "greenhouse": GreenhouseSource,
        "smartrecruiters": SmartRecruitersSource,
        "eures": EuresSource,
        "generic_api": GenericAPISource,
        "lever": LeverSource,
        "workday": WorkdaySource,
        "icims": ICIMSSource,
    })


register_defaults()
