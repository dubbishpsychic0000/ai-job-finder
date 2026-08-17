"""Connector package: multi-source discovery adapters.

Every adapter produces a normalized `Opportunity` — the single currency of the
pipeline. Priority: official API > RSS/feeds > HTML scraping.
"""
from app.connectors.base import JobSource, Opportunity, get_connector, registry
from app.connectors.company_careers import CompanyCareersSource
from app.connectors.rss import RSSJobSource
from app.connectors.search_engine import SearchEngineSource
from app.connectors.static_files import StaticFilesSource

__all__ = [
    "CompanyCareersSource",
    "JobSource",
    "Opportunity",
    "RSSJobSource",
    "SearchEngineSource",
    "StaticFilesSource",
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
    })


register_defaults()
