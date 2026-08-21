from __future__ import annotations

import asyncio

from app.connectors.search_engine import SearchEngineSource


def test_official_ats_search_filters_non_ats_results(monkeypatch):
    html = '''<div class="result"><a class="result__a" href="https://jobs.lever.co/acme/123">Civil Technician</a></div>
    <div class="result"><a class="result__a" href="https://example.com/job">Civil Technician</a></div>'''

    class Response:
        text = html
        def raise_for_status(self): pass

    monkeypatch.setattr("app.connectors.search_engine.requests.post", lambda *a, **k: Response())
    jobs = asyncio.run(SearchEngineSource(official_only=True).search("civil", "France"))
    assert len(jobs) == 1
    assert jobs[0].source_type == "ats"
    assert jobs[0].company == "Acme"
