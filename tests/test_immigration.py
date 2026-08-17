"""Immigration whitelist + agent claim verification."""
from __future__ import annotations

import asyncio

from app.agents.immigration_agent import ImmigrationAgent
from app.agents.llm import NullLLM
from app.connectors.immigration.official import OfficialSourceFetcher, is_official


def test_whitelist_accepts_official():
    assert is_official("https://www.canada.ca/en/immigration.html")
    assert is_official("https://www.service-public.fr/particuliers/vosdroits/F17931")
    assert is_official("https://www.make-it-in-germany.com/en/visa-residence")
    assert is_official("https://immi.homeaffairs.gov.au/...")  # ignore path


def test_whitelist_rejects_blog():
    assert not is_official("https://some-blog.example/france-visa-guide.html")
    assert not is_official("https://vfs-tutorial.blogspot.com/visa-easy")


def test_fetcher_rejects_non_official_without_network():
    fetcher = OfficialSourceFetcher(timeout=1)
    page = fetcher.verify("https://some-blog.example/visa-guide")
    assert not page.ok
    assert "whitelist" in page.error


def test_immigration_agent_claims_carry_evidence():
    agent = ImmigrationAgent(NullLLM())
    # no network -> unverified, but must NEVER fabricate claims
    result = asyncio.run(agent.research("France", "civil engineering technician"))
    if result["status"] == "unverified":
        assert result["claims"] == []
    else:
        for claim in result["claims"]:
            assert claim["source"], "every claim must carry an official source"
            assert claim["verified_at"], "every claim must be timestamped"
